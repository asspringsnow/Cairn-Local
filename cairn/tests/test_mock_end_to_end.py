from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from typing import Any

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest

from cairn.agent.settings import AgentSettings
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.models import ReasonCheckpoint
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.runtime.workspace import WorkspaceManager
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import ProjectDetail, ProjectSummary, Settings


class InProcessClient:
    def __init__(self, http: TestClient):
        self.http = http
        self._summaries = TypeAdapter(list[ProjectSummary])

    def close(self) -> None:
        return None

    def list_projects(self) -> list[ProjectSummary]:
        response = self.http.get("/projects")
        response.raise_for_status()
        return self._summaries.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self.http.get(f"/projects/{project_id}")
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self.http.get("/settings")
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self.http.get(f"/projects/{project_id}/export?format=yaml")
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/intents/{intent_id}/heartbeat", {"worker": worker})

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/claim", {"worker": worker, "trigger": trigger})

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/heartbeat", {"worker": worker})

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/release", {"worker": worker})

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/intents/{intent_id}/release", {"worker": worker})

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            {"worker": worker, "description": description},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/complete",
            {"from": from_ids, "description": description, "worker": worker},
        )

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/intents",
            {"from": from_ids, "description": description, "creator": creator, "worker": None},
        )

    def _post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        response = self.http.post(path, json=payload)
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        return ApiResult(response.status_code, data, response.text)


@pytest.fixture
def http_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as client:
        yield client


@pytest.fixture
def workspace(tmp_path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "work")


def _phase(
    outcome: str,
    *,
    rules: list[dict[str, Any]] | None = None,
    zero_outcomes: list[str] | None = None,
) -> str:
    outcomes = {name: 0 for name in zero_outcomes or []}
    outcomes[outcome] = 1
    payload: dict[str, Any] = {"delay": [0, 0], "outcomes": outcomes}
    if rules is not None:
        payload["rules"] = rules
    return json.dumps(payload)


def _config(
    *,
    bootstrap: str,
    reason: str,
    explore: str,
    task_types: list[str] | None = None,
    worker_healthcheck: str = "startup_only",
    healthcheck: str | None = None,
) -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "worker_healthcheck": worker_healthcheck,
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 2, "conclude_timeout": 2},
                "reason": {"timeout": 2, "max_intents": 1},
                "explore": {"timeout": 2, "conclude_timeout": 2},
            },
            "workers": [
                {
                    "name": "mock-worker",
                    "type": "mock",
                    "task_types": task_types or ["bootstrap", "reason", "explore"],
                    "max_running": 1,
                    "priority": 0,
                    "env": {
                        "MOCK_HEALTHCHECK": healthcheck or _phase("ok"),
                        "MOCK_BOOTSTRAP": bootstrap,
                        "MOCK_REASON": reason,
                        "MOCK_EXPLORE_EXECUTE": explore,
                    },
                }
            ],
        }
    )


def _loop(config: DispatchConfig, client: InProcessClient, workspace: WorkspaceManager) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = config
    loop.client = client
    loop.workspace = workspace
    loop.agent_settings = AgentSettings(api_key="dummy", model="dummy", base_url="http://dummy")
    loop._agent_settings_signature = None
    loop._refresh_agent_settings = lambda: None
    loop.run_startup_healthchecks = lambda **kwargs: None
    loop.executor = ThreadPoolExecutor(max_workers=config.runtime.max_workers)
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
    loop.futures = {}
    loop.cleanup_futures = {}
    loop.reason_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._log_state = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop.project_cursor = 0
    return loop


def _dispatch_and_wait(loop: DispatcherLoop) -> None:
    loop._reap_futures()
    summaries = loop.client.list_projects()
    loop._initialize_reason_checkpoints(summaries)
    loop._refresh_runtime_projects(summaries)
    loop._cancel_inactive_tasks(summaries)
    loop._queue_workspace_cleanups(summaries)
    loop._dispatch_available(summaries)
    assert loop.futures
    for future in list(loop.futures):
        future.result(timeout=5)
    loop._reap_futures()


def _create_project(http: TestClient) -> str:
    response = http.post(
        "/projects",
        json={"title": "integration", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_mock_scheduler_bootstrap_completes_project_end_to_end(http_client: TestClient, workspace: WorkspaceManager) -> None:
    client = InProcessClient(http_client)
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
        ),
        client,
        workspace,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin", "goal", "f001"]
    assert [(intent.id, intent.to) for intent in project.intents] == [("i001", "f001"), ("i002", "goal")]


def test_mock_scheduler_runs_reason_explore_reason_complete_chain(http_client: TestClient, workspace: WorkspaceManager) -> None:
    client = InProcessClient(http_client)
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("intent", rules=[{"fact_ids_gte": 3, "force": "complete"}]),
            explore=_phase("fact"),
        ),
        client,
        workspace,
    )
    project_id = _create_project(http_client)
    seed = client.create_intent(project_id, ["origin"], "seed", "seed-worker")
    assert seed.ok
    assert client.heartbeat(project_id, "i001", "seed-worker").ok
    assert client.conclude(project_id, "i001", "seed-worker", "seed fact").ok

    try:
        _dispatch_and_wait(loop)
        assert loop.reason_checkpoints[project_id] == ReasonCheckpoint(3, 0, 0)
        _dispatch_and_wait(loop)
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin", "goal", "f001", "f002"]
    assert [(intent.id, intent.to) for intent in project.intents] == [
        ("i001", "f001"),
        ("i002", "f002"),
        ("i003", "goal"),
    ]
    prompt_root = workspace.project_dir(project_id) / ".cairn" / "prompts"
    assert any(p.parent.name.startswith("reason_execute-") and "f002" in p.read_text(encoding="utf-8") for p in prompt_root.glob("*/graph.yaml"))
    assert any(p.parent.name.startswith("explore_execute-") and "f001" in p.read_text(encoding="utf-8") for p in prompt_root.glob("*/graph.yaml"))


def test_mock_scheduler_enabled_project_skips_bootstrap_when_worker_does_not_support_it(
    http_client: TestClient,
    workspace: WorkspaceManager,
) -> None:
    client = InProcessClient(http_client)
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            task_types=["reason", "explore"],
        ),
        client,
        workspace,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [(intent.description, intent.to) for intent in project.intents] == [
        ("mock complete from origin", "goal")
    ]


def test_task_healthcheck_healthy_worker_completes_end_to_end(http_client: TestClient, workspace: WorkspaceManager) -> None:
    client = InProcessClient(http_client)
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            worker_healthcheck="startup_and_task",
        ),
        client,
        workspace,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    # code-based check_health runs before the task, passes, and the bootstrap completes
    assert project.project.status == "completed"


def test_task_healthcheck_failure_aborts_task_and_cools_down_worker(http_client: TestClient, workspace: WorkspaceManager) -> None:
    client = InProcessClient(http_client)
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            worker_healthcheck="startup_and_task",
            healthcheck=_phase("fail", zero_outcomes=["ok"]),
        ),
        client,
        workspace,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    # unhealthy worker -> task aborted before execution, no facts written, worker put on cooldown
    assert project.project.status == "active"
    assert [fact.id for fact in project.facts] == ["origin", "goal"]
    assert "mock-worker" in loop.worker_unhealthy_until


def _failover_config() -> DispatchConfig:
    def worker(name: str, priority: int, healthcheck: str) -> dict:
        return {
            "name": name,
            "type": "mock",
            "task_types": ["bootstrap", "reason", "explore"],
            "max_running": 1,
            "priority": priority,
            "env": {
                "MOCK_HEALTHCHECK": healthcheck,
                "MOCK_BOOTSTRAP": _phase("complete"),
                "MOCK_REASON": _phase("complete", zero_outcomes=["intent"]),
                "MOCK_EXPLORE_EXECUTE": _phase("fact"),
            },
        }

    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "worker_healthcheck": "startup_and_task",
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 2, "conclude_timeout": 2},
                "reason": {"timeout": 2, "max_intents": 1},
                "explore": {"timeout": 2, "conclude_timeout": 2},
            },
            "workers": [
                worker("bad", 0, _phase("fail", zero_outcomes=["ok"])),
                worker("good", 1, _phase("ok")),
            ],
        }
    )


def test_unhealthy_worker_fails_over_to_healthy_worker(http_client: TestClient, workspace: WorkspaceManager) -> None:
    client = InProcessClient(http_client)
    loop = _loop(_failover_config(), client, workspace)
    project_id = _create_project(http_client)

    try:
        # round 1: 'bad' (priority 0) is chosen first, its health check fails -> cooldown
        _dispatch_and_wait(loop)
        assert "bad" in loop.worker_unhealthy_until
        assert client.get_project(project_id).project.status == "active"

        # round 2: 'bad' still cooling down -> 'good' takes over and completes the project
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert any(intent.worker == "good" for intent in project.intents)
