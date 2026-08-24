import pytest

from hermes_claude_runner.errors import ERROR_CODES, RunnerError


def test_runner_error_carries_stable_code_and_detail() -> None:
    err = RunnerError("unknown_run", "no run with id r1")
    assert err.code == "unknown_run"
    assert err.detail == "no run with id r1"
    assert "unknown_run" in str(err)
    assert err.to_envelope() == {
        "ok": False, "error": "unknown_run", "detail": "no run with id r1",
    }


def test_every_declared_code_is_snake_case_and_stable() -> None:
    assert {"invalid_request", "invalid_action", "invalid_params", "unknown_run",
            "invalid_project", "not_a_git_repository", "worktree_failed",
            "daemon_unavailable", "run_not_resumable", "no_claude_session",
            "secret_in_payload", "run_not_live", "internal_error"} <= ERROR_CODES
    assert all(c.islower() and " " not in c for c in ERROR_CODES)


def test_unknown_code_is_rejected_so_typos_cannot_ship() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        RunnerError("oops_not_declared", "x")
