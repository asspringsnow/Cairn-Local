from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from cairn.agent.providers import ModelError, build_client
from cairn.agent.settings import AgentSettings
from cairn.server.db import get_conn
from cairn.server.models import Settings, SettingsUpdate, TestSettingsRequest, TestSettingsResponse

LOG = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])

SETTINGS_COLUMNS = (
    "intent_timeout, reason_timeout, "
    "agent_provider, agent_base_url, agent_model, agent_api_key, "
    "agent_context_window, agent_max_tokens, agent_temperature, "
    "agent_request_timeout"
)


def _require_loopback(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "Cairn settings can only be changed from localhost; use an SSH tunnel for remote access")


def _row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(f"SELECT {SETTINGS_COLUMNS} FROM settings WHERE rowid = 1").fetchone()
    assert row is not None
    return row


def _agent_from_row(row: sqlite3.Row) -> AgentSettings:
    return AgentSettings(
        provider=row["agent_provider"],
        base_url=row["agent_base_url"],
        model=row["agent_model"],
        api_key=row["agent_api_key"] or "",
        context_window=row["agent_context_window"],
        max_tokens=row["agent_max_tokens"],
        temperature=row["agent_temperature"],
        request_timeout=row["agent_request_timeout"],
    )


def _view(row: sqlite3.Row) -> Settings:
    agent = _agent_from_row(row)
    return Settings(
        intent_timeout=row["intent_timeout"],
        reason_timeout=row["reason_timeout"],
        agent=agent.model_copy(update={"api_key": ""}),
        api_key_configured=bool(agent.api_key),
        api_key_hint=agent.masked_key,
    )


@router.get("/settings", response_model=Settings)
def get_settings():
    with get_conn() as conn:
        return _view(_row(conn))


@router.get("/settings/agent", response_model=AgentSettings)
def get_agent_settings(request: Request):
    """Full model configuration, including the key, used by the dispatcher only."""
    _require_loopback(request)
    with get_conn() as conn:
        return _agent_from_row(_row(conn))


@router.put("/settings", response_model=Settings)
def update_settings(body: SettingsUpdate):
    with get_conn() as conn:
        current = _agent_from_row(_row(conn))
        new_key = body.agent.api_key.strip()
        if body.clear_api_key:
            new_key = ""
        elif not new_key:
            if body.agent.base_url.rstrip("/") != current.base_url.rstrip("/"):
                raise HTTPException(
                    400,
                    "Base URL changed; provide a new API Key (or clear the saved key) instead of reusing the old key",
                )
            new_key = current.api_key
        agent = body.agent.model_copy(update={"api_key": new_key})
        conn.execute(
            """
            UPDATE settings
            SET intent_timeout = ?, reason_timeout = ?,
                agent_provider = ?, agent_base_url = ?, agent_model = ?, agent_api_key = ?,
                agent_context_window = ?, agent_max_tokens = ?, agent_temperature = ?,
                agent_request_timeout = ?
            WHERE rowid = 1
            """,
            (
                body.intent_timeout,
                body.reason_timeout,
                agent.provider,
                agent.base_url,
                agent.model,
                agent.api_key,
                agent.context_window,
                agent.max_tokens,
                agent.temperature,
                agent.request_timeout,
            ),
        )
        return _view(_row(conn))


@router.post("/settings/test", response_model=TestSettingsResponse)
def test_settings(body: TestSettingsRequest | None = None):
    test_request = body or TestSettingsRequest()
    with get_conn() as conn:
        stored = _agent_from_row(_row(conn))
    agent = test_request.agent or stored
    # Only reuse the stored key when the caller is testing the same endpoint.
    # Otherwise a LAN caller could point base_url at their own server and make
    # Cairn POST the stored key there.
    if (
        not agent.api_key.strip()
        and stored.api_key.strip()
        and agent.base_url.rstrip("/") == stored.base_url.rstrip("/")
    ):
        agent = agent.model_copy(update={"api_key": stored.api_key})
    if not agent.configured:
        return TestSettingsResponse(ok=False, model=agent.model, detail="Base URL、模型名和 API Key 都必须填写")
    client = build_client(agent)
    try:
        latency = client.ping(timeout=test_request.timeout)
        return TestSettingsResponse(ok=True, latency=round(latency, 2), model=agent.model)
    except ModelError as exc:
        LOG.warning("model connection test failed model=%s status=%s", agent.model, exc.status_code)
        return TestSettingsResponse(ok=False, model=agent.model, detail=str(exc)[:1000])
    except Exception as exc:
        LOG.warning("model connection test failed model=%s error=%s", agent.model, exc)
        return TestSettingsResponse(ok=False, model=agent.model, detail=str(exc)[:1000])
    finally:
        client.close()
