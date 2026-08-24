from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from hermes_claude_runner import config

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def make_repo(path: Path, *, initial_file: str = "README.md") -> Path:
    """Create a real git repository with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True,
                   capture_output=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    (path / initial_file).write_text("hello\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture()
def paths(tmp_path: Path) -> Iterator[config.RunnerPaths]:
    """RunnerPaths rooted inside tmp_path, with a short-enough socket path.

    ``home`` is redirected too, so the wrapper and the LaunchAgent plist point
    somewhere disposable. Without that a test writing to ``paths.wrapper_path``
    would overwrite the caller's real installation.

    pytest's tmp_path easily exceeds the 104-byte AF_UNIX limit, so the socket
    lives in its own short directory that is removed with the test.
    """
    home = tmp_path / "home"
    home.mkdir()
    projects = tmp_path / "Projects"
    projects.mkdir()
    socket_dir = Path(tempfile.gettempdir()) / f"hcr{uuid.uuid4().hex[:8]}"
    socket_dir.mkdir()
    try:
        yield config.paths_from_env({
            config.ENV_HOME: str(tmp_path / "data"),
            config.ENV_PROJECTS_ROOT: str(projects),
            config.ENV_SOCKET: str(socket_dir / "d.sock"),
            config.ENV_LOG_DIR: str(tmp_path / "logs"),
            config.ENV_CLAUDE_CLI: str(tmp_path / "claude"),
        }, home=home)
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)
