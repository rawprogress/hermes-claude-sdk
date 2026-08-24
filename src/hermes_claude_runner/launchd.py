"""LaunchAgent plist generation and idempotent installation.

Installation only ever adds files, and always keeps a timestamped copy of
anything it replaces. Uninstalling never touches the database or worktrees.
"""

from __future__ import annotations

import json
import plistlib
import shlex
import shutil
import time
from pathlib import Path
from typing import Any

from . import config
from .config import RunnerPaths

# launchd hands the job a minimal PATH, so the daemon needs an explicit one:
# git, node and the claude CLI all have to be findable from it.
_SYSTEM_PATH_ENTRIES = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def path_entries(home: Path) -> tuple[str, ...]:
    """PATH for the LaunchAgent, rooted in the installing account's home."""
    return (str(home / ".local/bin"), *_SYSTEM_PATH_ENTRIES)


def default_repo_root() -> Path:
    """Repository root of this checkout (``src/hermes_claude_runner/`` up two)."""
    return Path(__file__).resolve().parents[2]


def wrapper_script(repo_root: Path) -> str:
    """Shell wrapper installed at ``~/.local/bin/hermes-claude-runner``."""
    console_script = Path(repo_root) / ".venv" / "bin" / "hermes-claude-runner"
    detail = (
        f"the runner is not installed at {console_script}; "
        "re-run scripts/install_runner.sh on the Mac"
    )
    envelope = json.dumps({"ok": False, "error": "daemon_unavailable", "detail": detail})
    return (
        "#!/bin/sh\n"
        "# Managed by `hermes-claude-runner install` — regenerate rather than edit.\n"
        "# Execs the venv console script directly so nothing but the JSON\n"
        "# envelope can ever reach stdout.\n"
        f'TARGET="{console_script}"\n'
        'if [ ! -x "$TARGET" ]; then\n'
        "    # Answer Hermes in its own protocol instead of failing silently,\n"
        "    # and exit non-zero so launchd throttles instead of hot-looping.\n"
        f"    printf '%s\\n' {shlex.quote(envelope)}\n"
        "    exit 3\n"
        "fi\n"
        'exec "$TARGET" "$@"\n'
    )


def plist_definition(paths: RunnerPaths) -> dict[str, Any]:
    return {
        "Label": paths.launch_agent_label,
        "ProgramArguments": [str(paths.wrapper_path), "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        # A broken install would otherwise respawn as fast as launchd allows;
        # the wrapper reports the problem and this keeps the noise bounded.
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "WorkingDirectory": str(paths.data_dir),
        "StandardOutPath": str(paths.log_dir / "stdout.log"),
        "StandardErrorPath": str(paths.log_dir / "stderr.log"),
        "EnvironmentVariables": {
            "PATH": ":".join(path_entries(paths.home)),
            "HOME": str(paths.home),
            config.ENV_HOME: str(paths.data_dir),
            config.ENV_PROJECTS_ROOT: str(paths.projects_root),
            config.ENV_WORKTREES_ROOT: str(paths.worktrees_root),
            config.ENV_SOCKET: str(paths.socket_path),
            config.ENV_LOG_DIR: str(paths.log_dir),
            config.ENV_CLAUDE_CLI: str(paths.claude_cli_path),
        },
    }


def plist_xml(paths: RunnerPaths) -> str:
    return plistlib.dumps(plist_definition(paths)).decode()


def backup_path(target: Path, when: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when if when is not None else time.time()))
    return target.with_name(f"{target.name}.bak-{stamp}")


def _write_file(target: Path, content: str, *, executable: bool, dry_run: bool) -> dict[str, Any]:
    """Write *content* to *target*, backing up any differing existing file."""
    exists = target.exists()
    if exists and target.read_text() == content:
        return {"path": str(target), "action": "unchanged", "backup": None}

    action = "replace" if exists else "create"
    if dry_run:
        return {"path": str(target), "action": f"would_{action}", "backup": None}

    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if exists:
        backup = backup_path(target)
        shutil.copy2(target, backup)

    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(content)
    if executable:
        tmp.chmod(0o755)
    tmp.replace(target)
    return {
        "path": str(target),
        "action": "replaced" if exists else "created",
        "backup": str(backup) if backup else None,
    }


def install(
    paths: RunnerPaths,
    *,
    dry_run: bool = False,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    """Install the wrapper and the LaunchAgent plist. Idempotent."""
    root = Path(repo_root) if repo_root else default_repo_root()
    if not dry_run:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        paths.data_dir.mkdir(parents=True, exist_ok=True)

    label = paths.launch_agent_label
    wrapper = _write_file(paths.wrapper_path, wrapper_script(root),
                          executable=True, dry_run=dry_run)
    plist = _write_file(paths.plist_path, plist_xml(paths), executable=False, dry_run=dry_run)

    return {
        "dry_run": dry_run,
        "repo_root": str(root),
        "wrapper": wrapper,
        "plist": plist,
        "log_dir": str(paths.log_dir),
        "label": paths.launch_agent_label,
        "next_steps": [
            f"launchctl bootout gui/$(id -u)/{label} 2>/dev/null || true",
            f"launchctl bootstrap gui/$(id -u) {paths.plist_path}",
            f"launchctl kickstart -k gui/$(id -u)/{label}",
            f"launchctl print gui/$(id -u)/{label} | head -20",
            f"{paths.wrapper_path} health",
        ],
    }


def uninstall_instructions(paths: RunnerPaths) -> str:
    """Steps that stop the service while preserving every artefact."""
    return "\n".join([
        "# Stop and remove the LaunchAgent (state is preserved).",
        f"launchctl bootout gui/$(id -u)/{paths.launch_agent_label} 2>/dev/null || true",
        f"mv {paths.plist_path} {paths.plist_path}.removed",
        f"mv {paths.wrapper_path} {paths.wrapper_path}.removed",
        "",
        "# Deliberately preserved — delete by hand only if you really mean it:",
        f"#   database : {paths.db_path}",
        f"#   worktrees: {paths.worktrees_root}",
        f"#   logs     : {paths.log_dir}",
        f"#   data dir : {paths.data_dir}",
    ])
