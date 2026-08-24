"""hermes-claude-sdk — drive Claude Code on the Mac from Hermes Agent.

Hermes orchestrates; Claude does the coding, tests, lint, typecheck, build and
reviews. The plugin owns no state: it forwards JSON over a fixed ssh argv to
``hermes-claude-runner rpc`` on the Mac, which owns the runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import schemas
from .tools import HANDLERS, configure

logger = logging.getLogger(__name__)

TOOLSET = "claude_sdk"
SKILL_NAME = "claude-code-orchestration"

_EMOJI = {
    "claude_start": "🚀",
    "claude_send": "✉️",
    "claude_status": "📊",
    "claude_events": "📜",
    "claude_list": "📋",
    "claude_stop": "🛑",
    "claude_resume": "▶️",
}

_SETTING_KEYS = ("ssh_host", "remote_command", "timeout_seconds")


def _read_settings(ctx: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return settings
    for key in _SETTING_KEYS:
        try:
            value = getter(key, None)
        except Exception:  # noqa: BLE001 - a config read must never block load
            logger.warning("could not read plugin setting %s", key)
            continue
        if value is not None:
            settings[key] = value
    return settings


def _register_skill(ctx: Any) -> None:
    register_skill = getattr(ctx, "register_skill", None)
    if not callable(register_skill):
        return  # older Hermes without plugin skills
    skill_path = Path(__file__).parent / "skills" / SKILL_NAME / "SKILL.md"
    if not skill_path.is_file():
        logger.warning("bundled skill missing at %s", skill_path)
        return
    try:
        register_skill(
            name=SKILL_NAME,
            path=skill_path,
            description=(
                "How to orchestrate Claude Code runs on the Mac: Hermes plans and "
                "supervises, Claude writes the code and runs the checks."
            ),
        )
    except Exception:  # noqa: BLE001 - a skill clash must never break tool registration
        logger.warning("could not register the bundled skill", exc_info=True)


def register(ctx: Any) -> None:
    """Register the seven Claude orchestration tools and the bundled skill."""
    configure(_read_settings(ctx))

    for schema in schemas.ALL_SCHEMAS:
        name = schema["name"]
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=HANDLERS[name],
            description=schema["description"],
            emoji=_EMOJI.get(name, "🤖"),
        )

    _register_skill(ctx)
