"""Read-only preflight.

Answers two questions — "is this machine ready?" and, when it is not, "is
the only thing missing the service this installer is about to create?" —
without creating, migrating or starting anything.

Both answers are single booleans, because an installing agent has to branch
on them: ``report["installable"]`` before installing, ``report["ok"]``
afterwards. A human reads the same report as a table.
"""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import sys
from typing import Any

from . import __version__, client, db
from .config import RunnerPaths

# Kept in step with pyproject's requires-python by
# tests/test_portability.py::test_the_doctor_knows_the_real_python_floor.
MINIMUM_PYTHON = (3, 12)

#: Failures that installing is expected to fix. A machine whose problems are a
#: subset of these is ready to install; anything else is a prerequisite the
#: installer cannot supply for you.
INSTALLABLE_PROBLEMS = frozenset({"daemon", "launch_agent", "wrapper"})


def _probe(name: str) -> bool:
    """Is *name* on PATH? Isolated so a test can pin the environment."""
    return shutil.which(name) is not None


def _entry(
    name: str, ok: bool, detail: str, *, required: bool = True, fix: str = "", **extra: Any
) -> dict[str, Any]:
    return {"name": name, "ok": ok, "required": required, "detail": detail,
            "fix": "" if ok else fix, **extra}


def _platform_check() -> dict[str, Any]:
    system = platform.system()
    return _entry(
        "platform", system == "Darwin", f"{system} {platform.release()}",
        fix="the runner is a macOS LaunchAgent; install it on the Mac that runs Claude Code",
    )


def _python_check() -> dict[str, Any]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    floor = ".".join(str(part) for part in MINIMUM_PYTHON)
    return _entry(
        "python", sys.version_info >= MINIMUM_PYTHON, version,
        fix=f"install Python {floor} or newer (uv python install {floor})",
        executable=sys.executable,
    )


def _binary_check(
    name: str, label: str, fix: str, *, required: bool = True
) -> dict[str, Any]:
    found = shutil.which(name)
    detail = found or f"{name} was not found on PATH"
    if not required and not found:
        detail = f"{detail} — {fix}"
    return _entry(
        label, _probe(name), detail, required=required, fix=fix, path=found,
    )


def _claude_cli_check(paths: RunnerPaths) -> dict[str, Any]:
    """The configured binary wins; PATH is the documented fallback."""
    configured = paths.claude_cli_path
    if configured.exists():
        return _entry("claude_cli", True, str(configured), path=str(configured))
    found = shutil.which("claude")
    return _entry(
        "claude_cli", found is not None,
        found or f"no claude CLI at {configured} and none on PATH",
        fix="install Claude Code and sign in once: https://claude.com/claude-code",
        path=found,
    )


def _projects_root_check(paths: RunnerPaths) -> dict[str, Any]:
    root = paths.projects_root
    return _entry(
        "projects_root", root.is_dir(), str(root),
        fix=f"create {root}, or point HERMES_CLAUDE_RUNNER_PROJECTS_ROOT at your code",
    )


def _database_check(paths: RunnerPaths) -> dict[str, Any]:
    """Report the schema version without ever creating or migrating the file."""
    path = paths.db_path
    if not path.exists():
        return _entry(
            "database", False, f"no database at {path}", required=False,
            fix="the daemon creates it on first start; nothing to do before installing",
        )
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _entry("database", False, f"{path} is unreadable: {exc}",
                      fix="inspect the file by hand; the runner never deletes state")
    return _entry(
        "database", version <= db.SCHEMA_VERSION,
        f"{path} (schema {version}, runner speaks {db.SCHEMA_VERSION})",
        fix="the database is newer than this checkout; update the runner",
        schema_version=version,
    )


def _daemon_check(paths: RunnerPaths, *, probe: bool) -> dict[str, Any]:
    if not probe:
        return _entry("daemon", False, "skipped (--no-daemon-probe)", required=False,
                      fix="run without --no-daemon-probe to contact the socket")
    if not paths.socket_path.exists():
        return _entry(
            "daemon", False, f"no socket at {paths.socket_path}",
            fix="./scripts/install_runner.sh, then hermes-claude-runner health",
        )
    envelope = client.send_request(paths.socket_path, {"action": "health"}, timeout=10)
    if not envelope.get("ok"):
        return _entry(
            "daemon", False, str(envelope.get("detail") or envelope.get("error")),
            fix="launchctl kickstart -k gui/$(id -u)/<label>",
        )
    result = envelope.get("result") or {}
    return _entry(
        "daemon", True,
        f"version {result.get('version')}, {result.get('active_runs')} active run(s)",
        active_runs=result.get("active_runs"),
    )


def _launch_agent_check(paths: RunnerPaths) -> dict[str, Any]:
    exists = paths.plist_path.is_file()
    return _entry(
        "launch_agent", exists, str(paths.plist_path),
        fix="./scripts/install_runner.sh writes it (idempotently, with a backup)",
    )


def _wrapper_check(paths: RunnerPaths) -> dict[str, Any]:
    path = paths.wrapper_path
    ok = path.is_file() and os.access(path, os.X_OK)
    return _entry(
        "wrapper", ok, str(path),
        fix="./scripts/install_runner.sh writes and chmods it",
    )


def diagnose(paths: RunnerPaths, *, probe_daemon: bool = True) -> dict[str, Any]:
    """Inspect the machine and return a JSON-serializable verdict."""
    checks = [
        _platform_check(),
        _python_check(),
        _binary_check("git", "git", "install git (xcode-select --install)"),
        # Every documented command starts with `uv run`, and install_runner.sh
        # runs `uv sync` unconditionally, so a warning here would send an agent
        # two phases further to an unmapped "uv: command not found".
        _binary_check("uv", "uv", "install uv: https://docs.astral.sh/uv/"),
        # Claude Code is the requirement; Node is an implementation detail of
        # some Claude Code installs and absent from others.
        _binary_check("node", "node",
                      "only needed if your Claude Code install requires Node.js",
                      required=False),
        _claude_cli_check(paths),
        _projects_root_check(paths),
        _database_check(paths),
        _daemon_check(paths, probe=probe_daemon),
        _launch_agent_check(paths),
        _wrapper_check(paths),
    ]
    problems = [c["name"] for c in checks if c["required"] and not c["ok"]]
    return {
        "ok": not problems,
        # True when installing is all that stands between this machine and ok.
        "installable": set(problems) <= INSTALLABLE_PROBLEMS,
        "runner_version": __version__,
        "label": paths.launch_agent_label,
        "paths": {
            "projects_root": str(paths.projects_root),
            "worktrees_root": str(paths.worktrees_root),
            "database": str(paths.db_path),
            "socket": str(paths.socket_path),
            "logs": str(paths.log_dir),
            "wrapper": str(paths.wrapper_path),
            "launch_agent": str(paths.plist_path),
        },
        "checks": checks,
        "problems": problems,
    }


def render(report: dict[str, Any]) -> str:
    """The same verdict as a human-readable table."""
    width = max(len(c["name"]) for c in report["checks"])
    lines = [f"hermes-claude-runner {report['runner_version']}  ({report['label']})", ""]
    for entry in report["checks"]:
        mark = "ok  " if entry["ok"] else ("FAIL" if entry["required"] else "warn")
        lines.append(f"  [{mark}] {entry['name']:<{width}}  {entry['detail']}")
        if entry["fix"]:
            lines.append(f"         {' ' * width}  -> {entry['fix']}")
    lines.append("")
    if report["ok"]:
        lines.append("READY")
    elif report["installable"]:
        lines.append(
            "READY TO INSTALL: only the service is missing "
            f"({', '.join(report['problems'])}). Run ./scripts/install_runner.sh"
        )
    else:
        lines.append(f"NOT READY: {', '.join(report['problems'])}")
    return "\n".join(lines)
