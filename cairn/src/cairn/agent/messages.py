"""Provider-neutral transcript helpers.

Messages are plain dicts so a session file remains readable and versionable:

    {"role": "system", "content": "..."}
    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [
        {"id": "...", "name": "bash", "arguments": {"command": "id"}}
    ]}
    {"role": "tool", "tool_call_id": "...", "name": "bash", "content": "...", "is_error": false}

The loop keeps this neutral form; ``to_openai`` / ``to_anthropic`` convert only at
the model boundary.
"""

from __future__ import annotations

import json
from typing import Any


ToolCall = dict[str, Any]
Message = dict[str, Any]


def system_message(content: str) -> Message:
    return {"role": "system", "content": content}


def user_message(content: str) -> Message:
    return {"role": "user", "content": content}


def assistant_message(
    content: str,
    tool_calls: list[ToolCall] | None = None,
    thinking: list[dict[str, Any]] | None = None,
) -> Message:
    message: Message = {"role": "assistant", "content": content or ""}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if thinking:
        message["thinking"] = thinking
    return message


def tool_message(
    tool_call_id: str,
    name: str,
    content: str,
    *,
    is_error: bool = False,
    images: list[dict[str, Any]] | None = None,
) -> Message:
    message: Message = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": content,
        "is_error": bool(is_error),
    }
    if images:
        message["images"] = images
    return message


def assistant_text(message: Message) -> str:
    return str(message.get("content") or "")


def iter_tool_calls(message: Message) -> list[ToolCall]:
    calls = message.get("tool_calls")
    return list(calls) if isinstance(calls, list) else []


def message_text_for_estimate(message: Message) -> str:
    parts = [str(message.get("content") or "")]
    for _image in message.get("images") or []:
        parts.append("[image]")
    for call in iter_tool_calls(message):
        parts.append(str(call.get("name") or ""))
        parts.append(json.dumps(call.get("arguments") or {}, ensure_ascii=False, separators=(",", ":")))
    for block in message.get("thinking") or []:
        parts.append(str(block.get("thinking") or ""))
    return "\n".join(parts)


def _openai_tool_calls(calls: list[ToolCall]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(call.get("id") or f"call_{index}"),
            "type": "function",
            "function": {
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False, separators=(",", ":")),
            },
        }
        for index, call in enumerate(calls)
    ]


def to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    pending_images: list[dict[str, Any]] = []
    pending_names: set[str] = set()

    def flush_images() -> None:
        nonlocal pending_images, pending_names
        if not pending_images:
            return
        label = ", ".join(sorted(pending_names)) or "tool"
        converted.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Images returned by tool(s): {label}"},
                    *pending_images,
                ],
            }
        )
        pending_images = []
        pending_names = set()

    for message in messages:
        role = message.get("role")
        if role != "tool":
            flush_images()
        if role == "system":
            converted.append({"role": "system", "content": str(message.get("content") or "")})
        elif role == "user":
            converted.append({"role": "user", "content": str(message.get("content") or "")})
        elif role == "assistant":
            calls = iter_tool_calls(message)
            payload: dict[str, Any] = {
                "role": "assistant",
                "content": str(message.get("content") or "") or None,
            }
            if calls:
                payload["tool_calls"] = _openai_tool_calls(calls)
            converted.append(payload)
        elif role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id") or ""),
                    "content": str(message.get("content") or ""),
                }
            )
            for image in message.get("images") or []:
                mime = str(image.get("mime") or "")
                data = str(image.get("data") or "")
                if mime and data:
                    pending_images.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"},
                        }
                    )
                    pending_names.add(str(message.get("name") or "tool"))
    flush_images()
    return converted


def to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Return (system_text, anthropic_messages).

    Consecutive tool results are merged into one user message, which is what
    Anthropic requires after a batch of ``tool_use`` blocks.
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            converted.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for message in messages:
        role = message.get("role")
        if role == "system":
            text = str(message.get("content") or "").strip()
            if text:
                system_parts.append(text)
            continue

        flush_tool_results()

        if role == "user":
            block = {"type": "text", "text": str(message.get("content") or "")}
            if converted and converted[-1].get("role") == "user":
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            for thinking in message.get("thinking") or []:
                if not isinstance(thinking, dict):
                    continue
                if thinking.get("type") == "redacted_thinking":
                    data = str(thinking.get("data") or "")
                    if data:
                        blocks.append({"type": "redacted_thinking", "data": data})
                    continue
                text_thinking = str(thinking.get("thinking") or "")
                signature = str(thinking.get("signature") or "")
                block: dict[str, Any] = {"type": "thinking", "thinking": text_thinking}
                if signature:
                    block["signature"] = signature
                if text_thinking or signature:
                    blocks.append(block)
            text = str(message.get("content") or "")
            if text:
                blocks.append({"type": "text", "text": text})
            for call in iter_tool_calls(message):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(call.get("name") or ""),
                        "input": call.get("arguments") or {},
                    }
                )
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            converted.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            blocks: list[dict[str, Any]] = [{"type": "text", "text": str(message.get("content") or "")}]
            for image in message.get("images") or []:
                mime = str(image.get("mime") or "")
                data = str(image.get("data") or "")
                if mime and data:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": data},
                        }
                    )
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or ""),
                    "content": blocks,
                    "is_error": bool(message.get("is_error")),
                }
            )

    flush_tool_results()
    return "\n\n".join(system_parts), converted
