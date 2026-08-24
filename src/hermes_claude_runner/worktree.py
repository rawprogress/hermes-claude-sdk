"""Project containment and git worktree lifecycle.

Two rules drive this module: a run may only touch a git repository that
really lives under the projects root (symlinks resolved), and nothing here
ever deletes, resets or cleans anything.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import models
from .config import RunnerPaths
from .errors import RunnerError

GIT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class WorktreeInfo:
    """Where a run does its work."""

    path: Path
    branch: str | None
    base_sha: str
    mode: str  # "worktree" | "direct"


def git_argv(cwd: Path, *args: str) -> list[str]:
    return ["git", "-C", str(cwd), *args]


def worktree_add_argv(project: Path, target: Path, branch: str, start_point: str) -> list[str]:
    return git_argv(project, "worktree", "add", "-b", branch, str(target), start_point)


def _run_git(argv: list[str], *, code: str = "worktree_failed") -> str:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise RunnerError(code, "git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(code, f"git timed out: {' '.join(argv[:5])}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise RunnerError(code, detail[-1] if detail else f"git exited {completed.returncode}")
    return completed.stdout.strip()


def _refuse_nesting(candidate: Path, paths: RunnerPaths) -> None:
    """Runs never nest inside the runner's own worktree area."""
    try:
        worktrees_root = paths.worktrees_root.resolve()
    except (OSError, RuntimeError):
        worktrees_root = paths.worktrees_root
    if candidate == worktrees_root or candidate.is_relative_to(worktrees_root):
        raise RunnerError(
            "invalid_project",
            f"{candidate} is inside the runner's own worktree area; "
            "start the run against the original repository instead",
        )


def resolve_project(raw: object, paths: RunnerPaths) -> Path:
    """Resolve *raw* to a contained git repository root.

    Accepts an absolute path or a name relative to the projects root, and
    returns the repository top level. Anything that escapes the projects root
    after symlink resolution is rejected.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise RunnerError("invalid_project", "project must be a non-empty string")
    if "\x00" in raw:
        raise RunnerError("invalid_project", "project contains a null byte")

    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = paths.projects_root / candidate

    try:
        resolved = candidate.resolve(strict=True)
        root = paths.projects_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RunnerError("invalid_project", f"project path does not exist: {raw}") from exc

    if not resolved.is_dir():
        raise RunnerError("invalid_project", f"project is not a directory: {raw}")
    if resolved == root or not resolved.is_relative_to(root):
        raise RunnerError("invalid_project", f"project must live under {root}")

    _refuse_nesting(resolved, paths)

    top_level = _run_git(
        git_argv(resolved, "rev-parse", "--show-toplevel"), code="not_a_git_repository"
    )
    repo_root = Path(top_level).resolve()
    if repo_root == root or not repo_root.is_relative_to(root):
        raise RunnerError("invalid_project", f"repository root must live under {root}")

    # A linked worktree can sit inside the projects root while its object store
    # lives outside it; commits would then land in an uncontained repository.
    common = git_common_dir(resolved).resolve()
    if not common.is_relative_to(root):
        raise RunnerError(
            "invalid_project",
            f"the repository's git object store lives outside {root}: {common}",
        )

    _refuse_nesting(repo_root, paths)
    return repo_root


def git_common_dir(project: Path) -> Path:
    """Absolute path of the repository's shared object store.

    For a linked worktree this points at the *original* repository, which is
    what a commit actually writes into — so it has to be contained too.
    """
    raw = _run_git(
        git_argv(project, "rev-parse", "--path-format=absolute", "--git-common-dir"),
        code="not_a_git_repository",
    )
    return Path(raw)


def head_sha(project: Path) -> str:
    """Current HEAD commit of *project*."""
    return _run_git(git_argv(project, "rev-parse", "HEAD"))


def worktree_path(paths: RunnerPaths, project: Path, run_id: str) -> Path:
    return paths.worktrees_root / project.name / run_id


def _refuse_unless_reusable(target: Path, project: Path, branch: str) -> None:
    """Raise unless git proves *target* is this run's own worktree.

    Being a repository root is not enough: a standalone repository that was
    initialised at that path, or a linked worktree of a *different* repository,
    would look identical from the outside while commits landed somewhere the
    run never meant to touch. Reuse therefore needs two proofs from git — the
    same shared object store as *project*, and *branch* checked out — and a
    target that fails either one is refused as it stands, never repaired.
    """
    if not (target / ".git").exists():
        raise RunnerError(
            "worktree_failed", f"{target} already exists and is not a git worktree"
        )
    try:
        top_level = Path(_run_git(git_argv(target, "rev-parse", "--show-toplevel"))).resolve()
        common = git_common_dir(target).resolve()
        current = _run_git(git_argv(target, "rev-parse", "--abbrev-ref", "HEAD"))
    except RunnerError as exc:
        # ``not_a_git_repository`` is reserved for the *project* argument;
        # from here a failed probe only ever condemns the target.
        raise RunnerError(
            "worktree_failed",
            f"{target} already exists and is not a git worktree: {exc.detail}",
        ) from exc

    if top_level != target.resolve():
        raise RunnerError(
            "worktree_failed", f"{target} already exists and is not a git worktree"
        )
    expected_common = git_common_dir(project).resolve()
    if common != expected_common:
        raise RunnerError(
            "worktree_failed",
            f"{target} already exists but belongs to a different repository: "
            f"its git directory is {common}, expected {expected_common}",
        )
    if current != branch:
        seen = "a detached HEAD" if current == "HEAD" else f"branch {current!r}"
        raise RunnerError(
            "worktree_failed",
            f"{target} already exists on {seen}, expected branch {branch!r}",
        )


def create_worktree(paths: RunnerPaths, project: Path, run_id: str) -> WorktreeInfo:
    """Create ``<worktrees_root>/<repo>/<run-id>`` on branch ``hermes/<short>``.

    Re-running for the same run id reuses the existing worktree untouched, but
    only once git has confirmed it belongs to *project* and sits on the run's
    branch. Anything else is an error, never a reset.
    """
    target = worktree_path(paths, project, run_id)
    branch = models.branch_for_run(run_id)
    base = head_sha(project)

    if target.exists():
        _refuse_unless_reusable(target, project, branch)
        return WorktreeInfo(path=target, branch=branch, base_sha=base, mode="worktree")

    target.parent.mkdir(parents=True, exist_ok=True)
    _run_git(worktree_add_argv(project, target, branch, base))
    return WorktreeInfo(path=target, branch=branch, base_sha=base, mode="worktree")


def direct_checkout(project: Path) -> WorktreeInfo:
    """Describe direct-checkout mode for ``create_worktree=false``."""
    return WorktreeInfo(path=project, branch=None, base_sha=head_sha(project), mode="direct")
