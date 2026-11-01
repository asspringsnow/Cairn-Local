"""Worker session used by every Cairn task.

A native session owns one ``cairn.agent.worker`` child process and reuses it for
the execute phase and the optional conclude phase, so tool results and the
model transcript survive across phases. Mock sessions keep the old process-based
test adapter.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from cairn.agent.settings import AgentSettings
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.runtime.agent_process import AgentProcess
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.runtime.workspace import WorkspaceManager
from cairn.dispatcher.workers.health import HealthResult
from cairn.dispatcher.workers.registry import get_driver

LOG = logging.getLogger(__name__)
COMMUNICATE_GRACE_SECONDS = 15


class WorkerSession:
    def __init__(
        self,
        config: DispatchConfig,
        workspace: WorkspaceManager,
        project_id: str,
        worker: WorkerConfig,
        agent_settings: AgentSettings,
        *,
        task_type: str,
        run_id: str | None = None,
    ):
        self.config = config
        self.project_id = project_id
        self.worker = worker
        self.agent_settings = agent_settings
        self.task_type = task_type
        self.run_id = run_id or f"{task_type}-{uuid.uuid4().hex[:12]}"
        self.workspace_path = Path(workspace.ensure_running(project_id))
        self.session_path = workspace.session_path(project_id, task_type, self.run_id)
        self._native = worker.type == "native"
        self._driver = None if self._native else get_driver(worker.type)
        self._driver_session: str | None = None
        self._agent: AgentProcess | None = None
        self._process: Any | None = None
        self._lease: HeartbeatLease | None = None
        self._cancellation: TaskCancellation | None = None
        if not self._native and self._driver is not None:
            self._driver_session = self._driver.prepare_session()

    def attach(self, lease: HeartbeatLease, cancellation: TaskCancellation) -> None:
        self._lease = lease
        self._cancellation = cancellation
        self._attach(self._process)
        self._attach(self._agent)

    def _attach(self, process: Any | None) -> None:
        if process is None:
            return
        if self._lease is not None:
            self._lease.attach_process(process)
        if self._cancellation is not None:
            self._cancellation.attach_process(process)

    @property
    def supports_conclude(self) -> bool:
        return True if self._native else bool(self._driver and self._driver.supports_conclude())

    @property
    def has_session(self) -> bool:
        if self._native:
            try:
                data = json.loads(self.session_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            return bool(data.get("messages"))
        return bool(self._driver_session)

    def ping(self, timeout: float) -> HealthResult:
        if not self._native:
            assert self._driver is not None
            return self._driver.check_health(self.worker, timeout=timeout)
        try:
            agent = self._ensure_agent()
            latency = agent.ping(timeout=min(int(timeout), 60))
            return HealthResult(ok=True, status=200, detail=f"model ping {latency:.2f}s")
        except Exception as exc:
            return HealthResult(ok=False, status=None, detail=str(exc)[:200])

    def run(self, prompt: str, *, phase: str, timeout: int, tools: bool = True, allowed_tools: set[str] | None = None) -> ProcessResult:
        if self._native:
            return self._run_native(prompt, phase=phase, timeout=timeout, tools=tools, allowed_tools=allowed_tools)
        return self._run_mock(prompt, phase=phase, timeout=timeout, tools=tools)

    def conclude(self, prompt: str, *, phase: str, timeout: int) -> ProcessResult:
        return self.run(prompt, phase=phase, timeout=timeout, tools=False)

    def response_text(self, result: ProcessResult) -> str:
        if self._native:
            return result.stdout
        assert self._driver is not None
        return self._driver.extract_response_text(result.stdout, result.stderr)

    def close(self) -> None:
        if self._agent is not None:
            self._agent.close()
            self._agent = None
        process = self._process
        if process is not None:
            try:
                process.kill()
            except Exception:
                pass
            self._process = None

    def _ensure_agent(self) -> AgentProcess:
        if self._agent is None:
            self._agent = AgentProcess(
                self.agent_settings,
                self.workspace_path,
                self.session_path,
                phase=self.task_type,
            )
        if not self._agent.alive:
            self._agent.start()
            self._attach(self._agent)
        return self._agent

    def _run_native(
        self,
        prompt: str,
        *,
        phase: str,
        timeout: int,
        tools: bool,
        allowed_tools: set[str] | None = None,
    ) -> ProcessResult:
        agent = self._ensure_agent()
        result = agent.run(prompt, tools_enabled=tools, allowed_tools=allowed_tools, timeout=timeout, phase=phase)
        return ProcessResult(
            returncode=result.returncode,
            stdout=result.text,
            stderr=result.error,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            cancel_reason=result.error or None,
        )

    def _run_mock(self, prompt: str, *, phase: str, timeout: int, tools: bool) -> ProcessResult:
        assert self._driver is not None
        if tools:
            execute = self._driver.build_execute(self.worker, prompt, self._driver_session)
            argv = execute.argv
            self._driver_session = execute.session
        else:
            if not self._driver_session:
                return ProcessResult(1, "", "mock session unavailable", cancelled=True, cancel_reason="no-session")
            argv = self._driver.build_conclude(self.worker, prompt, self._driver_session)
        process = LocalProcess(
            argv,
            cwd=str(self.workspace_path),
            env={**os.environ, **self.worker.env},
            timeout_seconds=timeout,
            term_grace_seconds=5,
        )
        self._process = process
        self._attach(process)
        process.start()
        try:
            result = process.communicate(timeout=timeout + COMMUNICATE_GRACE_SECONDS)
        finally:
            self._process = None
        return result
