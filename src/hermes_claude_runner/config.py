"""Filesystem locations the runner owns.

Nothing here is specific to one machine or one account: every default is
derived from ``$HOME``, and every root is injectable through
``HERMES_CLAUDE_RUNNER_*`` environment variables so tests can run against
``tmp_path`` instead of the real database, the real ``~/Projects`` tree or
the real daemon socket.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# A vendor-neutral reverse-DNS label. An install made before this default
# existed keeps working: point HERMES_CLAUDE_RUNNER_LABEL at its old label.
DEFAULT_LAUNCH_AGENT_LABEL = "com.hermes-claude-sdk.runner"
WORKTREES_DIR_NAME = ".hermes-claude-worktrees"
# The managed runtime: one virtualenv per install generation, plus a
# ``current`` symlink the installed entrypoint follows. It deliberately sits
# beside the database rather than inside a checkout, so moving or deleting the
# source tree cannot disarm the service.
RUNTIME_DIR_NAME = "runtime"
RUNTIME_GENERATIONS_DIR_NAME = "versions"
RUNTIME_LINK_NAME = "current"

ENV_HOME = "HERMES_CLAUDE_RUNNER_HOME"
ENV_PROJECTS_ROOT = "HERMES_CLAUDE_RUNNER_PROJECTS_ROOT"
ENV_WORKTREES_ROOT = "HERMES_CLAUDE_RUNNER_WORKTREES_ROOT"
ENV_SOCKET = "HERMES_CLAUDE_RUNNER_SOCKET"
ENV_LOG_DIR = "HERMES_CLAUDE_RUNNER_LOG_DIR"
ENV_RUNTIME = "HERMES_CLAUDE_RUNNER_RUNTIME"
ENV_CLAUDE_CLI = "HERMES_CLAUDE_RUNNER_CLAUDE_CLI"
ENV_LABEL = "HERMES_CLAUDE_RUNNER_LABEL"

# The label becomes a filename and a launchctl target, so it stays to the
# conservative reverse-DNS shape launchd itself documents.
_LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9][A-Za-z0-9_-]*)+\Z")


@dataclass(frozen=True)
class RunnerPaths:
    """Resolved locations for one runner installation."""

    projects_root: Path
    data_dir: Path
    worktrees_root: Path
    socket_path: Path
    log_dir: Path
    runtime_dir: Path
    wrapper_path: Path
    plist_path: Path
    claude_cli_path: Path
    launch_agent_label: str = DEFAULT_LAUNCH_AGENT_LABEL
    # The account these defaults were derived from. The LaunchAgent needs it
    # verbatim: launchd hands the job a minimal environment, so HOME and PATH
    # have to be spelled out, and they must name the same account as the rest.
    home: Path = field(default_factory=Path.home)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "data.db"

    @property
    def runtime_versions_dir(self) -> Path:
        """Where each install generation's virtualenv is kept."""
        return self.runtime_dir / RUNTIME_GENERATIONS_DIR_NAME

    @property
    def runtime_link(self) -> Path:
        """The symlink that decides which generation is live."""
        return self.runtime_dir / RUNTIME_LINK_NAME

    @property
    def runtime_entrypoint(self) -> Path:
        """The stable executable the LaunchAgent's wrapper runs."""
        return self.runtime_link / "bin" / "hermes-claude-runner"


def _resolve(env: Mapping[str, str], key: str, fallback: Path) -> Path:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return fallback
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{key} must be an absolute path, got {raw!r}")
    return path


def _resolve_label(env: Mapping[str, str]) -> str:
    raw = env.get(ENV_LABEL)
    if raw is None:
        return DEFAULT_LAUNCH_AGENT_LABEL
    label = raw.strip()
    if not _LABEL_RE.match(label):
        raise ValueError(
            f"{ENV_LABEL} must be a reverse-DNS LaunchAgent label "
            f"like {DEFAULT_LAUNCH_AGENT_LABEL}, got {raw!r}"
        )
    return label


def paths_from_env(
    env: Mapping[str, str] | None = None, *, home: Path | None = None
) -> RunnerPaths:
    """Build :class:`RunnerPaths`, letting *env* override each root.

    *home* exists so a test can prove the defaults follow whichever account
    runs the code rather than the one that wrote it.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    label = _resolve_label(env)
    projects_root = _resolve(env, ENV_PROJECTS_ROOT, home / "Projects")
    data_dir = _resolve(env, ENV_HOME, home / "Library/Application Support/HermesClaudeRunner")
    return RunnerPaths(
        projects_root=projects_root,
        data_dir=data_dir,
        worktrees_root=_resolve(env, ENV_WORKTREES_ROOT, projects_root / WORKTREES_DIR_NAME),
        socket_path=_resolve(env, ENV_SOCKET, data_dir / "daemon.sock"),
        log_dir=_resolve(env, ENV_LOG_DIR, home / "Library/Logs/HermesClaudeRunner"),
        runtime_dir=_resolve(env, ENV_RUNTIME, data_dir / RUNTIME_DIR_NAME),
        wrapper_path=home / ".local/bin/hermes-claude-runner",
        plist_path=home / f"Library/LaunchAgents/{label}.plist",
        claude_cli_path=_resolve(env, ENV_CLAUDE_CLI, home / ".local/bin/claude"),
        launch_agent_label=label,
        home=home,
    )


def default_paths() -> RunnerPaths:
    """Paths for the real installation, ignoring environment overrides."""
    return paths_from_env({})
