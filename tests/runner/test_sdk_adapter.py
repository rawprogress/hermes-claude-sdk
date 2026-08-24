"""Options handed to claude-agent-sdk. No model calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_claude_runner import sdk_adapter


def spec(tmp_path: Path, **kw) -> sdk_adapter.SessionSpec:
    params = dict(cwd=tmp_path, role="implementer", resume=None, claude_cli_path=None)
    params.update(kw)
    return sdk_adapter.SessionSpec(**params)


def test_options_bypass_permissions_and_run_in_the_worktree(tmp_path: Path) -> None:
    options = sdk_adapter.build_options(spec(tmp_path))
    assert options.permission_mode == "bypassPermissions"
    assert Path(options.cwd) == tmp_path
    assert options.resume is None


def test_options_load_user_project_and_local_settings(tmp_path: Path) -> None:
    # Without this Claude would not see CLAUDE.md, skills, hooks, MCP or subagents.
    options = sdk_adapter.build_options(spec(tmp_path))
    assert options.setting_sources == ["user", "project", "local"]
    assert options.skills == "all"


def test_options_expose_the_dangerously_skip_permissions_flag(tmp_path: Path) -> None:
    options = sdk_adapter.build_options(spec(tmp_path))
    assert "dangerously-skip-permissions" in options.extra_args


def test_skip_permissions_flag_can_be_switched_off(tmp_path: Path) -> None:
    options = sdk_adapter.build_options(spec(tmp_path, dangerously_skip_permissions=False))
    assert "dangerously-skip-permissions" not in options.extra_args


def test_options_never_carry_an_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", 'sk' + '-ant-api03-MUSTNOTBECOPIED12345')
    options = sdk_adapter.build_options(spec(tmp_path))
    assert "ANTHROPIC_API_KEY" not in options.env
    assert "sk-ant" not in str(options.env)


def test_options_pin_the_cli_path_for_the_launch_agent(tmp_path: Path) -> None:
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    options = sdk_adapter.build_options(spec(tmp_path, claude_cli_path=cli))
    assert Path(options.cli_path) == cli


def test_missing_cli_path_is_left_to_path_lookup(tmp_path: Path) -> None:
    options = sdk_adapter.build_options(spec(tmp_path, claude_cli_path=tmp_path / "absent"))
    assert options.cli_path is None


def test_resume_is_passed_through(tmp_path: Path) -> None:
    options = sdk_adapter.build_options(spec(tmp_path, resume="sess-7"))
    assert options.resume == "sess-7"
    assert options.continue_conversation is False
    assert options.fork_session is False


def test_no_can_use_tool_callback_is_installed(tmp_path: Path) -> None:
    """bypassPermissions auto-approves before can_use_tool runs, so it is dead.

    The live interception point is a PreToolUse hook; keeping a shadowed
    callback around would only produce a warning worth suppressing.
    """
    assert sdk_adapter.build_options(spec(tmp_path)).can_use_tool is None


def test_pre_tool_use_hook_is_wired_when_supplied(tmp_path: Path) -> None:
    async def hook(payload, tool_use_id, ctx):  # pragma: no cover - identity check
        raise AssertionError

    options = sdk_adapter.build_options(spec(tmp_path), pre_tool_use=hook)
    matchers = options.hooks["PreToolUse"]
    assert len(matchers) == 1
    assert matchers[0].hooks == [hook]
    assert matchers[0].matcher is None, "every tool must pass through the hook"


def test_hook_timeout_outlives_the_mailbox_wait(tmp_path: Path) -> None:
    """A 60s default hook timeout would kill the wait exactly when it is used."""
    async def hook(payload, tool_use_id, ctx):  # pragma: no cover - identity check
        raise AssertionError

    session = spec(tmp_path, block_timeout_seconds=120.0)
    matcher = sdk_adapter.build_options(session, pre_tool_use=hook).hooks["PreToolUse"][0]
    assert matcher.timeout is not None
    assert matcher.timeout > session.block_timeout_seconds


def test_no_hooks_are_registered_without_a_callback(tmp_path: Path) -> None:
    assert not sdk_adapter.build_options(spec(tmp_path)).hooks


def test_system_prompt_uses_the_claude_code_preset(tmp_path: Path) -> None:
    """Leaving it unset makes the SDK pass an empty --system-prompt."""
    options = sdk_adapter.build_options(spec(tmp_path))
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}


def test_the_cli_receives_no_empty_system_prompt_flag(tmp_path: Path) -> None:
    """Pinned to the installed SDK's argv builder; update if the SDK changes."""
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    options = sdk_adapter.build_options(spec(tmp_path))
    transport = SubprocessCLITransport(prompt="hi", options=options)
    transport._cli_path = "/bin/true"
    argv = transport._build_command()
    assert "--system-prompt" not in argv, argv
    assert "" not in argv, "an empty argv entry means an empty prompt was passed"


def test_escalated_tools_default_to_ask_user_question(tmp_path: Path) -> None:
    assert spec(tmp_path).escalate_tools == ("AskUserQuestion",)


def test_initial_prompt_frames_the_role_without_inventing_instructions() -> None:
    framed = sdk_adapter.build_initial_prompt("reviewer", "check the diff")
    assert "check the diff" in framed
    assert "reviewer" in framed
    assert framed.strip().endswith("check the diff")


def test_initial_prompt_of_the_default_role_is_still_labelled() -> None:
    framed = sdk_adapter.build_initial_prompt("implementer", "do the thing")
    assert framed.startswith("[hermes role: implementer]")


@pytest.mark.parametrize("bad", ["", "   "])
def test_initial_prompt_requires_text(bad: str) -> None:
    with pytest.raises(ValueError):
        sdk_adapter.build_initial_prompt("implementer", bad)


def test_default_client_factory_builds_a_real_sdk_client(tmp_path: Path) -> None:
    from claude_agent_sdk import ClaudeSDKClient

    client = sdk_adapter.default_client_factory(sdk_adapter.build_options(spec(tmp_path)))
    assert isinstance(client, ClaudeSDKClient)


def test_escalation_set_is_configurable(tmp_path: Path) -> None:
    session = spec(tmp_path, escalate_tools=("AskUserQuestion", "Bash"))
    assert session.escalate_tools == ("AskUserQuestion", "Bash")
