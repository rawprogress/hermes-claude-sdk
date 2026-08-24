"""Schema, migrations and the concurrency pragmas the daemon/worker split needs."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

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


# ── private filesystem permissions ─────────────────────────────────────────

# The database holds prompts, results and session identifiers, so it belongs
# to the account running the daemon and to nobody else. The umask a caller
# happens to have is not part of that guarantee: these tests run under a
# deliberately permissive 022 and still expect 0700 directories and 0600
# files.


@contextmanager
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def sidecars(target: Path) -> list[Path]:
    return [target.with_name(target.name + suffix) for suffix in ("-wal", "-shm")]


def test_created_state_directories_are_private(tmp_path: Path) -> None:
    target = tmp_path / "state" / "nested" / "data.db"
    with permissive_umask():
        db.connect(target).close()
    assert mode_of(tmp_path / "state") == 0o700
    assert mode_of(tmp_path / "state" / "nested") == 0o700


def test_created_database_files_are_private(tmp_path: Path) -> None:
    target = tmp_path / "state" / "data.db"
    with permissive_umask():
        conn = db.connect(target)
        db.migrate(conn)
        present = [path for path in sidecars(target) if path.exists()]
        assert [path.name for path in present] == [f"data.db{s}" for s in ("-wal", "-shm")]
        modes = {path.name: mode_of(path) for path in (target, *present)}
        conn.close()
    assert modes == {"data.db": 0o600, "data.db-wal": 0o600, "data.db-shm": 0o600}


def test_existing_group_and_world_readable_state_is_tightened(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = state / "data.db"
    conn = db.connect(target)
    db.migrate(conn)
    conn.execute("INSERT INTO runs (run_id, project, role, prompt, status) VALUES (?,?,?,?,?)",
                 ("r1", "/p", "implementer", "do", "queued"))
    conn.commit()
    conn.close()
    state.chmod(0o755)
    target.chmod(0o644)

    with permissive_umask():
        conn = db.connect(target)

    # Tightening is a chmod, never a rewrite: the row survives it.
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert mode_of(state) == 0o700
    assert mode_of(target) == 0o600
    conn.close()


def test_sidecars_left_by_another_connection_are_tightened(tmp_path: Path) -> None:
    # A crashed worker can leave a world-readable -wal behind; SQLite reuses
    # such a file instead of recreating it, so opening again has to fix it.
    target = tmp_path / "state" / "data.db"
    first = db.connect(target)
    db.migrate(first)
    first.commit()
    for path in sidecars(target):
        path.chmod(0o644)

    with permissive_umask():
        second = db.connect(target)

    assert [mode_of(path) for path in sidecars(target)] == [0o600, 0o600]
    first.close()
    second.close()


def test_a_state_directory_owned_by_somebody_else_is_left_alone(tmp_path: Path) -> None:
    # chmod on a foreign directory raises; the runner skips it rather than
    # refusing to open a database it can still write.
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o777)
    target = state / "data.db"
    with mock.patch("os.geteuid", return_value=os.geteuid() + 1), permissive_umask():
        db.connect(target).close()
    assert mode_of(state) == 0o777
    assert mode_of(target) == 0o600  # created by the runner, private from the start
