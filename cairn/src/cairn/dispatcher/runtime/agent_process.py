"""Dispatcher-side manager for a ``cairn.agent.worker`` child process.

The dispatcher never runs the model loop in its own threads. Each task spawns
this process, talks JSON Lines over stdin/stdout, and can kill the whole process
group on timeout or project stop, exactly like the old external CLI workers.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cairn
from cairn.agent.prompt import build_system_prompt
from cairn.agent.settings import AgentSettings

LOG = logging.getLogger(__name__)
READY_TIMEOUT_SECONDS = 30


@dataclass(slots=True)
class AgentProcessResult:
    text: str = ""
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""
    kind: str = ""
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.cancelled and not self.error


class AgentProcess:
    def __init__(
        self,
        settings: AgentSettings,
        workspace: Path,
        session_path: Path,
        *,
        phase: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.settings = settings
        self.workspace = workspace
        self.session_path = session_path
        self.phase = phase
        self.on_event = on_event or (lambda _event: None)
        self._process: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._cancel_reason = ""

    # ------------------------------------------------------------------ lifecycle

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def start(self) -> None:
        if self.alive:
            return
        self._close_process()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        src_root = Path(cairn.__file__).resolve().parent.parent
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(src_root) + (os.pathsep + existing if existing else "")
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY"):
            env.pop(key, None)

        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._process = subprocess.Popen(
            [sys.executable, "-u", "-m", "cairn.agent.worker"],
            cwd=str(self.workspace),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        process = self._process
        event_queue: queue.Queue[str | None] = queue.Queue()
        self._queue = event_queue
        self._stdout_thread = threading.Thread(target=self._read_stdout, args=(process, event_queue), daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, args=(process,), daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        init = {
            "op": "init",
            "settings": self.settings.model_dump(),
            "workspace": str(self.workspace),
            "session_path": str(self.session_path),
            "system_prompt": build_system_prompt(str(self.workspace)),
            "phase": self.phase,
        }
        reply = self._request(init, timeout=READY_TIMEOUT_SECONDS, terminal={"ready"})
        if reply.get("type") == "error":
            raise RuntimeError(f"agent worker init failed: {reply.get('message')}")

    def close(self) -> None:
        if self.alive:
            try:
                self._send({"op": "shutdown"})
                self._process.wait(timeout=2)
            except Exception:
                pass
        self._close_process()

    def restart(self) -> None:
        self._close_process()
        self._cancelled = False
        self._cancel_reason = ""
        self.start()

    def cancel(self, reason: str = "cancelled") -> None:
        self._cancelled = True
        self._cancel_reason = reason
        self.kill()

    def kill(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        LOG.info("killing agent worker pid=%s reason=%s", process.pid, self._cancel_reason or "kill")
        if os.name == "nt":
            self._kill_tree(process)
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    # ------------------------------------------------------------------ operations

    def run(
        self,
        prompt: str,
        *,
        tools_enabled: bool,
        allowed_tools: set[str] | None = None,
        timeout: int,
        phase: str,
    ) -> AgentProcessResult:
        if self._cancelled:
            return AgentProcessResult(cancelled=True, kind="cancelled", error=self._cancel_reason or "cancelled")
        LOG.debug("agent run phase=%s tools=%s allowed=%s timeout=%ss", phase, tools_enabled, allowed_tools, timeout)
        try:
            self.start()
        except Exception as exc:
            return AgentProcessResult(error=str(exc), kind="internal", returncode=1)
        reply = self._request(
            {
                "op": "run",
                "prompt": prompt,
                "tools": tools_enabled,
                "allowed_tools": sorted(allowed_tools) if allowed_tools is not None else None,
                "timeout": timeout,
                "phase": phase,
            },
            timeout=timeout + 15,
            terminal={"result", "error"},
        )
        if reply.get("type") == "result":
            return AgentProcessResult(text=str(reply.get("text") or ""), returncode=0)
        kind = str(reply.get("kind") or "")
        message = str(reply.get("message") or "agent worker failed")
        if kind == "cancelled" or self._cancelled:
            return AgentProcessResult(cancelled=True, kind="cancelled", error=message, returncode=1)
        if kind == "timeout":
            return AgentProcessResult(timed_out=True, kind="timeout", error=message, returncode=124)
        return AgentProcessResult(error=message, kind=kind or "model", returncode=1)

    def ping(self, timeout: int = 30) -> float:
        self.start()
        reply = self._request({"op": "ping"}, timeout=timeout + 5, terminal={"pong", "error"})
        if reply.get("type") != "pong":
            raise RuntimeError(str(reply.get("message") or "model ping failed"))
        return float(reply.get("latency") or 0.0)

    # ------------------------------------------------------------------ internals

    def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("agent worker is not running")
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _request(self, payload: dict[str, Any], *, timeout: int, terminal: set[str]) -> dict[str, Any]:
        with self._lock:
            self._send(payload)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.kill()
                    return {"type": "error", "kind": "timeout", "message": f"agent worker timed out after {timeout}s"}
                try:
                    line = self._queue.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    if self._process is None or self._process.poll() is not None:
                        return {"type": "error", "kind": "internal", "message": "agent worker exited unexpectedly"}
                    continue
                if line is None:
                    return {"type": "error", "kind": "internal", "message": "agent worker stdout closed"}
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("agent worker emitted invalid JSON: %s", line[:500])
                    continue
                event_type = str(event.get("type") or "")
                if event_type in terminal:
                    return event
                self.on_event(event)

    def _read_stdout(self, process: subprocess.Popen[str], event_queue: queue.Queue[str | None]) -> None:
        if process.stdout is None:
            event_queue.put(None)
            return
        try:
            for line in process.stdout:
                event_queue.put(line)
        finally:
            # Always signal this reader's own queue. A later restart replaces
            # self._queue; putting None on the new queue would falsely abort it.
            event_queue.put(None)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            text = line.rstrip()
            if text:
                LOG.info("agent pid=%s %s", process.pid, text)

    @staticmethod
    def _kill_tree(process: subprocess.Popen[str]) -> None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass

    def _close_process(self) -> None:
        process = self._process
        if process is not None:
            try:
                if process.poll() is None:
                    if os.name == "nt":
                        self._kill_tree(process)
                    else:
                        process.kill()
            except OSError:
                pass
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass
        self._process = None
