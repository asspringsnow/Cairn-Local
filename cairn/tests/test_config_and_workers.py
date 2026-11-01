from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cairn.agent.settings import AgentSettings
from cairn.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources


REPO_ROOT = Path(__file__).resolve().parents[2]


def _minimal_config(**overrides):
    data = {
        "server": "http://127.0.0.1:8000",
        "runtime": {
            "interval": 3,
            "max_workers": 4,
            "max_running_projects": 2,
            "max_project_workers": 2,
            "healthcheck_timeout": 10,
            "prompt_group": "default",
            "workspace_root": "./workspaces",
        },
        "tasks": {
            "bootstrap": {"timeout": 10, "conclude_timeout": 5},
            "reason": {"timeout": 10, "max_intents": 2},
            "explore": {"timeout": 10, "conclude_timeout": 5},
        },
    }
    data.update(overrides)
    return data


def test_workers_are_optional_and_default_to_one_native_worker() -> None:
    config = DispatchConfig.model_validate(_minimal_config())
    assert len(config.workers) == 1
    worker = config.workers[0]
    assert worker.name == "cairn"
    assert worker.type == "native"
    assert worker.max_running == 4
    assert config.workspace_root == Path("./workspaces").expanduser()


def test_only_one_native_worker_is_supported() -> None:
    config = _minimal_config(
        workers=[
            {"name": "a", "type": "native"},
            {"name": "b", "type": "native"},
        ]
    )
    with pytest.raises(ValidationError, match="only one native worker"):
        DispatchConfig.model_validate(config)


def test_mock_worker_rejects_unknown_phase_configuration() -> None:
    with pytest.raises(ValidationError, match="unsupported mock env keys"):
        WorkerConfig.model_validate(
            {
                "name": "mock",
                "type": "mock",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"MOCK_UNKNOWN": "{}"},
            }
        )


def test_bundled_prompt_groups_have_required_placeholders() -> None:
    validate_prompt_resources("default")
    validate_prompt_resources("mock")


def test_sample_configs_load() -> None:
    example = DispatchConfig.load(REPO_ROOT / "dispatch.example.yaml")
    assert example.workers[0].type == "native"
    mock = DispatchConfig.load(REPO_ROOT / "dispatch_mock.yaml")
    assert all(worker.type == "mock" for worker in mock.workers)


def test_agent_settings_validation() -> None:
    with pytest.raises(ValidationError):
        AgentSettings(context_window=1024)
    settings = AgentSettings(provider="anthropic", api_key="x", model="m", base_url="http://x")
    assert settings.configured
    assert settings.provider == "anthropic"
