import pytest

from hermes_claude_runner import models


def test_lifecycle_statuses_match_the_brief() -> None:
    assert models.STATUSES == (
        "queued", "preparing", "working", "completed",
        "blocked", "failed", "stopped", "unknown",
    )
    assert models.TERMINAL_STATUSES == frozenset({"completed", "failed", "stopped", "unknown"})
    assert models.ACTIVE_STATUSES == frozenset({"queued", "preparing", "working", "blocked"})
    assert models.RESUMABLE_STATUSES == frozenset(
        {"stopped", "failed", "blocked", "completed", "unknown"}
    )


def test_new_run_id_is_unique_and_url_safe() -> None:
    ids = {models.new_run_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(models.is_valid_run_id(i) for i in ids)


def test_short_run_id_is_stable_and_branch_safe() -> None:
    run_id = models.new_run_id()
    short = models.short_run_id(run_id)
    assert short == models.short_run_id(run_id)
    assert len(short) == 8
    assert models.branch_for_run(run_id) == f"hermes/{short}"


@pytest.mark.parametrize("bad", ["", "..", "a/b", "x" * 200, "has space", "semi;colon", None, 5])
def test_invalid_run_ids_are_rejected(bad: object) -> None:
    assert not models.is_valid_run_id(bad)


@pytest.mark.parametrize("role", ["implementer", "reviewer", "my_role-2"])
def test_valid_roles(role: str) -> None:
    assert models.is_valid_role(role)


@pytest.mark.parametrize("bad", ["", "role with space", "role/slash", "x" * 65, None])
def test_invalid_roles(bad: object) -> None:
    assert not models.is_valid_role(bad)
