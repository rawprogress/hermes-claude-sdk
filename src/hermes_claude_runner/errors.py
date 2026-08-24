"""Stable error codes shared by the RPC surface and the Hermes plugin.

The codes are part of the wire contract: Hermes branches on them, so they
must never be renamed silently. ``RunnerError`` refuses undeclared codes.
"""

from __future__ import annotations

from typing import Any

ERROR_CODES = frozenset({
    "invalid_request",       # stdin was not a single JSON object
    "invalid_action",        # unknown or missing action
    "invalid_params",        # a parameter failed type/range validation
    "unknown_run",           # no run with that id
    "invalid_project",       # missing, outside the projects root, or escaping via symlink
    "not_a_git_repository",  # inside the projects root but not a git checkout
    "worktree_failed",       # git worktree add / rev-parse failed
    "daemon_unavailable",    # the LaunchAgent daemon is not accepting connections
    "run_not_resumable",     # run is still live, or has no session to resume
    "no_claude_session",     # resume requested before a Claude session id was captured
    "worker_spawn_failed",   # the daemon could not launch the worker process
    "secret_in_payload",     # the prompt or message carried credential-shaped material
    "run_not_live",          # send needs a live worker; this run has already finished
    "internal_error",        # unexpected failure; detail is redacted
})


class RunnerError(Exception):
    """An error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def to_envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "detail": self.detail}
