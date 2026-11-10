"""Structured, read-only graph queries for Cairn's built-in reason phase.

The reason agent must not read the workspace or a YAML file.  The dispatcher
writes a small JSON snapshot into ``.cairn/graph.json``; the ``graph`` tool
answers bounded queries against that snapshot.  Keeping this in one module
means the tool and the snapshot writer use exactly the same schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GRAPH_RELATIVE_PATH = ".cairn/graph.json"
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_NODE_TEXT = 8000
MAX_SUMMARY_TEXT = 1200
MAX_OVERVIEW_FACTS = 8
MAX_OVERVIEW_HINTS = 5


def write_snapshot(workspace: Path, snapshot: dict[str, Any]) -> Path:
    path = Path(workspace) / GRAPH_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def load_snapshot(workspace: Path) -> dict[str, Any]:
    path = Path(workspace) / GRAPH_RELATIVE_PATH
    if not path.is_file():
        raise ValueError(f"graph snapshot not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read graph snapshot: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("graph snapshot must be a JSON object")
    return data


def _clip(value: Any, limit: int = MAX_SUMMARY_TEXT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def _facts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in snapshot.get("facts") or [] if isinstance(item, dict)]


def _intents(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in snapshot.get("intents") or [] if isinstance(item, dict)]


def _hints(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in snapshot.get("hints") or [] if isinstance(item, dict)]


def _index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item.get("id") or "")
        if item_id:
            result[item_id] = item
    return result


def _fact_summary(fact: dict[str, Any], limit: int = 500) -> dict[str, Any]:
    return {"type": "fact", "id": str(fact.get("id") or ""), "description": _clip(fact.get("description"), limit)}


def _intent_summary(intent: dict[str, Any], limit: int = 800) -> dict[str, Any]:
    return {
        "type": "intent",
        "id": str(intent.get("id") or ""),
        "from": [str(value) for value in intent.get("from") or []],
        "to": intent.get("to"),
        "worker": intent.get("worker"),
        "description": _clip(intent.get("description"), limit),
    }


def _hint_summary(hint: dict[str, Any], limit: int = 500) -> dict[str, Any]:
    return {"type": "hint", "id": str(hint.get("id") or ""), "content": _clip(hint.get("content"), limit)}


def _limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_LIMIT, parsed))


def _snippet(text: Any, needle: str, limit: int = 400) -> str:
    source = str(text or "")
    position = source.lower().find(needle)
    if position < 0:
        return _clip(source, limit)
    start = max(0, position - limit // 3)
    end = min(len(source), start + limit)
    prefix = "…" if start else ""
    suffix = "…" if end < len(source) else ""
    return prefix + source[start:end] + suffix


def _overview(snapshot: dict[str, Any], limit: int) -> dict[str, Any]:
    facts = _facts(snapshot)
    intents = _intents(snapshot)
    hints = _hints(snapshot)
    fact_index = _index(facts)
    open_intents = [intent for intent in intents if not intent.get("to")]
    recent = [fact for fact in facts if str(fact.get("id") or "") not in {"origin", "goal"}]
    goal = fact_index.get("goal")
    origin = fact_index.get("origin")
    return {
        "query": "overview",
        "project": snapshot.get("project") or {},
        "counts": {
            "facts": len(facts),
            "intents": len(intents),
            "open_intents": len(open_intents),
            "hints": len(hints),
        },
        "goal": _clip(goal.get("description"), MAX_SUMMARY_TEXT) if goal else None,
        "origin": _clip(origin.get("description"), MAX_SUMMARY_TEXT) if origin else None,
        "open_intents": [_intent_summary(intent) for intent in open_intents[:limit]],
        "open_intents_truncated": len(open_intents) > limit,
        "recent_facts": [_fact_summary(fact, 500) for fact in recent[-MAX_OVERVIEW_FACTS:]],
        "recent_facts_truncated": len(recent) > MAX_OVERVIEW_FACTS,
        "hints": [_hint_summary(hint) for hint in hints[-MAX_OVERVIEW_HINTS:]],
        "hints_truncated": len(hints) > MAX_OVERVIEW_HINTS,
        "hint": (
            "Call graph(query=\"frontier\") for all open intents, "
            "graph(query=\"get\", id=...) for full node text, "
            "graph(query=\"search\", text=...) or graph(query=\"neighbors\", id=...) for relationships."
        ),
    }


def _frontier(snapshot: dict[str, Any], limit: int) -> dict[str, Any]:
    intents = [intent for intent in _intents(snapshot) if not intent.get("to")]
    return {
        "query": "frontier",
        "count": len(intents),
        "truncated": len(intents) > limit,
        "open_intents": [_intent_summary(intent, 2000) for intent in intents[:limit]],
    }


def _get(snapshot: dict[str, Any], node_id: str | None, limit: int) -> dict[str, Any]:
    if not node_id:
        raise ValueError("id is required for graph(query=\"get\")")
    facts = _index(_facts(snapshot))
    intents = _index(_intents(snapshot))
    hints = _index(_hints(snapshot))
    if node_id in facts:
        fact = facts[node_id]
        outgoing = [
            _intent_summary(intent, 300)
            for intent in intents.values()
            if node_id in [str(value) for value in intent.get("from") or []]
        ]
        incoming = [
            _intent_summary(intent, 300)
            for intent in intents.values()
            if str(intent.get("to") or "") == node_id
        ]
        return {
            "query": "get",
            "type": "fact",
            "id": node_id,
            "description": _clip(fact.get("description"), MAX_NODE_TEXT),
            "outgoing_intents": outgoing[:limit],
            "incoming_intents": incoming[:limit],
            "outgoing_truncated": len(outgoing) > limit,
            "incoming_truncated": len(incoming) > limit,
        }
    if node_id in intents:
        intent = intents[node_id]
        source_facts = [_fact_summary(facts[value], 500) for value in intent.get("from") or [] if value in facts]
        target = facts.get(str(intent.get("to") or ""))
        return {
            "query": "get",
            "type": "intent",
            "id": node_id,
            "from": [str(value) for value in intent.get("from") or []],
            "to": intent.get("to"),
            "worker": intent.get("worker"),
            "description": _clip(intent.get("description"), MAX_NODE_TEXT),
            "source_facts": source_facts[:limit],
            "source_facts_truncated": len(source_facts) > limit,
            "target_fact": _fact_summary(target, 1000) if target else None,
        }
    if node_id in hints:
        hint = hints[node_id]
        return {
            "query": "get",
            "type": "hint",
            "id": node_id,
            "content": _clip(hint.get("content"), MAX_NODE_TEXT),
            "creator": hint.get("creator"),
            "created_at": hint.get("created_at"),
        }
    raise ValueError(f"unknown graph node: {node_id}")


def _search(snapshot: dict[str, Any], text: str | None, limit: int) -> dict[str, Any]:
    needle = str(text or "").strip().lower()
    if not needle:
        raise ValueError("text is required for graph(query=\"search\")")
    results: list[dict[str, Any]] = []
    for fact in _facts(snapshot):
        haystack = f"{fact.get('id') or ''} {fact.get('description') or ''}".lower()
        if needle in haystack:
            results.append({"type": "fact", "id": fact.get("id"), "snippet": _snippet(fact.get("description"), needle)})
    for intent in _intents(snapshot):
        haystack = f"{intent.get('id') or ''} {intent.get('description') or ''}".lower()
        if needle in haystack:
            results.append(
                {
                    "type": "intent",
                    "id": intent.get("id"),
                    "to": intent.get("to"),
                    "from": intent.get("from") or [],
                    "snippet": _snippet(intent.get("description"), needle),
                }
            )
    for hint in _hints(snapshot):
        haystack = f"{hint.get('id') or ''} {hint.get('content') or ''}".lower()
        if needle in haystack:
            results.append({"type": "hint", "id": hint.get("id"), "snippet": _snippet(hint.get("content"), needle)})
    return {
        "query": "search",
        "text": text,
        "count": len(results),
        "truncated": len(results) > limit,
        "results": results[:limit],
    }


def _neighbors(snapshot: dict[str, Any], node_id: str | None, depth: int, limit: int) -> dict[str, Any]:
    if not node_id:
        raise ValueError("id is required for graph(query=\"neighbors\")")
    facts = _index(_facts(snapshot))
    intents = _index(_intents(snapshot))
    if node_id not in facts and node_id not in intents:
        raise ValueError(f"unknown graph node: {node_id}")
    depth = max(1, min(2, depth))
    seen_facts: set[str] = set()
    seen_intents: set[str] = set()
    queue: list[tuple[str, int]] = [(node_id, 0)]
    while queue and len(seen_facts) + len(seen_intents) < limit:
        current, distance = queue.pop(0)
        if current in facts:
            if current not in seen_facts:
                seen_facts.add(current)
            if distance >= depth:
                continue
            for intent_id, intent in intents.items():
                if len(seen_facts) + len(seen_intents) >= limit:
                    break
                if current in [str(value) for value in intent.get("from") or []] or str(intent.get("to") or "") == current:
                    if intent_id not in seen_intents:
                        seen_intents.add(intent_id)
                        queue.append((intent_id, distance + 1))
        elif current in intents:
            if current not in seen_intents:
                seen_intents.add(current)
            if distance >= depth:
                continue
            related = [str(value) for value in intents[current].get("from") or []]
            target = str(intents[current].get("to") or "")
            if target:
                related.append(target)
            for fact_id in related:
                if len(seen_facts) + len(seen_intents) >= limit:
                    break
                if fact_id in facts and fact_id not in seen_facts:
                    seen_facts.add(fact_id)
                    queue.append((fact_id, distance + 1))
    return {
        "query": "neighbors",
        "id": node_id,
        "depth": depth,
        "truncated": len(seen_facts) + len(seen_intents) >= limit,
        "facts": [_fact_summary(facts[fact_id], 500) for fact_id in sorted(seen_facts)][:limit],
        "intents": [_intent_summary(intents[intent_id], 500) for intent_id in sorted(seen_intents)][:limit],
    }


def query_graph(
    snapshot: dict[str, Any],
    query: str,
    *,
    node_id: str | None = None,
    text: str | None = None,
    depth: int = 1,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    name = str(query or "").strip().lower()
    bounded = _limit(limit)
    if name == "overview":
        return _overview(snapshot, bounded)
    if name == "frontier":
        return _frontier(snapshot, bounded)
    if name == "get":
        return _get(snapshot, node_id, bounded)
    if name == "search":
        return _search(snapshot, text, bounded)
    if name == "neighbors":
        return _neighbors(snapshot, node_id, depth, bounded)
    raise ValueError(f"unknown graph query: {query!r}; use overview/frontier/get/search/neighbors")
