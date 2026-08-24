"""Request validation, handlers and response envelopes.

Every reply is exactly one of ``{"ok": true, "result": {...}}`` or
``{"ok": false, "error": "stable_code", "detail": "human readable"}``.
Unknown actions, unknown runs and uncontained projects all fail closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from . import db, events, models, worktree
from .config import RunnerPaths
from .errors import RunnerError
from .redaction import find_secrets, redact_text
from .store import Store

__version__ = "0.2.0"

ACTIONS = frozenset({"start", "send", "status", "events", "list", "stop", "resume", "health"})

MAX_PROMPT_CHARS = 100_000
MAX_MESSAGE_CHARS = 100_000
MAX_IDENTIFIER_CHARS = 200
MAX_EVENT_LIMIT = 500
MAX_LIST_LIMIT = 200
MAX_SUMMARY_CHARS = 500
MAX_RESPONSE_BYTES = 256_000


def serialize_response(envelope: Any) -> str:
    """Render one envelope as a single compact JSON line."""
    return json.dumps(envelope, ensure_ascii=False, default=str, separators=(",", ":"))


# Compact separators make this 1, but deriving it keeps the accounting exact
# if serialize_response ever changes its formatting.
_LIST_SEPARATOR_BYTES = (
    len(serialize_response([0, 0])) - len(serialize_response([0])) - 1
)


class Spawner(Protocol):
    """Launches, inspects and signals per-run worker processes."""

    def spawn(self, run_id: str, *, resume: str | None = None) -> tuple[int, float]: ...

    def is_alive(
        self, pid: int | None, started_at: float | None, run_id: str | None = None
    ) -> bool: ...

    def signal_stop(
        self, pid: int | None, started_at: float | None, run_id: str | None = None
    ) -> bool: ...


@dataclass
class Runtime:
    """Everything a handler needs; injected so tests never touch the real system."""

    paths: RunnerPaths
    store: Store
    spawner: Spawner


# ── validation helpers ─────────────────────────────────────────────────────

def _require_text(value: object, field: str, *, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunnerError("invalid_params", f"{field} must be a non-empty string")
    if len(value) > max_chars:
        raise RunnerError("invalid_params", f"{field} exceeds {max_chars} characters")
    return value


def _refuse_secrets(text: str, field: str) -> str:
    """Reject credential-shaped payloads before anything durable is written.

    Redacting instead would hand Claude a corrupted prompt, so the runner
    refuses the call and says which kind it recognised.
    """
    kinds = find_secrets(text)
    if kinds:
        raise RunnerError(
            "secret_in_payload",
            f"{field} contains credential-shaped material ({', '.join(kinds)}); "
            "pass secrets through the environment or a file, never through Hermes",
        )
    return text


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RunnerError("invalid_params", f"{field} must be a string or null")
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise RunnerError("invalid_params", f"{field} exceeds {MAX_IDENTIFIER_CHARS} characters")
    return value or None


def _require_bool(value: object, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise RunnerError("invalid_params", f"{field} must be a boolean")
    return value


def _require_run_id(request: dict[str, Any]) -> str:
    run_id = request.get("run_id")
    if not models.is_valid_run_id(run_id):
        raise RunnerError("invalid_params", "run_id must be a short identifier string")
    assert isinstance(run_id, str)
    return run_id


def _require_index(value: object, field: str, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunnerError("invalid_params", f"{field} must be an integer")
    if value < 0:
        raise RunnerError("invalid_params", f"{field} must not be negative")
    return min(value, maximum)


def _load_run(runtime: Runtime, run_id: str) -> dict[str, Any]:
    run = runtime.store.get_run(run_id)
    if run is None:
        raise RunnerError("unknown_run", f"no run with id {run_id}")
    return run


def _summary(text: str | None) -> str:
    """Bound and scrub any text before it is persisted or returned.

    Operator-supplied prompts and follow-ups can carry credentials just as
    easily as tool output, so they take the same path.
    """
    return events.truncate(redact_text(text or ""), MAX_SUMMARY_CHARS)[0]


# ── handlers ───────────────────────────────────────────────────────────────

def _handle_health(_request: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "schema_version": db.SCHEMA_VERSION,
        "pid": os.getpid(),
        "db_path": str(runtime.paths.db_path),
        "socket_path": str(runtime.paths.socket_path),
        "projects_root": str(runtime.paths.projects_root),
        "active_runs": len(runtime.store.active_runs()),
    }


def _handle_start(request: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    prompt = _refuse_secrets(
        _require_text(request.get("prompt"), "prompt", max_chars=MAX_PROMPT_CHARS), "prompt"
    )
    role = request.get("role") or models.DEFAULT_ROLE
    if not models.is_valid_role(role):
        raise RunnerError("invalid_params", "role must match [A-Za-z0-9_-]{1,64}")
    create_worktree = _require_bool(request.get("create_worktree"), "create_worktree", True)
    hermes_session_id = _optional_identifier(request.get("hermes_session_id"),
                                             "hermes_session_id")
    hermes_task_id = _optional_identifier(request.get("hermes_task_id"), "hermes_task_id")

    project = worktree.resolve_project(request.get("project"), runtime.paths)

    run_id = models.new_run_id()
    runtime.store.create_run(
        run_id=run_id, project=str(project), role=role, prompt=prompt,
        hermes_session_id=hermes_session_id, hermes_task_id=hermes_task_id,
        create_worktree=create_worktree,
    )
    runtime.store.append_event(run_id, "run_created", {
        "project": str(project), "role": role, "create_worktree": create_worktree,
        "hermes_session_id": hermes_session_id, "hermes_task_id": hermes_task_id,
    })
    runtime.store.update_run(run_id, status=models.STATUS_PREPARING)

    try:
        info = (
            worktree.create_worktree(runtime.paths, project, run_id)
            if create_worktree
            else worktree.direct_checkout(project)
        )
    except RunnerError as exc:
        runtime.store.append_event(run_id, "error", {"code": exc.code, "detail": exc.detail})
        runtime.store.update_run(run_id, status=models.STATUS_FAILED, error=exc.detail)
        raise

    runtime.store.update_run(
        run_id, worktree=str(info.path), branch=info.branch, base_sha=info.base_sha
    )
    runtime.store.append_event(run_id, "worktree_ready", {
        "mode": info.mode, "path": str(info.path), "branch": info.branch,
        "base_sha": info.base_sha,
    })
    runtime.store.append_event(run_id, "prompt", {"text": _summary(prompt)})

    _spawn(runtime, run_id, resume=None)

    run = _load_run(runtime, run_id)
    return {
        "run_id": run_id,
        "status": run["status"],
        "worktree": str(info.path),
        "branch": info.branch,
        "base_sha": info.base_sha,
        "mode": info.mode,
        "project": str(project),
        "role": role,
    }


def _spawn(runtime: Runtime, run_id: str, *, resume: str | None) -> None:
    try:
        pid, started_at = runtime.spawner.spawn(run_id, resume=resume)
    except Exception as exc:  # noqa: BLE001 - normalized into a stable envelope
        detail = redact_text(str(exc)) or exc.__class__.__name__
        runtime.store.append_event(run_id, "error",
                                   {"code": "worker_spawn_failed", "detail": detail})
        runtime.store.update_run(run_id, status=models.STATUS_FAILED, error=detail)
        raise RunnerError("worker_spawn_failed", detail) from exc
    runtime.store.update_run(run_id, worker_pid=pid, worker_started_at=started_at)


def _handle_send(request: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    run_id = _require_run_id(request)
    message = _refuse_secrets(
        _require_text(request.get("message"), "message", max_chars=MAX_MESSAGE_CHARS), "message"
    )
    _load_run(runtime, run_id)

    queued, status = runtime.store.enqueue_if_active(run_id, message)
    if not queued:
        raise RunnerError(
            "run_not_live",
            f"run is {status}; nothing would read this message — use claude_resume",
        )
    runtime.store.append_event(run_id, "follow_up", {"text": _summary(message)})

    # Report the state as it is *after* the enqueue, not as it was before.
    refreshed = _load_run(runtime, run_id)
    return {
        "queued": True,
        "run_id": run_id,
        "status": refreshed["status"],
        "pending_messages": runtime.store.pending_message_count(run_id),
    }


def _activity(runtime: Runtime, run_id: str) -> dict[str, Any] | None:
    high_water = runtime.store.event_high_water(run_id)
    if high_water == 0:
        return None
    last = runtime.store.get_events(run_id, after=high_water - 1, limit=1)
    if not last:
        return None
    event = last[0]
    return {"kind": event["kind"], "seq": event["seq"], "at": event["created_at"]}


def _handle_status(request: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    run_id = _require_run_id(request)
    run = _load_run(runtime, run_id)
    return {
        "run_id": run_id,
        "status": run["status"],
        "project": run["project"],
        "role": run["role"],
        "prompt": _summary(run["prompt"]),
        "mode": "worktree" if run["create_worktree"] else "direct",
        "worktree": run["worktree"],
        "branch": run["branch"],
        "base_sha": run["base_sha"],
        "claude_session_id": run["claude_session_id"],
        "hermes_session_id": run["hermes_session_id"],
        "hermes_task_id": run["hermes_task_id"],
        "worker_pid": run["worker_pid"],
        "stop_requested": run["stop_requested"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "result": _summary(run["result"]) if run["result"] else None,
        "error": _summary(run["error"]) if run["error"] else None,
        "event_high_water": runtime.store.event_high_water(run_id),
        "pending_messages": runtime.store.pending_message_count(run_id),
        "activity": _activity(runtime, run_id),
    }


def _handle_events(request: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    run_id = _require_run_id(request)
    after = _require_index(request.get("after"), "after", default=0, maximum=2**53)
    limit = _require_index(request.get("limit"), "limit", default=100, maximum=MAX_EVENT_LIMIT)
    _load_run(runtime, run_id)

    selected = runtime.store.get_events(run_id, after=after, limit=max(1, limit))
    high_water = runtime.store.event_high_water(run_id)

    # Everything around the events counts against the same cap, so measure the
    # envelope once with an empty list and give the events what is left. The
    # widest plausible cursor and the longer boolean keep this an upper bound.
    skeleton = {
        "run_id": run_id,
        "events": [],
        "next_cursor": max(after, high_water),
        "high_water": high_water,
        "truncated": False,
    }
    overhead = len(serialize_response({"ok": True, "result": skeleton}).encode())
    selected, truncated = _fit_events(selected, MAX_RESPONSE_BYTES - overhead)

    next_cursor = selected[-1]["seq"] if selected else max(after, high_water)
    return {
        "run_id": run_id,
        "events": selected,
        "next_cursor": next_cursor,
        "high_water": high_water,
        "truncated": truncated,
    }


def _fit_events(
    events: list[dict[str, Any]], max_bytes: int
) -> tuple[list[dict[str, Any]], bool]:
    """Return the longest prefix of *events* that fits in *max_bytes*.

    Sizing uses the same serialization the response goes out with, so the
    bound holds on the wire rather than on an approximation. Each event is
    sized exactly once: dropping the tail and re-serializing the remaining
    list would cost O(n^2) bytes on a large page.

    A first event that cannot fit on its own is still delivered, with its
    payload replaced by a marker, so the caller's cursor can always advance.
    """
    if not events:
        return [], False

    total = 0
    kept = 0
    for index, event in enumerate(events):
        addition = len(serialize_response(event).encode()) + (
            _LIST_SEPARATOR_BYTES if index else 0
        )
        if total + addition > max_bytes:
            break
        total += addition
        kept += 1

    if kept == len(events):
        return events, False
    if kept:
        return events[:kept], True

    stub = dict(events[0])
    stub["payload"] = {"truncated": True, "kind": stub["kind"]}
    return [stub], True


def _handle_list(request: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    status = request.get("status")
    if status is not None:
        if not isinstance(status, str) or status not in models.STATUSES:
            raise RunnerError("invalid_params", f"status must be one of {list(models.STATUSES)}")
    project = request.get("project")
    project_filter: str | None = None
    if project is not None:
        project_filter = str(worktree.resolve_project(project, runtime.paths))
    limit = _require_index(request.get("limit"), "limit", default=50, maximum=MAX_LIST_LIMIT)
    limit = max(1, limit)

    runs = runtime.store.list_runs(project=project_filter, status=status, limit=limit)
    return {
        "runs": [
            {
                "run_id": run["run_id"],
                "project": run["project"],
                "role": run["role"],
                "status": run["status"],
                "prompt": _summary(run["prompt"]),
                "worktree": run["worktree"],
                "branch": run["branch"],
                "claude_session_id": run["claude_session_id"],
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
            }
            for run in runs
        ],
        "count": len(runs),
        "limit": limit,
    }


def _handle_stop(request: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    run_id = _require_run_id(request)
    run = _load_run(runtime, run_id)
    if run["status"] in models.TERMINAL_STATUSES:
        return {
            "run_id": run_id, "status": run["status"], "stop_requested": run["stop_requested"],
            "already_final": True,
        }
    runtime.store.request_stop(run_id)
    # The flag alone would only land on the worker's next poll; signalling it
    # makes a stop prompt without touching the worktree or the session.
    signalled = runtime.spawner.signal_stop(
        run["worker_pid"], run["worker_started_at"], run_id
    )
    runtime.store.append_event(run_id, "stop_requested",
                               {"from_status": run["status"], "signalled": signalled})
    return {
        "run_id": run_id,
        "status": runtime.store.get_run(run_id)["status"],  # type: ignore[index]
        "stop_requested": True,
        "signalled": signalled,
        "already_final": False,
        "worktree": run["worktree"],
    }


def _handle_resume(request: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    run_id = _require_run_id(request)
    message = _refuse_secrets(
        _require_text(request.get("message"), "message", max_chars=MAX_MESSAGE_CHARS), "message"
    )
    run = _load_run(runtime, run_id)

    if run["status"] not in models.RESUMABLE_STATUSES:
        raise RunnerError("run_not_resumable", f"run is {run['status']}; stop it first")

    # A worker can still be alive under any resumable status — a stop that has
    # not landed yet, a blocked run waiting on the mailbox, or an ``unknown``
    # run whose process turned out to be fine. Spawning a second one would give
    # the same worktree two writers.
    if runtime.spawner.is_alive(run["worker_pid"], run["worker_started_at"], run_id):
        raise RunnerError(
            "run_not_resumable",
            "a worker is still running for this run; use send, or stop it first",
        )

    session_id = run["claude_session_id"]
    if not session_id:
        raise RunnerError("no_claude_session", "no Claude session id was captured for this run")

    # Claim the run before spawning: two concurrent resumes must not both win.
    if not runtime.store.claim_for_resume(run_id):
        raise RunnerError("run_not_resumable", "another resume already claimed this run")

    runtime.store.enqueue_message(run_id, message)
    runtime.store.append_event(run_id, "follow_up", {"text": _summary(message), "resume": True})
    _spawn(runtime, run_id, resume=session_id)

    refreshed = _load_run(runtime, run_id)
    return {
        "run_id": run_id,
        "status": refreshed["status"],
        "claude_session_id": session_id,
        "worktree": refreshed["worktree"],
        "branch": refreshed["branch"],
        "base_sha": refreshed["base_sha"],
        "resumed": True,
    }


_HANDLERS = {
    "health": _handle_health,
    "start": _handle_start,
    "send": _handle_send,
    "status": _handle_status,
    "events": _handle_events,
    "list": _handle_list,
    "stop": _handle_stop,
    "resume": _handle_resume,
}


# ── entry points ───────────────────────────────────────────────────────────

def parse_request(raw: str) -> dict[str, Any]:
    """Parse one JSON request object, or raise ``invalid_request``."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise RunnerError("invalid_request", "stdin must contain one JSON object") from exc
    if not isinstance(parsed, dict):
        raise RunnerError("invalid_request", "request must be a JSON object")
    return parsed


def handle_request(request: Any, runtime: Runtime) -> dict[str, Any]:
    """Dispatch one validated request and return a response envelope."""
    try:
        if not isinstance(request, dict):
            raise RunnerError("invalid_request", "request must be a JSON object")
        action = request.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise RunnerError("invalid_action", f"action must be one of {sorted(ACTIONS)}")
        return {"ok": True, "result": _HANDLERS[action](request, runtime)}
    except RunnerError as exc:
        return exc.to_envelope()
    except Exception as exc:  # noqa: BLE001 - the RPC surface never leaks a traceback
        detail = redact_text(f"{exc.__class__.__name__}: {exc}")
        return RunnerError("internal_error", events.truncate(detail, 500)[0]).to_envelope()


__all__ = [
    "ACTIONS", "MAX_EVENT_LIMIT", "MAX_LIST_LIMIT", "MAX_MESSAGE_CHARS", "MAX_PROMPT_CHARS",
    "MAX_RESPONSE_BYTES", "MAX_SUMMARY_CHARS", "Runtime", "RunnerError", "Spawner",
    "handle_request", "parse_request", "serialize_response",
]
