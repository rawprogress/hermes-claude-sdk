"""Run lifecycle vocabulary and identifier rules."""

from __future__ import annotations

import re
import uuid

STATUS_QUEUED = "queued"
STATUS_PREPARING = "preparing"
STATUS_WORKING = "working"
STATUS_COMPLETED = "completed"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"
STATUS_UNKNOWN = "unknown"

STATUSES = (
    STATUS_QUEUED,
    STATUS_PREPARING,
    STATUS_WORKING,
    STATUS_COMPLETED,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_STOPPED,
    STATUS_UNKNOWN,
)

TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_STOPPED, STATUS_UNKNOWN}
)
ACTIVE_STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_PREPARING, STATUS_WORKING, STATUS_BLOCKED}
)
# ``blocked`` is active (a live worker waits on the mailbox) but also
# resumable, because a worker that died while blocked leaves the run stranded.
RESUMABLE_STATUSES = frozenset(
    {STATUS_STOPPED, STATUS_FAILED, STATUS_BLOCKED, STATUS_COMPLETED, STATUS_UNKNOWN}
)

_RUN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ROLE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

DEFAULT_ROLE = "implementer"


def new_run_id() -> str:
    """Return a fresh run identifier."""
    return f"r{uuid.uuid4().hex[:20]}"


def is_valid_run_id(value: object) -> bool:
    return isinstance(value, str) and bool(_RUN_ID_RE.match(value))


def is_valid_role(value: object) -> bool:
    return isinstance(value, str) and bool(_ROLE_RE.match(value))


def short_run_id(run_id: str) -> str:
    """Eight-character form used in branch names."""
    return run_id[1:9] if run_id.startswith("r") and len(run_id) > 8 else run_id[:8]


def branch_for_run(run_id: str) -> str:
    return f"hermes/{short_run_id(run_id)}"
