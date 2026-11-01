from __future__ import annotations

import logging
import time

from cairn.agent.settings import AgentSettings
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_explore_payload
from cairn.dispatcher.prompting import load_prompt, render_prompt
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
    write_graph_snapshot_reference,
)
from cairn.dispatcher.tasks.session import WorkerSession
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_explore_task(
    config: DispatchConfig,
    client: CairnClient,
    workspace: WorkspaceManager,
    agent_settings: AgentSettings,
    project: ProjectDetail,
    export_yaml: str,
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
        task_type="explore",
        run_id=f"explore-{intent.id}",
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
            load_prompt(config.runtime.prompt_group, "explore.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    session.workspace_path,
                    export_yaml.strip(),
                    phase="explore_execute",
                ),
                "intent_id": intent.id,
                "intent_description": intent.description,
            },
        )
        execute_started = time.perf_counter()
        first = session.run(prompt, phase="explore_execute", timeout=config.tasks.explore.timeout, tools=True)
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info("explore cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s", project.project.id, intent.id, worker.name, cancelled, execute_ms)
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning("heartbeat lost during explore project=%s intent=%s worker=%s status=%s execute_ms=%s", project.project.id, intent.id, worker.name, lease.failure.status_code, execute_ms)
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                payload = parse_json_output(session.response_text(first))
                kind, description = validate_explore_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "explore parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    total_ms,
                    preview(first.stdout),
                )
                return _try_conclude_fallback(config, client, session, project, intent, export_yaml, lease, cancellation)
            if kind == "rejected":
                LOG.warning("explore rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s", project.project.id, intent.id, worker.name, execute_ms, total_ms)
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            return write_conclude_result(
                client,
                project.project.id,
                intent.id,
                worker.name,
                description,
                source="explore_execute",
                phase_ms=execute_ms,
                total_ms=total_ms,
            )
        if did_timeout(first):
            LOG.warning(
                "explore timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(first.stdout),
            )
            return _try_conclude_fallback(config, client, session, project, intent, export_yaml, lease, cancellation)
        LOG.warning(
            "explore agent failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stderr_preview=%s",
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
        LOG.exception("explore task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
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
    export_yaml: str,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not session.supports_conclude or not session.has_session:
        LOG.info(
            "conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project.project.id,
            intent.id,
            session.worker.name,
            session.supports_conclude,
            session.has_session,
        )
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning("conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s", project.project.id, intent.id, session.worker.name)
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"
    if cancellation.is_cancelled:
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "cancelled"
    if not project_allows_conclude_fallback(client, project.project.id, worker_name=session.worker.name, intent_id=intent.id):
        best_effort_release(client, project.project.id, intent.id, session.worker.name)
        return "failed"

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "explore_conclude.md"),
        {
            "graph_yaml": write_graph_snapshot_reference(
                session.workspace_path,
                export_yaml.strip(),
                phase="explore_conclude",
            ),
            "intent_id": intent.id,
            "intent_description": intent.description,
        },
    )
    LOG.info("starting conclude fallback project=%s intent=%s worker=%s", project.project.id, intent.id, session.worker.name)
    conclude_started = time.perf_counter()
    result = session.conclude(prompt, phase="explore_conclude", timeout=config.tasks.explore.conclude_timeout)
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
            "conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stderr_preview=%s",
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
        kind, description = validate_explore_payload(payload)
    except Exception as exc:
        LOG.warning(
            "conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s",
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
        description,
        source="explore_conclude",
        phase_ms=conclude_ms,
    )
