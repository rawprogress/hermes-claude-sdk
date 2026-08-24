"""Adapter around the official ``claude-agent-sdk``.

Isolated behind a small factory so unit tests can drive the worker with a
fake client and never reach a model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionMode,
    SettingSource,
)

# The operator accepts this trust model when installing: Claude runs with the
# host account's real settings, skills, hooks, MCP servers and subagents.
SETTING_SOURCES: list[SettingSource] = ["user", "project", "local"]
PERMISSION_MODE: PermissionMode = "bypassPermissions"
# Without this the SDK passes an empty ``--system-prompt``, which is the one
# thing that would stop the worker behaving like the operator's normal
# Claude Code.
SYSTEM_PROMPT_PRESET = {"type": "preset", "preset": "claude_code"}
# HookMatcher defaults to a 60s timeout, which would kill a mailbox wait
# exactly when it is being used.
HOOK_TIMEOUT_MARGIN_SECONDS = 60.0
# Tools that must not run until Hermes has answered. Under bypassPermissions
# nothing else is ever intercepted.
ESCALATE_TOOLS = ("AskUserQuestion",)


@dataclass(frozen=True)
class SessionSpec:
    """Everything that varies per run."""

    cwd: Path
    role: str
    resume: str | None = None
    claude_cli_path: Path | None = None
    dangerously_skip_permissions: bool = True
    skills: list[str] | Literal["all"] = "all"
    env: dict[str, str] = field(default_factory=dict)
    escalate_tools: tuple[str, ...] = ESCALATE_TOOLS
    block_timeout_seconds: float = 900.0


def build_options(
    spec: SessionSpec,
    *,
    pre_tool_use: Callable[..., Any] | None = None,
) -> ClaudeAgentOptions:
    """Build SDK options for one run.

    OAuth is used implicitly: no key is read, copied or injected here.
    """
    extra_args: dict[str, str | None] = {}
    if spec.dangerously_skip_permissions:
        # Runs are unattended, so the CLI flag goes alongside the programmatic
        # permission mode: either one alone has been enough to leave a prompt
        # waiting for a human who is not there.
        extra_args["dangerously-skip-permissions"] = None

    cli_path = spec.claude_cli_path
    # The LaunchAgent PATH is minimal, so an explicit binary beats a lookup —
    # but only when it really exists, otherwise the SDK's own search is better.
    resolved_cli = str(cli_path) if cli_path is not None and cli_path.exists() else None

    hooks = None
    if pre_tool_use is not None:
        hooks = {
            "PreToolUse": [
                HookMatcher(
                    matcher=None,
                    hooks=[pre_tool_use],
                    timeout=spec.block_timeout_seconds + HOOK_TIMEOUT_MARGIN_SECONDS,
                )
            ]
        }

    return ClaudeAgentOptions(
        cwd=str(spec.cwd),
        permission_mode=PERMISSION_MODE,
        setting_sources=list(SETTING_SOURCES),
        system_prompt=SYSTEM_PROMPT_PRESET,  # type: ignore[arg-type]
        skills=spec.skills,
        resume=spec.resume,
        continue_conversation=False,
        fork_session=False,
        cli_path=resolved_cli,
        extra_args=extra_args,
        env=dict(spec.env),
        # can_use_tool is deliberately unset: bypassPermissions auto-approves
        # before it runs, so a PreToolUse hook is the live interception point.
        hooks=hooks,  # type: ignore[arg-type]
        include_partial_messages=False,
    )


def build_initial_prompt(role: str, prompt: str) -> str:
    """Prefix the operator's prompt with the run's role.

    Deliberately thin: the role is a label Hermes chose, not a system prompt
    this runner invents.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    return f"[hermes role: {role}]\n\n{prompt.strip()}"


def default_client_factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
    """Real client factory used by the worker process."""
    return ClaudeSDKClient(options=options)
