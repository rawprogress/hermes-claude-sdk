"""Schema, migrations and the concurrency pragmas the daemon/worker split needs."""

import sqlite3
from pathlib import Path

from hermes_claude_runner import db


def test_connect_enables_wal_and_busy_timeout(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "data.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_connect_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "data.db"
    db.connect(target).close()
    assert target.exists()


def test_rows_are_mappings(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "data.db")
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1
    conn.close()


def test_migrate_creates_tables_and_stamps_version(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "data.db")
    db.migrate(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "events", "messages"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("INSERT INTO runs (run_id, project, role, prompt, status) VALUES (?,?,?,?,?)",
                 ("r1", "/p", "implementer", "do", "queued"))
    conn.commit()
    conn.close()

    conn = db.connect(path)
    db.migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_runs_table_has_every_field_the_brief_requires(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "data.db")
    db.migrate(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {
        "run_id", "project", "role", "prompt", "hermes_session_id", "hermes_task_id",
        "status", "create_worktree", "worktree", "branch", "base_sha",
        "claude_session_id", "worker_pid", "worker_started_at", "stop_requested",
        "created_at", "updated_at", "started_at", "finished_at", "result", "error",
    } <= cols
    conn.close()


def test_events_are_unique_per_run_sequence(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "data.db")
    db.migrate(conn)
    conn.execute("INSERT INTO runs (run_id, project, role, prompt, status) VALUES (?,?,?,?,?)",
                 ("r1", "/p", "implementer", "do", "queued"))
    conn.execute("INSERT INTO events (run_id, seq, kind, payload) VALUES (?,?,?,?)",
                 ("r1", 1, "system", "{}"))
    try:
        conn.execute("INSERT INTO events (run_id, seq, kind, payload) VALUES (?,?,?,?)",
                     ("r1", 1, "system", "{}"))
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("duplicate (run_id, seq) must be rejected")
    conn.close()


def test_events_and_messages_reference_runs(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "data.db")
    db.migrate(conn)
    for sql in (
        "INSERT INTO events (run_id, seq, kind, payload) VALUES ('ghost',1,'system','{}')",
        "INSERT INTO messages (run_id, direction, body, status) "
        "VALUES ('ghost','inbound','hi','pending')",
    ):
        try:
            conn.execute(sql)
        except sqlite3.IntegrityError:
            continue
        raise AssertionError(f"foreign key not enforced for: {sql}")
    conn.close()


def test_second_connection_can_write_while_first_reads(tmp_path: Path) -> None:
    # WAL is what lets the daemon read while a worker writes.
    path = tmp_path / "data.db"
    first = db.connect(path)
    db.migrate(first)
    first.commit()
    second = db.connect(path)
    list(first.execute("SELECT * FROM runs"))
    second.execute("INSERT INTO runs (run_id, project, role, prompt, status) VALUES (?,?,?,?,?)",
                   ("r1", "/p", "implementer", "do", "queued"))
    second.commit()
    assert first.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    first.close()
    second.close()
