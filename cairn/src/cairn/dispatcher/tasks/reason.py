from __future__ import annotations

import logging
import time

from cairn.agent.settings import AgentSettings
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_reason_payload
from cairn.dispatcher.prompting import format_fact_ids, format_open_intents, load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.workspace import WorkspaceManager
from cairn.dispatcher.tasks.common import (
    best_effort_release_reason,
    cancel_reason,
    did_timeout,
    preview,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from cairn.dispatcher.tasks.session import WorkerSession
from cairn.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_reason_task(
    config: DispatchConfig,
    client: CairnClient,
    workspace: WorkspaceManager,
    agent_settings: AgentSettings,
    project: ProjectDetail,
    export_yaml: str,
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
        task_type="reason",
    )
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval)
    lease.start()
    session.attach(lease, cancellation)
    try:
        if task_healthcheck_enabled(config):
            LOG.info("checking agent model project=%s worker=%s timeout=%ss", project.project.id, worker.name, config.runtime.healthcheck_timeout)
            health = session.ping(config.runtime.healthcheck_timeout)
            if cancellation.is_cancelled:
                return "cancelled"
            if lease.failure is not None:
                return "failed"
            if not health.ok:
                LOG.warning("agent model unhealthy project=%s worker=%s status=%s detail=%s", project.project.id, worker.name, health.status, health.detail)
                return "unhealthy"

        open_intents = [
            {"id": intent.id, "from": intent.from_, "description": intent.description, "worker": intent.worker}
            for intent in project.intents
            if intent.to is None
        ]
        allowed_fact_ids = [fact.id for fact in project.facts if fact.id != "goal"]
        graph_yaml = export_yaml.strip()
        # Keep a debug snapshot for inspection, but inline the graph into the
        # reason prompt: reason has no tools and must not read the workspace.
        write_graph_snapshot_reference(session.workspace_path, graph_yaml, phase="reason_execute")
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "reason.md"),
            {
                "graph_yaml": graph_yaml,
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "open_intents": format_open_intents(open_intents),
                "max_intents": str(config.tasks.reason.max_intents),
            },
        )

        execute_started = time.perf_counter()
        result = session.run(
            prompt,
            phase="reason_execute",
            timeout=config.tasks.reason.timeout,
            tools=False,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info("reason cancelled project=%s worker=%s reason=%s execute_ms=%s", project.project.id, worker.name, cancelled, execute_ms)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning("heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s", project.project.id, worker.name, lease.failure.status_code, execute_ms)
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "reason agent failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        try:
            payload = parse_json_output(session.response_text(result))
            kind, data = validate_reason_payload(
                payload,
                open_intents_empty=not open_intents,
                max_intents=config.tasks.reason.max_intents,
            )
        except Exception as exc:
            LOG.warning(
                "reason parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "failed"
        if kind == "rejected":
            LOG.warning("reason rejected project=%s worker=%s execute_ms=%s total_ms=%s", project.project.id, worker.name, execute_ms, total_ms)
            return "rejected"
        if kind == "complete":
            response = client.complete(project.project.id, data["from"], data["description"], worker.name)
            if response.status_code == 403:
                LOG.info("project became inactive during reason complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if not response.ok:
                LOG.warning("reason complete write failed project=%s worker=%s status=%s body=%s", project.project.id, worker.name, response.status_code, response.text)
                return "failed"
            LOG.info(
                "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                data["from"],
                execute_ms,
                total_ms,
            )
            return "success"
        if kind == "intents":
            created = 0
            for intent_data in data:
                response = client.create_intent(project.project.id, intent_data["from"], intent_data["description"], worker.name)
                if response.status_code == 403:
                    LOG.info("project became inactive during reason intent create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                    return "success"
                if response.status_code == 409:
                    LOG.info("reason intent lost race project=%s worker=%s from=%s", project.project.id, worker.name, intent_data["from"])
                    continue
                if not response.ok:
                    LOG.warning("reason intent write failed project=%s worker=%s status=%s body=%s", project.project.id, worker.name, response.status_code, response.text)
                    continue
                created += 1
                LOG.info("reason created intent project=%s worker=%s from=%s description=%s", project.project.id, worker.name, intent_data["from"], intent_data["description"])
            LOG.info(
                "reason finished project=%s worker=%s created_intents=%s/%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                len(data),
                execute_ms,
                total_ms,
            )
            return "success" if created else "failed"
        LOG.info("reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s", project.project.id, worker.name, execute_ms, total_ms)
        return "success"
    except Exception:
        LOG.exception("reason task crashed project=%s worker=%s", project.project.id, worker.name)
        return "failed"
    finally:
        lease.stop()
        session.close()
        best_effort_release_reason(client, project.project.id, worker.name)
