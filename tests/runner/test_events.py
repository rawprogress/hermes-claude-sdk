"""SDK message -> durable event conversion. No chain of thought, no secrets."""

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from hermes_claude_runner import events


def _assistant(*blocks) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude-opus-5")


def test_assistant_text_becomes_one_event() -> None:
    out = events.events_from_message(_assistant(TextBlock(text="Fixed the bug.")))
    assert [e["kind"] for e in out] == ["assistant_text"]
    assert out[0]["payload"]["text"] == "Fixed the bug."


def test_thinking_is_never_persisted_verbatim() -> None:
    secret_thought = "I should consider the user's private plan " * 20
    out = events.events_from_message(_assistant(ThinkingBlock(thinking=secret_thought,
                                                             signature="sig")))
    assert [e["kind"] for e in out] == ["thinking"]
    payload = out[0]["payload"]
    assert payload == {"chars": len(secret_thought)}
    assert "consider" not in str(payload)
    assert "sig" not in str(payload)


def test_tool_use_records_name_and_redacted_input() -> None:
    out = events.events_from_message(_assistant(ToolUseBlock(
        id="tu_1", name="Bash",
        input={
            "command": (
                "export GH_TOKEN=" + "gh" + "p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" + " && ls"
            )
        },
    )))
    assert out[0]["kind"] == "tool_use"
    assert out[0]["payload"]["name"] == "Bash"
    assert out[0]["payload"]["id"] == "tu_1"
    assert "ghp_" not in str(out[0]["payload"])


def test_tool_result_records_status_and_bounded_preview() -> None:
    out = events.events_from_message(UserMessage(content=[
        ToolResultBlock(tool_use_id="tu_1", content="x" * 20_000, is_error=True)
    ]))
    payload = out[0]["payload"]
    assert out[0]["kind"] == "tool_result"
    assert payload["tool_use_id"] == "tu_1"
    assert payload["is_error"] is True
    assert len(payload["content"]) <= events.MAX_TEXT_CHARS + 32
    assert payload["truncated"] is True


def test_plain_user_text_becomes_a_user_message_event() -> None:
    out = events.events_from_message(UserMessage(content="please also add docs"))
    assert out[0]["kind"] == "user_message"
    assert out[0]["payload"]["text"] == "please also add docs"


def test_system_message_keeps_only_whitelisted_keys() -> None:
    msg = SystemMessage(subtype="init", data={
        "session_id": "sess-42",
        "model": "claude-opus-5",
        "cwd": "/Users/x/Projects/demo",
        "permissionMode": "bypassPermissions",
        "tools": ["Bash", "Read", "Edit"],
        "apiKeySource": "ANTHROPIC_API_KEY",
        "env": {"ANTHROPIC_API_KEY": 'sk' + '-ant-api03-SECRETSECRETSECRET1234'},
    })
    payload = events.events_from_message(msg)[0]["payload"]
    assert payload["subtype"] == "init"
    assert payload["session_id"] == "sess-42"
    assert payload["model"] == "claude-opus-5"
    assert payload["cwd"] == "/Users/x/Projects/demo"
    assert payload["permission_mode"] == "bypassPermissions"
    assert payload["tool_count"] == 3
    assert "apiKeySource" not in payload
    assert "sk-ant" not in str(payload)


def test_result_message_summarizes_the_turn() -> None:
    msg = ResultMessage(
        subtype="success", duration_ms=1234, duration_api_ms=1000, is_error=False,
        num_turns=3, session_id="sess-42", total_cost_usd=0.42, result="All 3 tests pass.",
    )
    payload = events.events_from_message(msg)[0]["payload"]
    assert events.events_from_message(msg)[0]["kind"] == "result"
    assert payload["subtype"] == "success"
    assert payload["is_error"] is False
    assert payload["num_turns"] == 3
    assert payload["session_id"] == "sess-42"
    assert payload["duration_ms"] == 1234
    assert payload["result"] == "All 3 tests pass."


def test_assistant_message_with_several_blocks_yields_several_events() -> None:
    out = events.events_from_message(_assistant(
        TextBlock(text="Running tests"),
        ToolUseBlock(id="tu_2", name="Bash", input={"command": "pytest -q"}),
    ))
    assert [e["kind"] for e in out] == ["assistant_text", "tool_use"]


def test_empty_assistant_text_is_dropped() -> None:
    assert events.events_from_message(_assistant(TextBlock(text="   "))) == []


def test_unknown_message_type_degrades_to_a_bounded_event() -> None:
    class Mystery:
        pass

    out = events.events_from_message(Mystery())
    assert out[0]["kind"] == "sdk_message"
    assert out[0]["payload"]["type"] == "Mystery"


def test_serialization_never_raises_on_weird_content() -> None:
    class Weird:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    out = events.events_from_message(_assistant(ToolUseBlock(
        id="tu", name="X", input={"weird": Weird()},
    )))
    assert out[0]["kind"] == "tool_use"


def test_every_emitted_kind_is_declared() -> None:
    messages = [
        _assistant(TextBlock(text="a"), ThinkingBlock(thinking="t", signature="s"),
                   ToolUseBlock(id="i", name="n", input={})),
        UserMessage(content=[ToolResultBlock(tool_use_id="i", content="c", is_error=None)]),
        UserMessage(content="hi"),
        SystemMessage(subtype="init", data={}),
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                      num_turns=1, session_id="s"),
    ]
    for msg in messages:
        for event in events.events_from_message(msg):
            assert event["kind"] in events.EVENT_KINDS


def test_session_id_is_extracted_from_system_and_result_messages() -> None:
    assert events.session_id_from_message(
        SystemMessage(subtype="init", data={"session_id": "s1"})) == "s1"
    assert events.session_id_from_message(
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                      num_turns=1, session_id="s2")) == "s2"
    assert events.session_id_from_message(
        AssistantMessage(content=[], model="m", session_id="s3")) == "s3"
    assert events.session_id_from_message(UserMessage(content="x")) is None
