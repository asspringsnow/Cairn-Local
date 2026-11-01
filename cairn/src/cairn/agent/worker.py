"""Standalone worker process for the built-in agent.

Protocol (one JSON object per line):

    dispatcher -> worker: {"op":"init", "settings":{...}, "workspace":"...",
                           "session_path":"...", "system_prompt":"...", "phase":"..."}
    worker -> dispatcher: {"type":"ready"}
    dispatcher -> worker: {"op":"run", "prompt":"...", "tools":true, "timeout":600}
    worker -> dispatcher: {"type":"step" | "assistant" | "tool_call" | "tool_result" |
                           "usage" | "context_compaction" | "result" | "error", ...}
    dispatcher -> worker: {"op":"ping"}
    worker -> dispatcher: {"type":"pong", "latency": 0.42}
    dispatcher -> worker: {"op":"shutdown"}

stdout is reserved for protocol lines. All diagnostics go to stderr.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from cairn.agent.loop import AgentCancelled, AgentLoop, AgentTimeout
from cairn.agent.prompt import build_system_prompt
from cairn.agent.providers import ModelError, build_client
from cairn.agent.session import SessionStore
from cairn.agent.settings import AgentSettings
from cairn.agent.tools import Toolbox


_lock = threading.Lock()
_cancel = threading.Event()


def emit(payload: dict[str, Any]) -> None:
    with _lock:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def log(message: str) -> None:
    sys.stderr.write(f"[cairn-agent] {message}\n")
    sys.stderr.flush()


def _preview(value: Any, limit: int = 500) -> Any:
    if isinstance(value, dict):
        return {key: _preview(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_preview(item, limit) for item in value[:20]]
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def event_sink(event: dict[str, Any]) -> None:
    payload = {"type": event.get("type"), **{key: value for key, value in event.items() if key != "type"}}
    if payload.get("type") == "tool_call":
        payload["arguments"] = _preview(payload.get("arguments"))
    emit(payload)


def _install_signals() -> None:
    def handler(_signum, _frame):
        _cancel.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def main() -> int:
    _install_signals()
    line = sys.stdin.readline()
    if not line:
        return 1
    try:
        init = json.loads(line)
    except json.JSONDecodeError as exc:
        emit({"type": "error", "message": f"invalid init: {exc}"})
        return 1
    if init.get("op") != "init":
        emit({"type": "error", "message": "first operation must be init"})
        return 1

    settings = AgentSettings.model_validate(init.get("settings") or {})
    workspace = Path(str(init.get("workspace") or ".")).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    session_path = Path(str(init.get("session_path") or workspace / ".cairn" / "session.json")).expanduser()
    system_prompt = str(init.get("system_prompt") or build_system_prompt(str(workspace)))
    phase = str(init.get("phase") or "task")

    store = SessionStore(session_path)
    session = store.load(system_prompt)
    toolbox = Toolbox(workspace, cancel_event=_cancel)
    client = build_client(settings)
    emit({"type": "ready", "phase": phase, "workspace": str(workspace)})

    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                emit({"type": "error", "message": f"invalid request: {exc}"})
                continue
            op = request.get("op")
            if op == "shutdown":
                break
            if op == "ping":
                try:
                    latency = client.ping(timeout=min(settings.request_timeout, 30))
                    emit({"type": "pong", "latency": round(latency, 2)})
                except Exception as exc:
                    emit({"type": "error", "message": f"model ping failed: {exc}"})
                continue
            if op != "run":
                emit({"type": "error", "message": f"unknown operation: {op}"})
                continue

            _cancel.clear()
            prompt = str(request.get("prompt") or "")
            tools_enabled = bool(request.get("tools", True))
            allowed_raw = request.get("allowed_tools")
            allowed_tools = {str(name) for name in allowed_raw} if isinstance(allowed_raw, list) else None
            timeout = request.get("timeout")
            timeout_value = int(timeout) if timeout else None
            loop = AgentLoop(settings, client, toolbox, session, store, event_sink, _cancel)
            try:
                text = loop.run(
                    prompt,
                    tools_enabled=tools_enabled,
                    allowed_tools=allowed_tools,
                    timeout=timeout_value,
                )
                emit(
                    {
                        "type": "result",
                        "text": text,
                        "usage": session.usage,
                        "steps": session.steps,
                    }
                )
            except AgentCancelled:
                emit({"type": "error", "kind": "cancelled", "message": "cancelled"})
            except AgentTimeout as exc:
                emit({"type": "error", "kind": "timeout", "message": str(exc)})
            except ModelError as exc:
                emit(
                    {
                        "type": "error",
                        "kind": "model",
                        "message": str(exc),
                        "status_code": exc.status_code,
                        "retryable": exc.retryable,
                    }
                )
            except Exception as exc:
                log(traceback.format_exc())
                emit({"type": "error", "kind": "internal", "message": str(exc) or "internal error"})
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
