"""Project containment and git worktree lifecycle against real repositories."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_claude_runner import worktree
from hermes_claude_runner.config import RunnerPaths
from hermes_claude_runner.errors import RunnerError

from .conftest import git, make_repo, requires_git

pytestmark = requires_git


# ── containment ────────────────────────────────────────────────────────────

def test_resolves_a_repository_inside_the_projects_root(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    assert worktree.resolve_project("demo", paths) == repo.resolve()
    assert worktree.resolve_project(str(repo), paths) == repo.resolve()


def test_subdirectory_resolves_to_the_repository_root(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    (repo / "src" / "deep").mkdir(parents=True)
    assert worktree.resolve_project(str(repo / "src" / "deep"), paths) == repo.resolve()


def test_path_outside_projects_root_is_rejected(paths: RunnerPaths, tmp_path: Path) -> None:
    outside = make_repo(tmp_path / "outside")
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(outside), paths)
    assert exc.value.code == "invalid_project"


def test_symlink_escape_is_rejected(paths: RunnerPaths, tmp_path: Path) -> None:
    outside = make_repo(tmp_path / "elsewhere")
    (paths.projects_root / "sneaky").symlink_to(outside)
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(paths.projects_root / "sneaky"), paths)
    assert exc.value.code == "invalid_project"


def test_parent_traversal_is_rejected(paths: RunnerPaths, tmp_path: Path) -> None:
    make_repo(tmp_path / "outside")
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(paths.projects_root / ".." / "outside"), paths)
    assert exc.value.code == "invalid_project"


def test_projects_root_itself_is_rejected(paths: RunnerPaths) -> None:
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(paths.projects_root), paths)
    assert exc.value.code == "invalid_project"


def test_missing_directory_is_rejected(paths: RunnerPaths) -> None:
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(paths.projects_root / "nope"), paths)
    assert exc.value.code == "invalid_project"


def test_file_instead_of_directory_is_rejected(paths: RunnerPaths) -> None:
    target = paths.projects_root / "a-file"
    target.write_text("x")
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(target), paths)
    assert exc.value.code == "invalid_project"


def test_non_git_directory_is_rejected(paths: RunnerPaths) -> None:
    plain = paths.projects_root / "plain"
    plain.mkdir()
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(plain), paths)
    assert exc.value.code == "not_a_git_repository"


@pytest.mark.parametrize("bad", ["", "   ", None, 42, "\x00evil"])
def test_non_string_or_empty_project_is_rejected(paths: RunnerPaths, bad: object) -> None:
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(bad, paths)
    assert exc.value.code == "invalid_project"


# ── command construction ───────────────────────────────────────────────────

def test_worktree_add_argv_is_explicit_and_shell_free() -> None:
    argv = worktree.worktree_add_argv(
        Path("/p/demo"), Path("/wt/demo/r1"), "hermes/abc12345", "deadbeef"
    )
    assert argv == [
        "git", "-C", "/p/demo", "worktree", "add",
        "-b", "hermes/abc12345", "/wt/demo/r1", "deadbeef",
    ]
    assert all(isinstance(part, str) for part in argv)


def test_worktree_path_layout_matches_the_brief(paths: RunnerPaths) -> None:
    target = worktree.worktree_path(paths, Path("/anything/my-repo"), "rabc123")
    assert target == paths.worktrees_root / "my-repo" / "rabc123"


# ── lifecycle ──────────────────────────────────────────────────────────────

def test_create_worktree_makes_a_branch_from_head(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    head = git(repo, "rev-parse", "HEAD")

    info = worktree.create_worktree(paths, repo, "rabc12345")

    assert info.base_sha == head
    assert info.branch == "hermes/abc12345"
    assert info.path == paths.worktrees_root / "demo" / "rabc12345"
    assert (info.path / "README.md").read_text() == "hello\n"
    assert git(info.path, "rev-parse", "--abbrev-ref", "HEAD") == "hermes/abc12345"
    assert git(info.path, "rev-parse", "HEAD") == head


def test_create_worktree_never_touches_the_human_checkout(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    (repo / "dirty.txt").write_text("uncommitted work\n")
    before_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    info = worktree.create_worktree(paths, repo, "rabc12345")

    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == before_branch
    assert (repo / "dirty.txt").read_text() == "uncommitted work\n"
    assert not (info.path / "dirty.txt").exists()


def test_create_worktree_reports_base_sha_of_the_current_head(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    (repo / "second.txt").write_text("2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    head = git(repo, "rev-parse", "HEAD")
    assert worktree.create_worktree(paths, repo, "rabc12345").base_sha == head


def test_create_worktree_is_idempotent_for_the_same_run(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    first = worktree.create_worktree(paths, repo, "rabc12345")
    (first.path / "work.txt").write_text("progress\n")

    second = worktree.create_worktree(paths, repo, "rabc12345")

    assert second.path == first.path
    assert second.branch == first.branch
    assert (second.path / "work.txt").read_text() == "progress\n", "existing work destroyed"


def test_create_worktree_fails_closed_on_git_error(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    target = worktree.worktree_path(paths, repo, "rabc12345")
    target.mkdir(parents=True)
    (target / "stray").write_text("not a worktree")
    with pytest.raises(RunnerError) as exc:
        worktree.create_worktree(paths, repo, "rabc12345")
    assert exc.value.code == "worktree_failed"


def test_empty_repository_without_commits_is_rejected(paths: RunnerPaths) -> None:
    empty = paths.projects_root / "empty"
    empty.mkdir()
    os.system(f"git init -q -b main {empty}")  # noqa: S605 - fixture setup
    with pytest.raises(RunnerError) as exc:
        worktree.create_worktree(paths, empty, "rabc12345")
    assert exc.value.code == "worktree_failed"


def test_head_sha_reads_the_current_commit(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    assert worktree.head_sha(repo) == git(repo, "rev-parse", "HEAD")


def test_describe_direct_mode_reports_the_checkout(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    info = worktree.direct_checkout(repo)
    assert info.path == repo
    assert info.branch is None
    assert info.base_sha == git(repo, "rev-parse", "HEAD")
    assert info.mode == "direct"


# ── object-store containment ───────────────────────────────────────────────

def test_linked_worktree_pointing_outside_the_root_is_rejected(
    paths: RunnerPaths, tmp_path: Path
) -> None:
    """A worktree inside ~/Projects may still write into a repo outside it.

    ``--show-toplevel`` reports the contained worktree, but the object store
    lives wherever the real repository is, so commits would land outside.
    """
    outside = make_repo(tmp_path / "outside")
    sneaky = paths.projects_root / "sneaky-worktree"
    git(outside, "worktree", "add", "-q", str(sneaky))

    assert Path(git(sneaky, "rev-parse", "--show-toplevel")).resolve() == sneaky.resolve()
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(sneaky), paths)
    assert exc.value.code == "invalid_project"
    assert "object store" in exc.value.detail or "common" in exc.value.detail


def test_gitfile_pointing_outside_the_root_is_rejected(
    paths: RunnerPaths, tmp_path: Path
) -> None:
    outside = make_repo(tmp_path / "outside")
    smuggled = paths.projects_root / "smuggled"
    smuggled.mkdir()
    (smuggled / ".git").write_text(f"gitdir: {outside / '.git'}\n")
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(smuggled), paths)
    assert exc.value.code == "invalid_project"


def test_an_ordinary_repository_passes_the_object_store_check(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    assert worktree.resolve_project(str(repo), paths) == repo.resolve()


def test_git_common_dir_is_read_as_an_absolute_path(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    common = worktree.git_common_dir(repo)
    assert common.is_absolute()
    assert common.resolve() == (repo / ".git").resolve()


# ── no nesting inside the runner's own worktrees ───────────────────────────

def test_a_run_cannot_target_an_existing_hermes_worktree(paths: RunnerPaths) -> None:
    repo = make_repo(paths.projects_root / "demo")
    info = worktree.create_worktree(paths, repo, "rabc12345")
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(info.path), paths)
    assert exc.value.code == "invalid_project"
    assert "worktree" in exc.value.detail


def test_the_worktrees_root_itself_is_rejected(paths: RunnerPaths) -> None:
    paths.worktrees_root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RunnerError) as exc:
        worktree.resolve_project(str(paths.worktrees_root), paths)
    assert exc.value.code == "invalid_project"
