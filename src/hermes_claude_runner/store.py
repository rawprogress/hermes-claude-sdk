"""Durable state access.

Writers use ``BEGIN IMMEDIATE`` so the daemon and the workers can share the
file without losing an event sequence number to a lost update.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from . import db, models

_RUN_COLUMNS = (
    "run_id", "project", "role", "prompt", "hermes_session_id", "hermes_task_id",
    "status", "create_worktree", "worktree", "branch", "base_sha",
    "claude_session_id", "worker_pid", "worker_started_at", "stop_requested",
    "created_at", "updated_at", "started_at", "finished_at", "result", "error",
)
_UPDATABLE_COLUMNS = frozenset(_RUN_COLUMNS) - {"run_id", "created_at", "updated_at"}
_BOOL_COLUMNS = ("create_worktree", "stop_requested")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _row_to_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    run = {key: row[key] for key in row.keys()}
    for key in _BOOL_COLUMNS:
        if key in run:
            run[key] = bool(run[key])
    return run


class Store:
    """Thin data-access layer over one SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: Path | str) -> Store:
        conn = db.connect(path)
        db.migrate(conn)
        return cls(conn)

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._rollback()
            raise
        try:
            self._conn.execute("COMMIT")
        except BaseException:
            # A failed COMMIT leaves the transaction open; roll it back so the
            # connection stays usable and the caller sees the real error.
            self._rollback()
            raise

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - secondary failure, never masks the first
            pass

    # ── runs ───────────────────────────────────────────────────────────────

    def create_run(
        self,
        *,
        run_id: str,
        project: str,
        role: str,
        prompt: str,
        hermes_session_id: str | None = None,
        hermes_task_id: str | None = None,
        create_worktree: bool = True,
        status: str = models.STATUS_QUEUED,
    ) -> dict[str, Any]:
        now = _now()
        try:
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO runs (run_id, project, role, prompt, hermes_session_id,"
                    " hermes_task_id, status, create_worktree, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (run_id, project, role, prompt, hermes_session_id, hermes_task_id,
                     status, int(create_worktree), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"run {run_id!r} already exists") from exc
        run = self.get_run(run_id)
        assert run is not None
        return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _row_to_run(row)

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - _UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"unknown run column(s): {sorted(unknown)}")
        if not fields:
            return self.get_run(run_id)

        values = dict(fields)
        for key in _BOOL_COLUMNS:
            if key in values:
                values[key] = int(bool(values[key]))
        now = _now()
        status = values.get("status")
        if status in models.TERMINAL_STATUSES:
            values.setdefault("finished_at", now)

        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._write() as conn:
            cursor = conn.execute(
                f"UPDATE runs SET {assignments}, updated_at = ? WHERE run_id = ?",
                (*values.values(), now, run_id),
            )
            if cursor.rowcount == 0:
                return None
            if status == models.STATUS_WORKING:
                # First transition into ``working`` wins; a resume keeps the original.
                conn.execute(
                    "UPDATE runs SET started_at = ? WHERE run_id = ? AND started_at IS NULL",
                    (now, run_id),
                )
        return self.get_run(run_id)

    def list_runs(
        self,
        project: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit)))
        rows = self._conn.execute(
            f"SELECT * FROM runs{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            params,
        ).fetchall()
        return [run for run in (_row_to_run(row) for row in rows) if run is not None]

    def active_runs(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in models.ACTIVE_STATUSES)
        rows = self._conn.execute(
            f"SELECT * FROM runs WHERE status IN ({placeholders}) ORDER BY created_at",
            tuple(sorted(models.ACTIVE_STATUSES)),
        ).fetchall()
        return [run for run in (_row_to_run(row) for row in rows) if run is not None]

    def request_stop(self, run_id: str) -> bool:
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE runs SET stop_requested = 1, updated_at = ? WHERE run_id = ?",
                (_now(), run_id),
            )
            return cursor.rowcount > 0

    def claim_for_resume(self, run_id: str) -> bool:
        """Atomically move a resumable run into ``preparing``.

        Returns False when the run is not resumable — including when a
        concurrent resume already claimed it, which is what stops two workers
        from being spawned for the same run.
        """
        placeholders = ",".join("?" for _ in models.RESUMABLE_STATUSES)
        with self._write() as conn:
            cursor = conn.execute(
                # The previous worker's identity has to go with the claim:
                # a reconcile sweep landing before the new spawn would
                # otherwise see a dead pid past its grace period and declare
                # the run unknown for no reason.
                f"UPDATE runs SET status = ?, stop_requested = 0, error = NULL,"
                f" finished_at = NULL, worker_pid = NULL, worker_started_at = NULL,"
                f" updated_at = ?"
                f" WHERE run_id = ? AND status IN ({placeholders})",
                (models.STATUS_PREPARING, _now(), run_id,
                 *sorted(models.RESUMABLE_STATUSES)),
            )
            return cursor.rowcount > 0

    def clear_stop(self, run_id: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE runs SET stop_requested = 0, updated_at = ? WHERE run_id = ?",
                (_now(), run_id),
            )

    # ── events ─────────────────────────────────────────────────────────────

    def append_event(self, run_id: str, kind: str, payload: Mapping[str, Any]) -> int:
        blob = json.dumps(payload, ensure_ascii=False, default=str)
        with self._write() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO events (run_id, seq, kind, payload, created_at) VALUES (?,?,?,?,?)",
                (run_id, seq, kind, blob, _now()),
            )
        return int(seq)

    def get_events(self, run_id: str, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT seq, kind, payload, created_at FROM events"
            " WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, int(after), max(1, int(limit))),
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def event_high_water(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row[0])

    # ── mailbox ────────────────────────────────────────────────────────────

    def enqueue_message(self, run_id: str, body: str, direction: str = "inbound") -> int:
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (run_id, direction, body, status, created_at)"
                " VALUES (?,?,?,'pending',?)",
                (run_id, direction, body, _now()),
            )
        return int(cursor.lastrowid or 0)

    def take_pending_messages(self, run_id: str) -> list[dict[str, Any]]:
        """Atomically claim every pending inbound message for *run_id*."""
        now = _now()
        with self._write() as conn:
            rows = conn.execute(
                "SELECT id, body, created_at FROM messages"
                " WHERE run_id = ? AND status = 'pending' ORDER BY id",
                (run_id,),
            ).fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE messages SET status='delivered', delivered_at=?"
                f" WHERE id IN ({placeholders})",
                (now, *ids),
            )
        return [{"id": r["id"], "body": r["body"], "created_at": r["created_at"]} for r in rows]

    def enqueue_if_active(self, run_id: str, body: str) -> tuple[bool, str | None]:
        """Queue *body* only while the run can still consume it.

        Returns ``(queued, status)``. A finished run refuses the message rather
        than storing a follow-up nobody will ever read.
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return False, None
            status = str(row["status"])
            if status in models.TERMINAL_STATUSES:
                return False, status
            conn.execute(
                "INSERT INTO messages (run_id, direction, body, status, created_at)"
                " VALUES (?, 'inbound', ?, 'pending', ?)",
                (run_id, body, _now()),
            )
            return True, status

    def claim_pending_or_finalize(
        self, run_id: str, *, result: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Take the mailbox, or complete the run — in one transaction.

        This is the other half of :meth:`enqueue_if_active`: because both take
        ``BEGIN IMMEDIATE`` on the same database, a follow-up can never be
        queued into a run that has just completed, and a run can never complete
        while a follow-up is landing.

        Returns ``(messages, final_status)``. ``final_status`` is ``None`` when
        messages were claimed, ``"completed"`` when the run was finalized here,
        ``"stopped"`` when a stop is pending, and ``"unknown"`` when the run is
        gone. Only the ``"completed"`` case writes a status.
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT status, stop_requested FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return [], models.STATUS_UNKNOWN
            if row["stop_requested"]:
                return [], models.STATUS_STOPPED

            rows = conn.execute(
                "SELECT id, body, created_at FROM messages"
                " WHERE run_id = ? AND status = 'pending' ORDER BY id",
                (run_id,),
            ).fetchall()
            now = _now()
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE messages SET status='delivered', delivered_at=?"
                    f" WHERE id IN ({placeholders})",
                    (now, *ids),
                )
                return (
                    [{"id": r["id"], "body": r["body"], "created_at": r["created_at"]}
                     for r in rows],
                    None,
                )

            terminal = ",".join("?" for _ in models.TERMINAL_STATUSES)
            conn.execute(
                # COALESCE: a resumed run that produced no new result keeps the
                # evidence from its earlier turns.
                f"UPDATE runs SET status = ?, result = COALESCE(?, result),"
                f" finished_at = ?, updated_at = ?"
                f" WHERE run_id = ? AND status NOT IN ({terminal})",
                (models.STATUS_COMPLETED, result, now, now, run_id,
                 *sorted(models.TERMINAL_STATUSES)),
            )
            return [], models.STATUS_COMPLETED

    def pending_message_count(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE run_id = ? AND status = 'pending'", (run_id,)
        ).fetchone()
        return int(row[0])
