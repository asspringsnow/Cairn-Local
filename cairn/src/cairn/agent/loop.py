"""The built-in agent loop.

One loop = one Cairn task phase. It calls the model, runs tool calls, writes
results back, compacts the transcript when needed, and returns the model's final
text (which the dispatcher parses as the task contract JSON).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from cairn.agent.context import compact_messages, should_compact
from cairn.agent.messages import assistant_message, iter_tool_calls, tool_message, user_message
from cairn.agent.providers import BaseClient, ModelError, ModelResponse
from cairn.agent.session import SessionState, SessionStore
from cairn.agent.settings import AgentSettings
from cairn.agent.tools import ToolResult, Toolbox

EventSink = Callable[[dict[str, Any]], None]


class AgentCancelled(RuntimeError):
    pass


class AgentTimeout(RuntimeError):
    pass


class AgentLoop:
    def __init__(
        self,
        settings: AgentSettings,
        client: BaseClient,
        toolbox: Toolbox,
        session: SessionState,
        store: SessionStore,
        event_sink: EventSink,
        cancel_event: threading.Event,
    ):
        self.settings = settings
        self.client = client
        self.toolbox = toolbox
        self.session = session
        self.store = store
        self.emit = event_sink
        self.cancel_event = cancel_event
        self._last_prompt_tokens = 0
        self._allowed_tools: set[str] | None = None

    def _check(self, deadline: float | None) -> None:
        if self.cancel_event.is_set():
            raise AgentCancelled("cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise AgentTimeout("phase timeout")

    def _message_list(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.session.system_prompt}, *self.session.messages]

    def _save(self) -> None:
        self.store.save(self.session)

    def _call_model(
        self,
        messages: list[dict[str, Any]],
        *,
        tools_enabled: bool,
        allowed_tools: set[str] | None,
        deadline: float | None,
    ) -> ModelResponse:
        self._check(deadline)
        available = [
            spec
            for spec in self.toolbox.specs()
            if tools_enabled and (allowed_tools is None or spec["name"] in allowed_tools)
        ]
        timeout = self.settings.request_timeout
        if deadline is not None:
            timeout = max(5, min(timeout, int(deadline - time.monotonic())))
        try:
            return self.client.chat(messages, available, timeout=timeout, stream=True)
        except ModelError as exc:
            if not exc.context_overflow:
                raise
            self.emit({"type": "context_compaction", "reason": "provider reported context overflow"})
            messages = compact_messages(messages, self.client, self.settings, notice_only=True)
            replacement = [message for message in messages if message.get("role") != "system"]
            self.session.messages = replacement
            self._last_prompt_tokens = 0
            self._save()
            self._check(deadline)
            return self.client.chat(messages, available, timeout=timeout, stream=True)

    def run(
        self,
        prompt: str,
        *,
        tools_enabled: bool = True,
        allowed_tools: set[str] | None = None,
        timeout: int | None = None,
    ) -> str:
        if tools_enabled:
            self._allowed_tools = set(allowed_tools) if allowed_tools is not None else None
        else:
            self._allowed_tools = set()
        self.session.messages.append(user_message(prompt))
        self._save()
        deadline = time.monotonic() + timeout if timeout else None
        step = 0
        while True:
            step += 1
            self._check(deadline)
            messages = self._message_list()
            if should_compact(messages, self.settings, last_prompt_tokens=self._last_prompt_tokens):
                self.emit({"type": "context_compaction", "reason": "context budget"})
                compacted = compact_messages(messages, self.client, self.settings)
                self.session.messages = [message for message in compacted if message.get("role") != "system"]
                self._last_prompt_tokens = 0
                self._save()
                messages = self._message_list()

            self.emit({"type": "step", "step": step})
            response = self._call_model(messages, tools_enabled=tools_enabled, allowed_tools=allowed_tools, deadline=deadline)
            self.session.steps += 1
            self.session.record_usage(response.usage)
            self._last_prompt_tokens = int(response.usage.get("prompt_tokens") or 0)
            self.emit({"type": "usage", **response.usage})

            calls = response.tool_calls
            text = response.text.strip()
            if text:
                self.emit({"type": "assistant", "text": text[:4000]})

            if not calls:
                self.session.messages.append(assistant_message(response.text, thinking=response.thinking))
                self._save()
                return response.text

            self.session.messages.append(assistant_message(response.text, calls, thinking=response.thinking))
            self._save()
            self._execute_tool_calls(calls, deadline)


    def _emit_tool_call(self, call: dict[str, Any]) -> None:
        arguments = call.get("arguments")
        self.emit(
            {
                "type": "tool_call",
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )

    def _execute_tool(self, call: dict[str, Any], deadline: float | None) -> ToolResult:
        name = str(call.get("name") or "")
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return ToolResult(f"ERROR: tool '{name}' is not allowed in this phase", is_error=True)
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            return ToolResult("ERROR: tool arguments must be an object", is_error=True)
        if "__raw_arguments__" in arguments:
            return ToolResult(
                f"ERROR: tool arguments were not valid JSON: {arguments['__raw_arguments__'][:500]}",
                is_error=True,
            )
        if deadline is not None and time.monotonic() >= deadline:
            return ToolResult("ERROR: phase timeout reached before tool execution", is_error=True)
        if name == "bash" and deadline is not None:
            remaining = max(1, int(deadline - time.monotonic()))
            requested = arguments.get("timeout")
            try:
                requested_seconds = int(requested) if requested is not None else remaining
            except (TypeError, ValueError):
                requested_seconds = remaining
            arguments = {**arguments, "timeout": min(requested_seconds, remaining)}
        return self.toolbox.execute(name, arguments)

    def _emit_tool_result(self, call: dict[str, Any], result: ToolResult) -> None:
        preview = result.content[:2000]
        self.emit(
            {
                "type": "tool_result",
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "is_error": result.is_error,
                "output": preview,
                "truncated": len(result.content) > len(preview),
                "image_count": len(result.images),
            }
        )

    def _append_tool_result(self, call: dict[str, Any], result: ToolResult) -> None:
        self._emit_tool_result(call, result)
        self.session.messages.append(
            tool_message(
                str(call.get("id") or ""),
                str(call.get("name") or ""),
                result.content,
                is_error=result.is_error,
                images=result.images,
            )
        )
        self._save()

    def _execute_tool_calls(self, calls: list[dict[str, Any]], deadline: float | None) -> None:
        parallel = len(calls) > 1 and all(
            self.toolbox.is_parallel_safe(str(call.get("name") or "")) for call in calls
        )
        if not parallel:
            for call in calls:
                timed_out = deadline is not None and time.monotonic() >= deadline
                cancelled = self.cancel_event.is_set()
                if timed_out or cancelled:
                    result = ToolResult(
                        "ERROR: task cancelled or phase timeout reached before tool execution",
                        is_error=True,
                    )
                else:
                    result = self._execute_tool(call, deadline)
                self._emit_tool_call(call)
                self._append_tool_result(call, result)
            return

        # Read-only tools may run concurrently. Keep result messages in the
        # original assistant order so providers see a valid tool-call/result
        # transaction even though execution finished out of order.
        for call in calls:
            self._emit_tool_call(call)
        with ThreadPoolExecutor(max_workers=min(8, len(calls))) as pool:
            futures = [pool.submit(self._execute_tool, call, deadline) for call in calls]
            results = [future.result() for future in futures]
        for call, result in zip(calls, results):
            self._append_tool_result(call, result)
