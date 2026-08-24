"""SQLite connection setup and schema migrations.

The daemon and every worker process open their own connection to the same
file, so WAL plus a generous ``busy_timeout`` are load-bearing rather than
cosmetic.

The file holds prompts, results and session identifiers, so it stays private
to the account that runs the daemon no matter which umask the caller brought
along.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

#: Modes for the runner's own state. Applied explicitly rather than left to
#: the umask, which an installer or a LaunchAgent may well have set to 022.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: SQLite keeps two companions next to a WAL database. They are recreated per
#: connection, but a crashed process leaves them behind for the next one.
SIDECAR_SUFFIXES = ("-wal", "-shm")

#: The group and world bits, which is all this module ever takes away.
_SHARED_BITS = 0o077

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    project            TEXT NOT NULL,
    role               TEXT NOT NULL,
    prompt             TEXT NOT NULL,
    hermes_session_id  TEXT,
    hermes_task_id     TEXT,
    status             TEXT NOT NULL,
    create_worktree    INTEGER NOT NULL DEFAULT 1,
    worktree           TEXT,
    branch             TEXT,
    base_sha           TEXT,
    claude_session_id  TEXT,
    worker_pid         INTEGER,
    worker_started_at  REAL,
    stop_requested     INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at         TEXT,
    finished_at        TEXT,
    result             TEXT,
    error              TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    direction    TEXT NOT NULL,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_pending ON messages(run_id, status, id);
"""


def _tighten(path: Path, /) -> None:
    """Drop the group and world bits from *path*, adding none.

    A path owned by another account is left alone: chmod would raise there,
    and a database this runner can still write is worth more than an error.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != os.geteuid() or not mode & _SHARED_BITS:
        return
    os.chmod(path, mode & ~_SHARED_BITS)


def _ensure_private_dir(directory: Path, /) -> None:
    """Create *directory* and any missing ancestor as a private directory."""
    missing: list[Path] = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for path in reversed(missing):
        path.mkdir(mode=DIR_MODE, exist_ok=True)
        os.chmod(path, DIR_MODE)  # mkdir's mode is filtered through the umask
    if not missing:
        # Only the directory holding the database, never an ancestor above it:
        # those belong to the account, not to the runner.
        _tighten(directory)


def _prepare_file(target: Path, /) -> None:
    """Make the database file exist and be private before SQLite opens it.

    SQLite copies the main database's permissions onto the ``-wal`` and
    ``-shm`` files it creates, so getting this in first is what keeps the
    sidecars of a fresh database private too.
    """
    if target.exists():
        _tighten(target)
        return
    os.close(os.open(target, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, FILE_MODE))
    os.chmod(target, FILE_MODE)  # the open mode is filtered through the umask


def connect(path: Path | str) -> sqlite3.Connection:
    """Open *path* with the pragmas the daemon/worker split relies on."""
    target = Path(path)
    _ensure_private_dir(target.parent)
    _prepare_file(target)
    conn = sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Sidecars inherited from a database that was world-readable when it was
    # created, or left behind by a crashed process, are reused rather than
    # recreated, so inheriting the mode is not enough on its own.
    for suffix in SIDECAR_SUFFIXES:
        _tighten(target.with_name(target.name + suffix))
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema; safe to call on every process start."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than this runner ({SCHEMA_VERSION})"
        )
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
