"""Paths must be injectable so tests never touch the real DB or ~/Projects."""

import dataclasses
from pathlib import Path

import pytest

from hermes_claude_runner import config


def test_default_paths_are_relative_to_the_running_user_home() -> None:
    paths = config.default_paths()
    home = Path.home()
    assert paths.projects_root == home / "Projects"
    assert paths.data_dir == home / "Library/Application Support/HermesClaudeRunner"
    assert paths.db_path == paths.data_dir / "data.db"
    assert paths.worktrees_root == home / "Projects/.hermes-claude-worktrees"
    assert paths.log_dir == home / "Library/Logs/HermesClaudeRunner"
    assert paths.wrapper_path == home / ".local/bin/hermes-claude-runner"
    assert paths.plist_path == home / "Library/LaunchAgents/com.hermes-claude-sdk.runner.plist"
    assert paths.claude_cli_path == home / ".local/bin/claude"
    assert paths.launch_agent_label == config.DEFAULT_LAUNCH_AGENT_LABEL


def test_the_default_label_is_vendor_neutral() -> None:
    """A public release must not ship one operator's reverse-DNS namespace."""
    assert config.DEFAULT_LAUNCH_AGENT_LABEL == "com.hermes-claude-sdk.runner"


def test_an_existing_install_keeps_its_label(tmp_path: Path) -> None:
    """Backward compatibility: an install made under any earlier label.

    The value is invented on purpose. What matters is that an overridden label
    survives, not which label some particular installation happened to use.
    """
    old_label = "com.example.legacy-runner"
    paths = config.paths_from_env({config.ENV_LABEL: old_label})
    assert paths.launch_agent_label == old_label
    assert paths.plist_path.name == f"{old_label}.plist"


@pytest.mark.parametrize(
    "label",
    ["", "   ", "with space", "with/slash", "../escape", "tab\there", "dot.."],
)
def test_an_unusable_label_is_refused(label: str) -> None:
    """A label becomes a filename and a launchctl target; it must stay tame."""
    with pytest.raises(ValueError, match="label"):
        config.paths_from_env({config.ENV_LABEL: label})


def test_socket_path_stays_within_unix_domain_limit() -> None:
    # macOS sun_path is 104 bytes; an over-long socket path fails at bind() time.
    assert len(str(config.default_paths().socket_path).encode()) < 104


def test_env_overrides_every_root(tmp_path: Path) -> None:
    env = {
        "HERMES_CLAUDE_RUNNER_HOME": str(tmp_path / "data"),
        "HERMES_CLAUDE_RUNNER_PROJECTS_ROOT": str(tmp_path / "projects"),
        "HERMES_CLAUDE_RUNNER_WORKTREES_ROOT": str(tmp_path / "wt"),
        "HERMES_CLAUDE_RUNNER_SOCKET": str(tmp_path / "d.sock"),
        "HERMES_CLAUDE_RUNNER_LOG_DIR": str(tmp_path / "logs"),
        "HERMES_CLAUDE_RUNNER_CLAUDE_CLI": str(tmp_path / "claude"),
    }
    paths = config.paths_from_env(env)
    assert paths.data_dir == tmp_path / "data"
    assert paths.db_path == tmp_path / "data/data.db"
    assert paths.projects_root == tmp_path / "projects"
    assert paths.worktrees_root == tmp_path / "wt"
    assert paths.socket_path == tmp_path / "d.sock"
    assert paths.log_dir == tmp_path / "logs"
    assert paths.claude_cli_path == tmp_path / "claude"


def test_worktrees_root_follows_projects_root_override(tmp_path: Path) -> None:
    paths = config.paths_from_env({"HERMES_CLAUDE_RUNNER_PROJECTS_ROOT": str(tmp_path / "p")})
    assert paths.worktrees_root == tmp_path / "p/.hermes-claude-worktrees"


def test_env_paths_are_expanded_and_absolute(tmp_path: Path) -> None:
    paths = config.paths_from_env({"HERMES_CLAUDE_RUNNER_PROJECTS_ROOT": "~/somewhere"})
    assert paths.projects_root == Path.home() / "somewhere"


def test_relative_env_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        config.paths_from_env({"HERMES_CLAUDE_RUNNER_HOME": "relative/dir"})


def test_paths_are_frozen() -> None:
    paths = config.default_paths()
    with pytest.raises(dataclasses.FrozenInstanceError):
        paths.projects_root = Path("/tmp/x")  # type: ignore[misc]


def test_home_override_moves_every_home_relative_root(tmp_path: Path) -> None:
    """A packaged release must work for any user, not only the author's account.

    HOME is the only machine-specific input the defaults read.
    """
    fake_home = tmp_path / "somebody-else"
    fake_home.mkdir()
    paths = config.paths_from_env({}, home=fake_home)

    for root in (
        paths.projects_root, paths.data_dir, paths.worktrees_root,
        paths.socket_path, paths.log_dir, paths.wrapper_path,
        paths.plist_path, paths.claude_cli_path,
    ):
        assert root.is_relative_to(fake_home), root


def test_the_shared_paths_fixture_is_disposable(paths: config.RunnerPaths, tmp_path: Path) -> None:
    """Guard: a test writing to these must never reach the caller's real install."""
    for root in (paths.data_dir, paths.projects_root, paths.log_dir,
                 paths.wrapper_path, paths.plist_path, paths.claude_cli_path):
        assert not root.is_relative_to(Path.home()) or root.is_relative_to(tmp_path), root
        assert root.is_relative_to(tmp_path), root
