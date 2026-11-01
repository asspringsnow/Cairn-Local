from __future__ import annotations

import json
import re
from typing import Any


FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    # strict=False lets the parser accept raw newlines/tabs inside JSON strings.
    # Models occasionally emit those in long descriptions; rejecting the whole
    # object would make the caller fall back to an inner fragment and report a
    # misleading contract error.
    decoder = json.JSONDecoder(strict=False)
    seen: set[str] = set()

    for candidate in _candidate_segments(text):
        segment = candidate.strip()
        if not segment or segment in seen:
            continue
        seen.add(segment)

        try:
            parsed = json.loads(segment, strict=False)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        starts = _object_start_positions(segment)
        for start in starts:
            try:
                parsed, _end = decoder.raw_decode(segment[start:])
            except json.JSONDecodeError:
                # Models occasionally stop one closing brace short. Try to
                # balance the first top-level object; only do this for the
                # first start so an inner fragment is never preferred over
                # the real envelope.
                if start != 0 and starts and starts[0] != start:
                    continue
                repaired = _close_unbalanced(segment[start:])
                try:
                    parsed = json.loads(repaired, strict=False)
                except json.JSONDecodeError:
                    continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("no JSON object found in output")


def _close_unbalanced(text: str) -> str:
    """Append missing closing braces/brackets and an unterminated string quote.

    This is deliberately conservative: it only appends characters, never
    removes or rewrites content, so a complete object is left untouched and a
    truncated object has a chance to parse.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if stack and ((char == "}" and stack[-1] == "{") or (char == "]" and stack[-1] == "[")):
                stack.pop()
    repaired = text
    if in_string:
        repaired += '"'
    repaired += "".join("}" if item == "{" else "]" for item in reversed(stack))
    return repaired


def _candidate_segments(text: str) -> list[str]:
    segments = [text.strip()]
    segments.extend(match.group(1).strip() for match in FENCED_BLOCK_RE.finditer(text))
    return segments


def _object_start_positions(text: str) -> list[int]:
    return [index for index, char in enumerate(text) if char == "{"]
