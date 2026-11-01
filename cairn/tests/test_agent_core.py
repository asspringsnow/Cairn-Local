from __future__ import annotations

import json

from cairn.agent.context import estimate_messages_tokens, should_compact
from cairn.agent.messages import assistant_message, system_message, tool_message, to_anthropic, to_openai, user_message
from cairn.agent.providers import anthropic_url, openai_url, parse_anthropic_sse, parse_openai_sse
from cairn.agent.session import SessionState, SessionStore
from cairn.agent.settings import AgentSettings
from cairn.agent.tools import Toolbox
from cairn.dispatcher.contracts import validate_bootstrap_execute_payload, validate_explore_payload, validate_reason_payload
from cairn.dispatcher.output_parser import extract_json_object
from cairn.dispatcher.runtime.workspace import WorkspaceManager


def _settings(base_url: str, provider: str = "openai") -> AgentSettings:
    return AgentSettings(provider=provider, base_url=base_url, model="test-model", api_key="secret")


def test_provider_urls_accept_base_v1_and_full_endpoint() -> None:
    assert openai_url(_settings("https://api.deepseek.com/v1")) == "https://api.deepseek.com/v1/chat/completions"
    assert openai_url(_settings("https://api.deepseek.com")) == "https://api.deepseek.com/v1/chat/completions"
    assert openai_url(_settings("https://x/v1/chat/completions")) == "https://x/v1/chat/completions"
    assert anthropic_url(_settings("https://api.deepseek.com/anthropic", "anthropic")) == "https://api.deepseek.com/anthropic/v1/messages"
    assert anthropic_url(_settings("https://api.anthropic.com", "anthropic")) == "https://api.anthropic.com/v1/messages"


def test_messages_convert_to_openai_and_anthropic_tool_transactions() -> None:
    messages = [
        system_message("system"),
        user_message("inspect"),
        assistant_message("", [{"id": "c1", "name": "bash", "arguments": {"command": "id"}}]),
        tool_message("c1", "bash", "uid=0"),
    ]

    openai = to_openai(messages)
    assert openai[2]["tool_calls"][0]["function"]["name"] == "bash"
    assert openai[3] == {"role": "tool", "tool_call_id": "c1", "content": "uid=0"}

    system, anthropic = to_anthropic(messages)
    assert system == "system"
    assert anthropic[1]["content"][0]["type"] == "tool_use"
    assert anthropic[2]["content"][0]["type"] == "tool_result"
    assert anthropic[2]["content"][0]["tool_use_id"] == "c1"


def test_toolbox_write_read_edit_grep_find_ls(tmp_path) -> None:
    toolbox = Toolbox(tmp_path)
    assert "Successfully wrote" in toolbox.execute("write", {"path": "notes/a.txt", "content": "alpha\nbeta\n"}).content
    assert "beta" in toolbox.execute("read", {"path": "notes/a.txt"}).content
    edited = toolbox.execute("edit", {"path": "notes/a.txt", "edits": [{"oldText": "beta", "newText": "BETA"}]})
    assert not edited.is_error
    assert "BETA" in toolbox.execute("read", {"path": "notes/a.txt"}).content
    assert "BETA" in toolbox.execute("grep", {"pattern": "BETA", "path": "."}).content
    assert "notes/a.txt" in toolbox.execute("find", {"pattern": "*.txt", "path": "."}).content
    assert "notes/" in toolbox.execute("ls", {"path": "."}).content


def test_bash_tool_runs_in_workspace(tmp_path) -> None:
    toolbox = Toolbox(tmp_path)
    result = toolbox.execute("bash", {"command": "echo hello"})
    assert not result.is_error
    payload = json.loads(result.content)
    assert "hello" in payload["output"]
    assert payload["status"] == "success"


def test_output_parser_repairs_truncated_json_and_accepts_nested_complete() -> None:
    truncated = '{"accepted": true, "data": {"fact": {"description": "done"}, "complete": {"description": "goal met"}'
    payload = extract_json_object(truncated)
    assert validate_bootstrap_execute_payload(payload)[0] == "complete"

    nested = {
        "accepted": True,
        "data": {"fact": {"description": "done", "complete": {"description": "goal met"}}},
    }
    assert validate_bootstrap_execute_payload(nested)[0] == "complete"


def test_reason_and_explore_contracts() -> None:
    assert validate_reason_payload({"accepted": True, "data": {}}, open_intents_empty=False, max_intents=2)[0] == "noop"
    assert validate_reason_payload(
        {"accepted": True, "data": {"intents": [{"from": ["f001"], "description": "next"}]}},
        open_intents_empty=False,
        max_intents=2,
    )[0] == "intents"
    assert validate_explore_payload({"accepted": True, "data": {"description": "fact"}}) == ("fact", "fact")


def test_session_store_roundtrip(tmp_path) -> None:
    path = tmp_path / "session.json"
    store = SessionStore(path)
    state = SessionState(system_prompt="sys")
    state.messages.append(user_message("hello"))
    state.record_usage({"prompt_tokens": 3, "completion_tokens": 2, "cached_tokens": 1})
    store.save(state)

    loaded = store.load("other")
    assert loaded.system_prompt == "sys"
    assert loaded.messages == state.messages
    assert loaded.usage == {"prompt_tokens": 3, "completion_tokens": 2, "cached_tokens": 1}


def test_workspace_manager_paths_and_cleanup(tmp_path) -> None:
    workspace = WorkspaceManager(tmp_path / "sessions", completed_action="remove")
    project_dir = workspace.ensure_running("proj_001")
    assert project_dir.endswith("proj_001")
    assert workspace.session_path("proj_001", "explore", "i001").name == "explore-i001.json"
    assert workspace.needs_completed_cleanup("proj_001")
    assert workspace.cleanup_completed("proj_001")
    assert not workspace.project_dir("proj_001").exists()


def test_agent_settings_mask_and_token_estimate() -> None:
    settings = AgentSettings(api_key="sk-1234567890", model="m", base_url="http://x")
    assert settings.masked_key == "sk-1...7890"
    assert settings.configured
    assert estimate_messages_tokens([user_message("a" * 40)]) > 0


def test_agent_default_context_and_output_follow_pi() -> None:
    settings = AgentSettings(api_key="x", model="deepseek-flash", base_url="https://api.deepseek.com/v1")
    assert settings.context_window == 1_000_000
    assert settings.max_tokens == 16_384


def test_openai_stream_parses_text_tool_deltas_and_usage() -> None:
    lines = [
        '{"choices":[{"delta":{"content":"hel"}}]}',
        '{"choices":[{"delta":{"content":"lo"}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"bash","arguments":"{\\"command\\":\\"id"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"}"}}]}}]}',
        '{"usage":{"prompt_tokens":10,"completion_tokens":5}}',
        '[DONE]',
    ]
    response = parse_openai_sse(lines)
    assert response.text == "hello"
    assert response.tool_calls[0]["name"] == "bash"
    assert response.tool_calls[0]["arguments"] == {"command": "id"}
    assert response.usage["prompt_tokens"] == 10


def test_anthropic_stream_parses_text_tool_input_and_usage() -> None:
    lines = [
        '{"type":"message_start","message":{"usage":{"input_tokens":10,"cache_read_input_tokens":2}}}',
        '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}',
        '{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"c1","name":"bash","input":{}}}',
        '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":\\"id\\"}"}}',
        '{"type":"message_delta","usage":{"output_tokens":5},"delta":{"stop_reason":"tool_use"}}',
        '{"type":"message_stop"}',
    ]
    response = parse_anthropic_sse(lines)
    assert response.text == "hello"
    assert response.tool_calls[0]["arguments"] == {"command": "id"}
    assert response.usage == {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 2}


def test_usage_based_compaction_trigger_and_parallel_tool_flags(tmp_path) -> None:
    settings = AgentSettings(api_key="x", model="m", base_url="http://x", context_window=100000)
    messages = [user_message("small")]
    assert not should_compact(messages, settings, last_prompt_tokens=1000)
    assert should_compact(messages, settings, last_prompt_tokens=99000)

    toolbox = Toolbox(tmp_path)
    assert toolbox.is_parallel_safe("read")
    assert toolbox.is_parallel_safe("grep")
    assert toolbox.is_parallel_safe("find")
    assert toolbox.is_parallel_safe("ls")
    assert not toolbox.is_parallel_safe("bash")
    assert not toolbox.is_parallel_safe("write")
    assert not toolbox.is_parallel_safe("edit")
