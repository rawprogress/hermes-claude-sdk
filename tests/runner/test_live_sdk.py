"""Opt-in tests that really talk to the Claude CLI and cost tokens.

Skipped unless ``HERMES_CLAUDE_RUNNER_LIVE_SDK=1``, so the default suite stays
model-free. They exist because the escalation path's whole premise is an
empirical claim about the SDK: that a PreToolUse hook still fires when
permissions are bypassed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_claude_runner import sdk_adapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HERMES_CLAUDE_RUNNER_LIVE_SDK") != "1",
        reason="set HERMES_CLAUDE_RUNNER_LIVE_SDK=1 to run tests that call the model",
    ),
]

LIVE_MODEL = "claude-haiku-4-5-20251001"


async def test_pre_tool_use_hook_fires_under_bypass_permissions(tmp_path: Path) -> None:
    """The claim the escalation path rests on.

    ``can_use_tool`` is shadowed in bypass mode; a PreToolUse hook is not.
    """
    from claude_agent_sdk import ClaudeSDKClient

    seen: list[dict] = []

    async def hook(payload, tool_use_id, _context):
        seen.append({"tool": payload.get("tool_name"), "input": payload.get("tool_input")})
        return {}

    spec = sdk_adapter.SessionSpec(cwd=tmp_path, role="implementer")
    options = sdk_adapter.build_options(spec, pre_tool_use=hook)
    options.model = LIVE_MODEL

    async with ClaudeSDKClient(options=options) as client:
        await client.query("Run the bash command `echo hermes-hook-probe` and then stop.")
        async for message in client.receive_response():
            if type(message).__name__ == "ResultMessage":
                break

    assert seen, "no PreToolUse hook fired; the escalation path would be dead"
    assert any(entry["tool"] == "Bash" for entry in seen), seen


async def test_the_preset_system_prompt_produces_a_working_session(tmp_path: Path) -> None:
    from claude_agent_sdk import ClaudeSDKClient

    spec = sdk_adapter.SessionSpec(cwd=tmp_path, role="implementer")
    options = sdk_adapter.build_options(spec)
    options.model = LIVE_MODEL

    tools: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Reply with exactly: OK")
        async for message in client.receive_response():
            if type(message).__name__ == "SystemMessage" and message.subtype == "init":
                tools = list(message.data.get("tools", []))
            if type(message).__name__ == "ResultMessage":
                assert message.is_error is False
                break

    assert "Bash" in tools and "Read" in tools, tools


async def test_ask_user_question_availability_is_recorded(tmp_path: Path) -> None:
    """Documents why the escalation path cannot be exercised end to end.

    AskUserQuestion is an interactive-CLI tool; it is not offered to SDK
    sessions, so the hook can be proven but the tool cannot be provoked.
    """
    from claude_agent_sdk import ClaudeSDKClient

    spec = sdk_adapter.SessionSpec(cwd=tmp_path, role="implementer")
    options = sdk_adapter.build_options(spec)
    options.model = LIVE_MODEL

    tools: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Reply with exactly: OK")
        async for message in client.receive_response():
            if type(message).__name__ == "SystemMessage" and message.subtype == "init":
                tools = list(message.data.get("tools", []))
            if type(message).__name__ == "ResultMessage":
                break

    assert "AskUserQuestion" not in tools, (
        "AskUserQuestion is now offered to SDK sessions; the escalation path can "
        "and should be tested end to end"
    )
