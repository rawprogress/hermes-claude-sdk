"""Socket server, worker spawning and reconciliation of lost workers."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_claude_runner import client as rpc_client
from hermes_claude_runner import daemon, models
from hermes_claude_runner.config import RunnerPaths
from hermes_claude_runner.store import Store

from .conftest import make_repo, requires_git


@pytest.fixture()
def store(paths: RunnerPaths) -> Store:
    s = Store.open(paths.db_path)
    yield s
    s.close()


@pytest.fixture()
def server(paths: RunnerPaths):
    """A live daemon on a temporary unix socket."""
    d = daemon.Daemon(paths, spawner=_NullSpawner())
    thread = threading.Thread(target=d.serve_forever, daemon=True)
    thread.start()
    for _ in range(200):
        if paths.socket_path.exists():
            break
        time.sleep(0.01)
    yield d
    d.shutdown()
    thread.join(timeout=5)


class _NullSpawner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def spawn(self, run_id: str, *, resume: str | None = None) -> tuple[int, float]:
        self.calls.append((run_id, resume))
        return os.getpid(), time.time()

    def is_alive(self, pid, started_at, run_id: str | None = None) -> bool:
        return False

    def signal_stop(self, pid, started_at, run_id: str | None = None) -> bool:
        return False


# ── socket transport ───────────────────────────────────────────────────────

def test_daemon_answers_health_over_the_socket(paths: RunnerPaths, server) -> None:
    response = rpc_client.send_request(paths.socket_path, {"action": "health"}, timeout=5)
    assert response["ok"] is True
    assert response["result"]["status"] == "ok"


def test_daemon_returns_an_error_envelope_for_garbage(paths: RunnerPaths, server) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(paths.socket_path))
        sock.sendall(b"this is not json\n")
        sock.shutdown(socket.SHUT_WR)
        payload = b""
        while chunk := sock.recv(65536):
            payload += chunk
    assert json.loads(payload)["error"] == "invalid_request"


def test_daemon_rejects_an_oversized_request(paths: RunnerPaths, server) -> None:
    huge = {"action": "start", "project": "x", "prompt": "y" * (daemon.MAX_REQUEST_BYTES + 10)}
    response = rpc_client.send_request(paths.socket_path, huge, timeout=5)
    assert response["ok"] is False
    assert response["error"] in ("invalid_request", "invalid_params")


def test_daemon_serves_concurrent_clients(paths: RunnerPaths, server) -> None:
    results: list[dict] = []
    lock = threading.Lock()

    def ask() -> None:
        response = rpc_client.send_request(paths.socket_path, {"action": "list"}, timeout=10)
        with lock:
            results.append(response)

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert all(r["ok"] for r in results)


@requires_git
def test_daemon_starts_a_run_end_to_end(paths: RunnerPaths, server, store: Store) -> None:
    repo = make_repo(paths.projects_root / "demo")
    response = rpc_client.send_request(
        paths.socket_path,
        {"action": "start", "project": str(repo), "prompt": "fix", "hermes_task_id": "t-1"},
        timeout=10,
    )
    run_id = response["result"]["run_id"]
    assert server.spawner.calls == [(run_id, None)]
    assert store.get_run(run_id)["hermes_task_id"] == "t-1"


def test_daemon_replaces_a_stale_socket_file(paths: RunnerPaths) -> None:
    paths.socket_path.parent.mkdir(parents=True, exist_ok=True)
    paths.socket_path.write_text("stale")
    d = daemon.Daemon(paths, spawner=_NullSpawner())
    thread = threading.Thread(target=d.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if paths.socket_path.is_socket():
                break
            time.sleep(0.01)
        assert rpc_client.send_request(paths.socket_path, {"action": "health"},
                                       timeout=5)["ok"] is True
    finally:
        d.shutdown()
        thread.join(timeout=5)


def test_socket_is_only_reachable_by_its_owner(paths: RunnerPaths, server) -> None:
    assert (paths.socket_path.stat().st_mode & 0o077) == 0


# ── client-side failure envelopes ──────────────────────────────────────────

def test_client_reports_an_absent_daemon(tmp_path: Path) -> None:
    response = rpc_client.send_request(tmp_path / "missing.sock", {"action": "health"}, timeout=1)
    assert response["ok"] is False
    assert response["error"] == "daemon_unavailable"


def test_client_reports_a_timeout(paths: RunnerPaths) -> None:
    paths.socket_path.parent.mkdir(parents=True, exist_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(paths.socket_path))
    listener.listen(1)
    try:
        response = rpc_client.send_request(paths.socket_path, {"action": "health"}, timeout=0.2)
        assert response["ok"] is False
        assert response["error"] == "daemon_unavailable"
    finally:
        listener.close()


# ── reconciliation ─────────────────────────────────────────────────────────

def test_reconcile_marks_a_lost_worker_unknown(paths: RunnerPaths, store: Store) -> None:
    store.create_run(run_id="rlost0001", project="/p", role="implementer", prompt="p")
    store.update_run("rlost0001", status=models.STATUS_WORKING, worker_pid=999_999,
                     worker_started_at=time.time() - 3600)

    d = daemon.Daemon(paths, spawner=_NullSpawner())
    assert d.reconcile() == ["rlost0001"]

    run = store.get_run("rlost0001")
    assert run["status"] == models.STATUS_UNKNOWN
    assert run["result"] is None, "a lost worker must never look successful"
    assert "reconciled" in [e["kind"] for e in store.get_events("rlost0001")]


def test_reconcile_marks_a_run_without_any_worker_unknown(
    paths: RunnerPaths, store: Store
) -> None:
    store.create_run(run_id="rorph0001", project="/p", role="implementer", prompt="p")
    store.update_run("rorph0001", status=models.STATUS_PREPARING,
                     worker_started_at=time.time() - 3600)
    d = daemon.Daemon(paths, spawner=_NullSpawner())
    assert d.reconcile() == ["rorph0001"]
    assert store.get_run("rorph0001")["status"] == models.STATUS_UNKNOWN


def test_reconcile_leaves_live_workers_alone(paths: RunnerPaths, store: Store) -> None:
    class LiveSpawner(_NullSpawner):
        def is_alive(self, pid, started_at, run_id=None) -> bool:
            return True

    store.create_run(run_id="rlive0001", project="/p", role="implementer", prompt="p")
    store.update_run("rlive0001", status=models.STATUS_WORKING, worker_pid=4242,
                     worker_started_at=time.time() - 3600)
    d = daemon.Daemon(paths, spawner=LiveSpawner())
    assert d.reconcile() == []
    assert store.get_run("rlive0001")["status"] == models.STATUS_WORKING


def test_reconcile_respects_a_grace_period_for_just_spawned_workers(
    paths: RunnerPaths, store: Store
) -> None:
    store.create_run(run_id="rfresh001", project="/p", role="implementer", prompt="p")
    store.update_run("rfresh001", status=models.STATUS_PREPARING, worker_pid=999_999,
                     worker_started_at=time.time())
    d = daemon.Daemon(paths, spawner=_NullSpawner())
    assert d.reconcile() == []
    assert store.get_run("rfresh001")["status"] == models.STATUS_PREPARING


def test_reconcile_ignores_finished_runs(paths: RunnerPaths, store: Store) -> None:
    store.create_run(run_id="rdone0001", project="/p", role="implementer", prompt="p")
    store.update_run("rdone0001", status=models.STATUS_COMPLETED, result="all good",
                     worker_pid=999_999)
    d = daemon.Daemon(paths, spawner=_NullSpawner())
    assert d.reconcile() == []
    assert store.get_run("rdone0001")["result"] == "all good"


def test_daemon_reconciles_on_startup(paths: RunnerPaths, store: Store) -> None:
    store.create_run(run_id="rboot0001", project="/p", role="implementer", prompt="p")
    store.update_run("rboot0001", status=models.STATUS_WORKING, worker_pid=999_999,
                     worker_started_at=time.time() - 3600)
    d = daemon.Daemon(paths, spawner=_NullSpawner())
    thread = threading.Thread(target=d.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(300):
            if store.get_run("rboot0001")["status"] == models.STATUS_UNKNOWN:
                break
            time.sleep(0.01)
        assert store.get_run("rboot0001")["status"] == models.STATUS_UNKNOWN
    finally:
        d.shutdown()
        thread.join(timeout=5)


# ── process spawner ────────────────────────────────────────────────────────

def test_worker_argv_is_absolute_and_shell_free(paths: RunnerPaths) -> None:
    spawner = daemon.ProcessSpawner(paths)
    argv = spawner.worker_argv("rabc12345", resume="sess-1")
    assert argv[0] == sys.executable or Path(argv[0]).is_absolute()
    assert "--run-id" in argv and "rabc12345" in argv
    assert "--resume" in argv and "sess-1" in argv
    assert all(isinstance(part, str) for part in argv)


def test_worker_argv_omits_resume_when_absent(paths: RunnerPaths) -> None:
    assert "--resume" not in daemon.ProcessSpawner(paths).worker_argv("rabc12345", resume=None)


def test_spawned_worker_gets_its_own_process_session(paths: RunnerPaths) -> None:
    spawner = daemon.ProcessSpawner(paths)
    # The trailing argument makes ``ps`` show the same marker a real worker
    # shows, which is what is_alive matches on.
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import os,time;print(os.getsid(0),flush=True);time.sleep(2)",
         daemon.WORKER_MARKER],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    child_sid = int(proc.stdout.readline().strip())
    try:
        assert child_sid != os.getsid(0), "workers must survive a shell disconnect"
        assert spawner.is_alive(proc.pid, time.time()) is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_is_alive_rejects_a_dead_pid(paths: RunnerPaths) -> None:
    spawner = daemon.ProcessSpawner(paths)
    assert spawner.is_alive(999_999, time.time()) is False
    assert spawner.is_alive(None, None) is False


def test_is_alive_rejects_a_recycled_pid(paths: RunnerPaths) -> None:
    # Our own process is alive, but it did not start when the run claims to.
    spawner = daemon.ProcessSpawner(paths)
    assert spawner.is_alive(os.getpid(), started_at=1.0) is False


def test_is_alive_rejects_a_live_process_that_is_not_a_worker(paths: RunnerPaths) -> None:
    spawner = daemon.ProcessSpawner(paths)
    assert spawner.is_alive(os.getpid(), started_at=time.time()) is False


def test_reconcile_gives_a_brand_new_run_without_a_worker_its_grace(
    paths: RunnerPaths, store: Store
) -> None:
    # Caught between create_run and spawn: both worker fields are still empty.
    store.create_run(run_id="rrace0001", project="/p", role="implementer", prompt="p")
    store.update_run("rrace0001", status=models.STATUS_PREPARING)
    d = daemon.Daemon(paths, spawner=_NullSpawner())
    assert d.reconcile() == []
    assert store.get_run("rrace0001")["status"] == models.STATUS_PREPARING


def test_transport_timeout_exceeds_the_slowest_synchronous_handler() -> None:
    # start runs `git worktree add` inline; a shorter client timeout would report
    # daemon_unavailable for a run that actually started.
    from hermes_claude_runner import client as rpc_client
    from hermes_claude_runner import worktree

    assert rpc_client.DEFAULT_TIMEOUT_SECONDS > worktree.GIT_TIMEOUT_SECONDS


def test_daemon_backlog_absorbs_a_burst_of_clients(paths: RunnerPaths, server) -> None:
    """The default socketserver backlog is 5; Hermes can open more at once."""
    assert daemon._Server.request_queue_size >= 64

    results: list[dict] = []
    lock = threading.Lock()

    def ask() -> None:
        response = rpc_client.send_request(paths.socket_path, {"action": "health"},
                                           timeout=30)
        with lock:
            results.append(response)

    threads = [threading.Thread(target=ask) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 32
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]][:2]


# ── liveness identity ──────────────────────────────────────────────────────

def test_elapsed_time_parsing_covers_every_ps_format() -> None:
    assert daemon._parse_etime("05") == 5
    assert daemon._parse_etime("01:30") == 90
    assert daemon._parse_etime("02:03:04") == 7384
    assert daemon._parse_etime("1-02:03:04") == 93_784
    assert daemon._parse_etime("") is None
    assert daemon._parse_etime("nonsense") is None


def test_liveness_uses_timezone_free_elapsed_time(paths: RunnerPaths) -> None:
    """lstart is local time; a DST shift must not make a live worker look dead."""
    source = daemon.__file__
    text = __import__("pathlib").Path(source).read_text()
    assert "mktime" not in text, "local-time parsing is ambiguous across DST"
    assert "etime" in text


def test_is_alive_requires_the_run_id_in_the_command(paths: RunnerPaths) -> None:
    spawner = daemon.ProcessSpawner(paths)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time;time.sleep(3)",
         daemon.WORKER_MARKER, "--run-id", "rreal0001"],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert spawner.is_alive(proc.pid, time.time(), run_id="rreal0001") is True
        assert spawner.is_alive(proc.pid, time.time(), run_id="rother001") is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_is_alive_rejects_a_pid_that_started_far_too_long_ago(paths: RunnerPaths) -> None:
    spawner = daemon.ProcessSpawner(paths)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time;time.sleep(3)", daemon.WORKER_MARKER],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert spawner.is_alive(proc.pid, started_at=time.time() - 86_400) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_reconcile_checks_identity_with_the_run_id(paths: RunnerPaths, store: Store) -> None:
    seen: list[dict] = []

    class RecordingSpawner(_NullSpawner):
        def is_alive(self, pid, started_at, run_id=None) -> bool:
            seen.append({"pid": pid, "run_id": run_id})
            return False

    store.create_run(run_id="rrecon001", project="/p", role="implementer", prompt="p")
    store.update_run("rrecon001", status=models.STATUS_WORKING, worker_pid=4242,
                     worker_started_at=time.time() - 3600)
    daemon.Daemon(paths, spawner=RecordingSpawner()).reconcile()
    assert seen == [{"pid": 4242, "run_id": "rrecon001"}]


# ── stop reaches the live worker ───────────────────────────────────────────

def test_signal_stop_refuses_a_process_that_is_not_the_run_s_worker(
    paths: RunnerPaths,
) -> None:
    spawner = daemon.ProcessSpawner(paths)
    # Our own pid is alive but is not a worker for this run.
    assert spawner.signal_stop(os.getpid(), time.time(), run_id="rnope0001") is False


def test_signal_stop_terminates_the_real_worker(paths: RunnerPaths) -> None:
    spawner = daemon.ProcessSpawner(paths)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time;time.sleep(30)",
         daemon.WORKER_MARKER, "--run-id", "rkill0001"],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    started = time.time()
    try:
        assert spawner.signal_stop(proc.pid, started, run_id="rkill0001") is True
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:  # pragma: no cover - cleanup
            proc.kill()
            proc.wait(timeout=5)


def test_reconcile_does_not_touch_a_run_between_resume_and_spawn(
    paths: RunnerPaths, store: Store
) -> None:
    """The window between claim_for_resume and the new spawn must stay quiet."""
    store.create_run(run_id="rwindow01", project="/p", role="implementer", prompt="p")
    store.update_run("rwindow01", status=models.STATUS_STOPPED, worker_pid=999_999,
                     worker_started_at=time.time() - 3600)
    assert store.claim_for_resume("rwindow01") is True

    assert daemon.Daemon(paths, spawner=_NullSpawner()).reconcile() == []
    assert store.get_run("rwindow01")["status"] == models.STATUS_PREPARING


# ── timestamp parsing ──────────────────────────────────────────────────────

def test_stamp_parsing_accepts_both_timestamp_shapes() -> None:
    """The schema default writes fractional UTC; the store writes whole seconds."""
    whole = daemon._parse_stamp("2026-08-24T12:34:56Z")
    fractional = daemon._parse_stamp("2026-08-24T12:34:56.789Z")
    assert whole is not None and fractional is not None
    assert abs(fractional - whole) < 1.0
    assert daemon._parse_stamp("2026-08-24T12:34:56.789Z") == pytest.approx(whole + 0.789,
                                                                           abs=0.01)


@pytest.mark.parametrize("bad", [None, "", "not a timestamp", "2026-13-45T99:99:99Z",
                                 "1787527226"])
def test_stamp_parsing_rejects_nonsense(bad: object) -> None:
    assert daemon._parse_stamp(bad) is None  # type: ignore[arg-type]


def test_a_schema_default_timestamp_still_grants_the_grace_period(
    paths: RunnerPaths, store: Store
) -> None:
    """A row written with the schema default must not be reconciled instantly."""
    store.connection.execute(
        "INSERT INTO runs (run_id, project, role, prompt, status) VALUES (?,?,?,?,?)",
        ("rdefault1", "/p", "implementer", "p", models.STATUS_PREPARING),
    )
    stamp = store.get_run("rdefault1")["updated_at"]
    assert "." in stamp, "the schema default is expected to be fractional"
    assert daemon.Daemon(paths, spawner=_NullSpawner()).reconcile() == []
    assert store.get_run("rdefault1")["status"] == models.STATUS_PREPARING
