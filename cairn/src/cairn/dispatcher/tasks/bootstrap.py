from __future__ import annotations

import logging
import time

from cairn.agent.settings import AgentSettings
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
)
from cairn.dispatcher.prompting import format_hints, load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.workspace import WorkspaceManager
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    cancel_reason,
    did_timeout,
    preview,
    project_allows_conclude_fallback,
    task_healthcheck_enabled,
    write_conclude_result,
    write_conclude_result_with_fact_id,
)
from cairn.dispatcher.tasks.session import WorkerSession
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_bootstrap_task(
    config: DispatchConfig,
    client: CairnClient,
    workspace: WorkspaceManager,
    agent_settings: AgentSettings,
    project: ProjectDetail,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    task_started = time.perf_counter()
    session = WorkerSession(
        config,
        workspace,
        project.project.id,
        worker,
        agent_settings,
        task_type="bootstrap",
        run_id=f"bootstrap-{intent.id}",
    )
    lease = HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    lease.start()
    session.attach(lease, cancellation)
    try:
        if task_healthcheck_enabled(config):
            LOG.info("checking agent model project=%s intent=%s worker=%s timeout=%ss", project.project.id, intent.id, worker.name, config.runtime.healthcheck_timeout)
            health = session.ping(config.runtime.healthcheck_timeout)
            if cancellation.is_cancelled:
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "failed"
            if not health.ok:
                LOG.warning("agent model unhealthy project=%s intent=%s worker=%s detail=%s", project.project.id, intent.id, worker.name, health.detail)
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "unhealthy"

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "bootstrap.md"),
            _bootstrap_prompt_replacements(project),
        )
        execute_started = time.perf_counter()
        first = session.run(prompt, phase="bootstrap", timeout=config.tasks.bootstrap.timeout, tools=True)
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info("bootstrap cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s", project.project.id, intent.id, worker.name, cancelled, execute_ms)
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning("heartbeat lost during bootstrap project=%s intent=%s worker=%s status=%s execute_ms=%s", project.project.id, intent.id, worker.name, lease.failure.status_code, execute_ms)
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                payload = parse_json_output(session.response_text(first))
                kind, data = validate_bootstrap_execute_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "bootstrap parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    total_ms,
                    preview(first.stdout),
                )
                return _try_conclude_fallback(config, client, session, project, intent, lease, cancellation)
            if kind == "rejected":
                LOG.warning("bootstrap rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s", project.project.id, intent.id, worker.name, execute_ms, total_ms)
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            return _write_bootstrap_complete_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                data["fact_description"],
                data["complete_description"],
                source="bootstrap",
                phase_ms=execute_ms,
                total_ms=total_ms,
            )
        if did_timeout(first):
            LOG.warning(
                "bootstrap timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(first.stdout),
            )
            return _try_conclude_fallback(config, client, session, project, intent, lease, cancellation)
        LOG.warning(
            "bootstrap agent failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            first.returncode,
            execute_ms,
            total_ms,
            preview(first.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("bootstrap task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()
        session.close()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: CairnClient,
    session: WorkerSession,
    project: ProjectDetail,
    intent: Intent,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not session.supports_conclude or not session.has_session:
        LOG.info(
            "bootstrap conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project.project.id,
            intent.id,
            session.worker.name,
            session.supports_conclude,
            session.has_session,
        )
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"
    if lease.failure is not None:
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"
    if cancellation.is_cancelled:
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "cancelled"
    if not project_allows_conclude_fallback(client, project.project.id, worker_name=session.worker.name, intent_id=intent.id):
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "bootstrap_conclude.md"),
        _bootstrap_prompt_replacements(project),
    )
    LOG.info("starting bootstrap conclude fallback project=%s intent=%s worker=%s", project.project.id, intent.id, session.worker.name)
    conclude_started = time.perf_counter()
    result = session.conclude(prompt, phase="bootstrap_conclude", timeout=config.tasks.bootstrap.conclude_timeout)
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "bootstrap conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            session.worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"
    try:
        payload = parse_json_output(session.response_text(result))
        kind, fact_description = validate_bootstrap_conclude_payload(payload)
    except Exception as exc:
        LOG.warning(
            "bootstrap conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s",
            project.project.id,
            intent.id,
            session.worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"
    if kind == "rejected":
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "rejected"
    return write_conclude_result(
        client,
        project.project.id,
        intent.id,
        session.worker.name,
        fact_description or "",
        source="bootstrap_conclude",
        phase_ms=conclude_ms,
    )


def _bootstrap_prompt_replacements(project: ProjectDetail) -> dict[str, str]:
    facts = {fact.id: fact.description for fact in project.facts}
    hints = [
        {"id": hint.id, "content": hint.content, "creator": hint.creator, "created_at": hint.created_at}
        for hint in project.hints
    ]
    return {
        "origin": facts.get("origin", ""),
        "goal": facts.get("goal", ""),
        "hints": format_hints(hints),
    }


def _write_bootstrap_complete_result(
    client: CairnClient,
    project_id: str,
    intent_id: str,
    worker_name: str,
    fact_description: str,
    complete_description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> str:
    conclude = write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        fact_description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    )
    if conclude.status != "success":
        return "failed"
    if conclude.fact_id is None:
        LOG.warning(
            "bootstrap complete deferred because conclude response omitted fact id project=%s intent=%s worker=%s source=%s",
            project_id,
            intent_id,
            worker_name,
            source,
        )
        return "success"

    response = client.complete(project_id, [conclude.fact_id], complete_description, worker_name)
    if response.status_code in (403, 409):
        LOG.info(
            "bootstrap complete deferred project=%s intent=%s worker=%s source=%s status=%s fact_id=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            response.status_code,
            conclude.fact_id,
        )
        return "success"
    if not response.ok:
        LOG.warning(
            "bootstrap complete write failed project=%s intent=%s worker=%s source=%s fact_id=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            conclude.fact_id,
            response.status_code,
            response.text,
        )
        return "success"
    if total_ms is None:
        LOG.info("bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s", project_id, intent_id, worker_name, source, [conclude.fact_id], phase_ms)
    else:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s total_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
            total_ms,
        )
    return "success"
