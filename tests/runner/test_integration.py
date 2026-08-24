"""End-to-end paths that cross real process boundaries.

No model is contacted: the spawned worker is given a run whose work directory
is missing, so it fails before it would ever build an SDK client.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_claude_runner import client as rpc_client
from hermes_claude_runner import config, daemon, models
from hermes_claude_runner.config import RunnerPaths
from hermes_claude_runner.store import Store

from .conftest import make_repo, requires_git

pytestmark = pytest.mark.integration


def env_for(paths: RunnerPaths) -> dict[str, str]:
    return {
        **os.environ,
        config.ENV_HOME: str(paths.data_dir),
        config.ENV_PROJECTS_ROOT: str(paths.projects_root),
        config.ENV_WORKTREES_ROOT: str(paths.worktrees_root),
        config.ENV_SOCKET: str(paths.socket_path),
        config.ENV_LOG_DIR: str(paths.log_dir),
        config.ENV_CLAUDE_CLI: str(paths.claude_cli_path),
    }


def wait_for(predicate, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_spawned_worker_process_really_runs_and_records_its_outcome(
    paths: RunnerPaths,
) -> None:
    with Store.open(paths.db_path) as store:
        store.create_run(run_id="rspawn001", project=str(paths.projects_root / "gone"),
                         role="implementer", prompt="never reaches the model")
        store.update_run("rspawn001", worktree=str(paths.projects_root / "absent"))

    spawner = daemon.ProcessSpawner(paths)
    argv = spawner.worker_argv("rspawn001")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, capture_output=True, text=True, env=env_for(paths), timeout=120,
    )

    assert completed.returncode == 1, completed.stderr
    with Store.open(paths.db_path) as store:
        run = store.get_run("rspawn001")
        assert run["status"] == models.STATUS_FAILED
        assert run["result"] is None, "a failed worker must never look successful"
        assert "missing" in run["error"]


@requires_git
def test_daemon_start_spawns_a_detached_worker_that_survives_the_daemon(
    paths: RunnerPaths,
) -> None:
    """The full path: socket -> start -> worktree -> real detached worker."""
    repo = make_repo(paths.projects_root / "demo")
    server = daemon.Daemon(paths, spawner=_TrackingSpawner(paths))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: paths.socket_path.exists())
        response = rpc_client.send_request(
            paths.socket_path,
            {"action": "start", "project": str(repo), "prompt": "hello"},
            timeout=30,
        )
        assert response["ok"] is True, response
        run_id = response["result"]["run_id"]
        pid = server.spawner.last_pid  # type: ignore[attr-defined]

        assert Path(response["result"]["worktree"]).is_dir()
        assert os.getsid(pid) != os.getsid(0), "worker must be in its own session"
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.spawner.cleanup()  # type: ignore[attr-defined]

    with Store.open(paths.db_path) as store:
        assert store.get_run(run_id)["worker_pid"] == pid


class _TrackingSpawner(daemon.ProcessSpawner):
    """Spawns a harmless detached process that mimics a worker's argv."""

    def __init__(self, paths: RunnerPaths) -> None:
        super().__init__(paths)
        self.last_pid = 0
        self._process: subprocess.Popen | None = None

    def spawn(self, run_id: str, *, resume: str | None = None) -> tuple[int, float]:
        self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", "import time;time.sleep(30)", daemon.WORKER_MARKER],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.last_pid = self._process.pid
        return self._process.pid, time.time()

    def cleanup(self) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process.wait(timeout=10)


@requires_git
def test_rpc_binary_drives_a_full_run_lifecycle(paths: RunnerPaths) -> None:
    """start -> send -> status -> events -> stop -> resume through the real CLI."""
    repo = make_repo(paths.projects_root / "demo")
    server = daemon.Daemon(paths, spawner=_TrackingSpawner(paths))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def rpc(request: dict) -> dict:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "hermes_claude_runner", "rpc"],
            input=json.dumps(request), capture_output=True, text=True,
            env=env_for(paths), timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    try:
        assert wait_for(lambda: paths.socket_path.exists())

        started = rpc({"action": "start", "project": str(repo), "prompt": "fix it",
                       "hermes_session_id": "hs-1", "hermes_task_id": "ht-1"})["result"]
        run_id = started["run_id"]
        assert started["branch"] == models.branch_for_run(run_id)

        assert rpc({"action": "send", "run_id": run_id,
                    "message": "also add docs"})["result"]["pending_messages"] == 1

        status = rpc({"action": "status", "run_id": run_id})["result"]
        assert status["hermes_task_id"] == "ht-1"
        assert status["event_high_water"] >= 3

        events = rpc({"action": "events", "run_id": run_id, "after": 0,
                      "limit": 50})["result"]
        assert [e["kind"] for e in events["events"]][:2] == ["run_created", "worktree_ready"]

        assert rpc({"action": "list"})["result"]["count"] == 1

        assert rpc({"action": "stop", "run_id": run_id})["result"]["stop_requested"] is True
        assert Path(started["worktree"]).is_dir(), "stop must preserve the worktree"

        # A resume needs a captured session id; simulate what the worker stores.
        with Store.open(paths.db_path) as store:
            store.update_run(run_id, status=models.STATUS_STOPPED,
                             claude_session_id="sess-int-1", worker_pid=None,
                             worker_started_at=None)

        resumed = rpc({"action": "resume", "run_id": run_id,
                       "message": "now add a second tested behaviour"})["result"]
        assert resumed["claude_session_id"] == "sess-int-1"
        assert resumed["worktree"] == started["worktree"]
        assert rpc({"action": "status", "run_id": run_id})["result"]["stop_requested"] is False
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.spawner.cleanup()  # type: ignore[attr-defined]


def test_unknown_action_over_the_real_binary_fails_closed(paths: RunnerPaths) -> None:
    server = daemon.Daemon(paths, spawner=_TrackingSpawner(paths))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: paths.socket_path.exists())
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "hermes_claude_runner", "rpc"],
            input='{"action":"delete_everything"}', capture_output=True, text=True,
            env=env_for(paths), timeout=120,
        )
        assert json.loads(completed.stdout)["error"] == "invalid_action"
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.spawner.cleanup()  # type: ignore[attr-defined]
