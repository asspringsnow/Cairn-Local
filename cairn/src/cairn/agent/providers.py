"""OpenAI-compatible and Anthropic model clients used by the built-in agent.

Both providers support non-streaming and SSE streaming. Streaming keeps long
model calls responsive and lets the dispatcher cancel/timeout the worker without
waiting for a complete response body.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

from cairn.agent.messages import Message, to_anthropic, to_openai
from cairn.agent.settings import AgentSettings


@dataclass(slots=True)
class ModelResponse:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


class ModelError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        context_overflow: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.context_overflow = context_overflow


def _context_overflow(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "context length",
        "context window",
        "maximum context",
        "too many tokens",
        "prompt is too long",
        "input is too long",
        "reduce the length",
        "exceeds the maximum",
        "token limit",
    )
    return any(marker in lowered for marker in markers)


def _normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    usage = usage or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = int(
        details.get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or usage.get("cache_read_input_tokens")
        or 0
    )
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "cached_tokens": cached,
    }


def _tool_call(index: int, raw_id: Any, name: Any, raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        arguments = raw_arguments
        raw_text = json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"))
    else:
        raw_text = str(raw_arguments or "{}")
        try:
            arguments = json.loads(raw_text) if raw_text.strip() else {}
        except json.JSONDecodeError:
            arguments = {"__raw_arguments__": raw_text}
    return {
        "id": str(raw_id or f"call_{index}"),
        "name": str(name or ""),
        "arguments": arguments,
        "raw_arguments": raw_text,
    }


def _iter_sse_data(response: requests.Response) -> Iterator[str]:
    for line in response.iter_lines(chunk_size=64, decode_unicode=True):
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        text = line.strip()
        if not text or text.startswith(":") or text.startswith("event:"):
            continue
        if text.startswith("data:"):
            text = text[5:].strip()
        if text:
            yield text


def parse_openai_sse(lines: Iterable[str]) -> ModelResponse:
    text_parts: list[str] = []
    tool_parts: dict[int, dict[str, str]] = {}
    stop_reason = "stop"
    usage: dict[str, int] = {}
    for data in lines:
        if data == "[DONE]":
            break
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid streaming JSON: {data[:500]}") from exc
        if isinstance(payload.get("error"), dict):
            raise ModelError(str(payload["error"].get("message") or payload["error"]))
        if payload.get("usage"):
            usage = _normalize_usage(payload["usage"])
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            for call in delta.get("tool_calls") or []:
                index = int(call.get("index", 0))
                entry = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if call.get("id"):
                    entry["id"] = str(call["id"])
                function = call.get("function") or {}
                if function.get("name"):
                    entry["name"] = str(function["name"])
                if function.get("arguments"):
                    entry["arguments"] += str(function["arguments"])
            if choice.get("finish_reason"):
                stop_reason = str(choice["finish_reason"])
    calls = [
        _tool_call(index, entry["id"], entry["name"], entry["arguments"])
        for index, entry in sorted(tool_parts.items())
    ]
    return ModelResponse(text="".join(text_parts), tool_calls=calls, stop_reason=stop_reason, usage=usage)


def parse_anthropic_sse(lines: Iterable[str]) -> ModelResponse:
    text_parts: list[str] = []
    tool_parts: dict[int, dict[str, Any]] = {}
    stop_reason = "end_turn"
    usage: dict[str, int] = {}
    for data in lines:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ModelError(f"invalid streaming JSON: {data[:500]}") from exc
        event_type = payload.get("type")
        if event_type == "error":
            error = payload.get("error") or {}
            raise ModelError(str(error.get("message") or error))
        if event_type == "message_start":
            usage = _normalize_usage((payload.get("message") or {}).get("usage"))
        elif event_type == "content_block_start":
            index = int(payload.get("index", 0))
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                tool_parts[index] = {
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "json": "",
                    "input": block.get("input") if isinstance(block.get("input"), dict) else None,
                }
            elif block.get("type") == "text" and block.get("text"):
                text_parts.append(str(block["text"]))
        elif event_type == "content_block_delta":
            index = int(payload.get("index", 0))
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                text_parts.append(str(delta.get("text") or ""))
            elif delta.get("type") == "input_json_delta":
                entry = tool_parts.setdefault(index, {"id": "", "name": "", "json": "", "input": None})
                entry["json"] += str(delta.get("partial_json") or "")
        elif event_type == "message_delta":
            delta = payload.get("delta") or {}
            if delta.get("stop_reason"):
                stop_reason = str(delta["stop_reason"])
            usage = {
                **usage,
                "completion_tokens": int(
                    (payload.get("usage") or {}).get("output_tokens") or usage.get("completion_tokens", 0)
                ),
            }
        elif event_type == "message_stop":
            break
    calls: list[dict[str, Any]] = []
    for index, entry in sorted(tool_parts.items()):
        raw = entry.get("json") or ""
        argument_value: Any = raw if raw.strip() else (entry.get("input") if entry.get("input") is not None else raw)
        calls.append(_tool_call(index, entry.get("id"), entry.get("name"), argument_value))
    return ModelResponse(text="".join(text_parts), tool_calls=calls, stop_reason=stop_reason, usage=usage)


class BaseClient:
    def __init__(self, settings: AgentSettings):
        self.settings = settings
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int, retries: int = 4) -> dict[str, Any]:
        last_error: ModelError | None = None
        attempts = max(1, retries)
        for attempt in range(attempts):
            try:
                response = self._session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(10, max(5, timeout)),
                )
            except requests.RequestException as exc:
                last_error = ModelError(f"request failed: {exc}", retryable=True)
                if attempt == attempts - 1:
                    raise last_error from exc
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ModelError("model endpoint returned non-JSON response", status_code=response.status_code) from exc
                body = response.text[:4000]
                error = ModelError(
                    f"HTTP {response.status_code}: {body}",
                    status_code=response.status_code,
                    retryable=response.status_code in (408, 409, 425, 429, 500, 502, 503, 504),
                    context_overflow=_context_overflow(body),
                )
                if not error.retryable or attempt == attempts - 1:
                    raise error
                last_error = error
            time.sleep(min(8, 2**attempt))
        raise last_error or ModelError("model request failed")

    def _open_stream(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int, retries: int = 4) -> requests.Response:
        last_error: ModelError | None = None
        attempts = max(1, retries)
        for attempt in range(attempts):
            try:
                response = self._session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(10, max(5, timeout)),
                    stream=True,
                )
            except requests.RequestException as exc:
                last_error = ModelError(f"request failed: {exc}", retryable=True)
                if attempt == attempts - 1:
                    raise last_error from exc
            else:
                if response.status_code < 400:
                    return response
                body = response.text[:4000]
                response.close()
                error = ModelError(
                    f"HTTP {response.status_code}: {body}",
                    status_code=response.status_code,
                    retryable=response.status_code in (408, 409, 425, 429, 500, 502, 503, 504),
                    context_overflow=_context_overflow(body),
                )
                if not error.retryable or attempt == attempts - 1:
                    raise error
                last_error = error
            time.sleep(min(8, 2**attempt))
        raise last_error or ModelError("model request failed")

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        retries: int = 4,
        stream: bool = False,
    ) -> ModelResponse:
        raise NotImplementedError

    def ping(self, timeout: int = 30) -> float:
        started = time.monotonic()
        self.chat(
            [{"role": "user", "content": "Reply with the single word: pong"}],
            [],
            max_tokens=8,
            temperature=0.0,
            timeout=timeout,
            retries=1,
            stream=False,
        )
        return time.monotonic() - started


def _base(settings: AgentSettings) -> str:
    return settings.base_url.rstrip("/")


def openai_url(settings: AgentSettings) -> str:
    url = _base(settings)
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def anthropic_url(settings: AgentSettings) -> str:
    url = _base(settings)
    if url.endswith("/v1/messages"):
        return url
    if url.endswith("/v1"):
        return f"{url}/messages"
    return f"{url}/v1/messages"


def _text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return "" if value is None else str(value)


class OpenAIClient(BaseClient):
    def _payload(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        temperature: float | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": to_openai(messages),
            "max_tokens": max_tokens or self.settings.max_tokens,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        retries: int = 4,
        stream: bool = False,
    ) -> ModelResponse:
        payload = self._payload(messages, tools, max_tokens=max_tokens, temperature=temperature, stream=stream)
        headers = {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}
        url = openai_url(self.settings)
        timeout_value = timeout or self.settings.request_timeout
        if stream:
            try:
                response = self._open_stream(url, headers, payload, timeout_value, retries=retries)
                try:
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        # Some gateways return a normal JSON body despite
                        # stream=true. Try it first; if the body is actually
                        # SSE with a missing header, json() raises and we fall
                        # through to the SSE parser below.
                        try:
                            return self._parse_non_stream(response.json())
                        except (ValueError, KeyError, TypeError):
                            pass
                    return parse_openai_sse(_iter_sse_data(response))
                finally:
                    response.close()
            except (ModelError, requests.RequestException) as exc:
                # Some OpenAI-compatible servers reject stream_options or
                # streaming entirely. Retry once as a normal request.
                if isinstance(exc, ModelError) and (exc.context_overflow or exc.status_code in (401, 403)):
                    raise
                payload.pop("stream_options", None)
                payload["stream"] = False
        request_timeout = max(5, timeout_value // 2) if stream else timeout_value
        return self._parse_non_stream(self._post(url, headers, payload, request_timeout, retries=retries))

    @staticmethod
    def _parse_non_stream(data: dict[str, Any]) -> ModelResponse:
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"unexpected OpenAI response: {str(data)[:1000]}") from exc
        calls: list[dict[str, Any]] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") or {}
            calls.append(_tool_call(index, call.get("id"), function.get("name"), function.get("arguments")))
        return ModelResponse(
            text=_text_content(message.get("content")),
            tool_calls=calls,
            stop_reason=str(choice.get("finish_reason") or "stop"),
            usage=_normalize_usage(data.get("usage")),
            raw=data,
        )


class AnthropicClient(BaseClient):
    def _payload(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        temperature: float | None,
        stream: bool,
    ) -> dict[str, Any]:
        system_text, converted = to_anthropic(messages)
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "messages": converted,
            "stream": stream,
        }
        if system_text:
            payload["system"] = system_text
        if tools:
            payload["tools"] = [
                {"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}
                for tool in tools
            ]
        return payload

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
        retries: int = 4,
        stream: bool = False,
    ) -> ModelResponse:
        payload = self._payload(messages, tools, max_tokens=max_tokens, temperature=temperature, stream=stream)
        headers = {
            "x-api-key": self.settings.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = anthropic_url(self.settings)
        timeout_value = timeout or self.settings.request_timeout
        if stream:
            try:
                response = self._open_stream(url, headers, payload, timeout_value, retries=retries)
                try:
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        # Anthropic-compatible gateways may ignore stream=true
                        # and return a plain JSON message instead.
                        try:
                            return self._parse_non_stream(response.json())
                        except (ValueError, KeyError, TypeError):
                            pass
                    return parse_anthropic_sse(_iter_sse_data(response))
                finally:
                    response.close()
            except (ModelError, requests.RequestException) as exc:
                if isinstance(exc, ModelError) and (exc.context_overflow or exc.status_code in (401, 403)):
                    raise
                payload["stream"] = False
        request_timeout = max(5, timeout_value // 2) if stream else timeout_value
        return self._parse_non_stream(self._post(url, headers, payload, request_timeout, retries=retries))

    @staticmethod
    def _parse_non_stream(data: dict[str, Any]) -> ModelResponse:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ModelError(f"unexpected Anthropic response: {str(data)[:1000]}")
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                calls.append(_tool_call(index, block.get("id"), block.get("name"), block.get("input") or {}))
        return ModelResponse(
            text="\n".join(part for part in texts if part),
            tool_calls=calls,
            stop_reason=str(data.get("stop_reason") or "end_turn"),
            usage=_normalize_usage(data.get("usage")),
            raw=data,
        )


def build_client(settings: AgentSettings) -> BaseClient:
    if settings.provider == "anthropic":
        return AnthropicClient(settings)
    return OpenAIClient(settings)
