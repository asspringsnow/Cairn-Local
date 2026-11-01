"""Durable per-task session for the built-in agent.

The session file is written after every completed step. If the dispatcher kills
the agent process on timeout, a new worker process can load the same file and
run the conclude phase with the work that was already confirmed.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairn.agent.messages import Message

SESSION_VERSION = 1


@dataclass
class SessionState:
    system_prompt: str
    messages: list[Message] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})
    steps: int = 0

    def record_usage(self, usage: dict[str, int]) -> None:
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            self.usage[key] = self.usage.get(key, 0) + int(usage.get(key) or 0)


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, default_system_prompt: str) -> SessionState:
        if not self.path.exists():
            return SessionState(system_prompt=default_system_prompt)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return SessionState(system_prompt=default_system_prompt)
        if not isinstance(data, dict) or data.get("version") != SESSION_VERSION:
            return SessionState(system_prompt=default_system_prompt)
        messages = data.get("messages")
        usage = data.get("usage")
        return SessionState(
            system_prompt=str(data.get("system_prompt") or default_system_prompt),
            messages=messages if isinstance(messages, list) else [],
            usage=usage if isinstance(usage, dict) else {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
            steps=int(data.get("steps") or 0),
        )

    def save(self, state: SessionState) -> None:
        payload = {
            "version": SESSION_VERSION,
            "system_prompt": state.system_prompt,
            "messages": state.messages,
            "usage": state.usage,
            "steps": state.steps,
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(prefix=".cairn-session-", suffix=".json.tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
