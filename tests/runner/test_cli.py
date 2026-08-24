"""CLI surface. The rpc mode's stdout contract is the load-bearing part."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_claude_runner import cli, daemon
from hermes_claude_runner.config import RunnerPaths
from hermes_claude_runner.store import Store


def run_cli(argv: list[str], stdin_text: str, capsys, env: dict[str, str], monkeypatch) -> dict:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.stdin", _FakeStdin(stdin_text))
    code = cli.main(argv)
    captured = capsys.readouterr()
    return {"code": code, "out": captured.out, "err": captured.err}


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


def env_for(paths: RunnerPaths) -> dict[str, str]:
    from hermes_claude_runner import config

    return {
        config.ENV_HOME: str(paths.data_dir),
        config.ENV_PROJECTS_ROOT: str(paths.projects_root),
        config.ENV_SOCKET: str(paths.socket_path),
        config.ENV_LOG_DIR: str(paths.log_dir),
        config.ENV_CLAUDE_CLI: str(paths.claude_cli_path),
    }


@pytest.fixture()
def server(paths: RunnerPaths):
    class _Null:
        def spawn(self, run_id: str, *, resume: str | None = None) -> tuple[int, float]:
            return os.getpid(), time.time()

        def is_alive(self, pid, started_at, run_id=None) -> bool:
            return False

        def signal_stop(self, pid, started_at, run_id=None) -> bool:
            return False

    d = daemon.Daemon(paths, spawner=_Null())
    thread = threading.Thread(target=d.serve_forever, daemon=True)
    thread.start()
    for _ in range(200):
        if paths.socket_path.exists():
            break
        time.sleep(0.01)
    yield d
    d.shutdown()
    thread.join(timeout=5)


# ── rpc mode ───────────────────────────────────────────────────────────────

def test_rpc_writes_one_json_envelope(paths, server, capsys, monkeypatch) -> None:
    result = run_cli(["rpc"], '{"action":"health"}', capsys, env_for(paths), monkeypatch)
    assert result["code"] == 0
    assert result["out"].count("\n") == 1
    assert json.loads(result["out"])["result"]["status"] == "ok"


def test_rpc_reports_an_absent_daemon_as_json(paths, capsys, monkeypatch) -> None:
    result = run_cli(["rpc"], '{"action":"health"}', capsys, env_for(paths), monkeypatch)
    assert json.loads(result["out"])["error"] == "daemon_unavailable"
    assert result["code"] == 0, "a well-formed envelope is still a successful transport"


def test_rpc_rejects_non_json_stdin_without_touching_the_daemon(
    paths, capsys, monkeypatch
) -> None:
    result = run_cli(["rpc"], "hello there", capsys, env_for(paths), monkeypatch)
    assert json.loads(result["out"])["error"] == "invalid_request"


def test_rpc_rejects_empty_stdin(paths, capsys, monkeypatch) -> None:
    result = run_cli(["rpc"], "", capsys, env_for(paths), monkeypatch)
    assert json.loads(result["out"])["error"] == "invalid_request"


def test_rpc_stdout_never_carries_log_noise(paths, server, tmp_path) -> None:
    """Full-fidelity check through a real process, with debug logging enabled."""
    env = {**os.environ, **env_for(paths), "HERMES_CLAUDE_RUNNER_LOG_LEVEL": "DEBUG"}
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "hermes_claude_runner", "rpc"],
        input='{"action":"health"}', capture_output=True, text=True, env=env, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    parsed = json.loads(completed.stdout)  # raises if anything else was printed
    assert parsed["ok"] is True
    assert completed.stdout.strip().count("\n") == 0


def test_rpc_passes_the_request_through_untouched(paths, server, capsys, monkeypatch) -> None:
    request = '{"action":"list","limit":7}'
    result = run_cli(["rpc"], request, capsys, env_for(paths), monkeypatch)
    assert json.loads(result["out"])["result"]["limit"] == 7


# ── other subcommands ──────────────────────────────────────────────────────

def test_version_prints_the_package_version(capsys, paths, monkeypatch) -> None:
    result = run_cli(["version"], "", capsys, env_for(paths), monkeypatch)
    from hermes_claude_runner import __version__

    assert __version__ in result["out"]
    assert result["code"] == 0


def test_health_subcommand_reaches_the_daemon(paths, server, capsys, monkeypatch) -> None:
    result = run_cli(["health"], "", capsys, env_for(paths), monkeypatch)
    assert json.loads(result["out"])["result"]["status"] == "ok"
    assert result["code"] == 0


def test_health_subcommand_exits_nonzero_without_a_daemon(paths, capsys, monkeypatch) -> None:
    result = run_cli(["health"], "", capsys, env_for(paths), monkeypatch)
    assert json.loads(result["out"])["error"] == "daemon_unavailable"
    assert result["code"] == 1


def test_unknown_subcommand_fails(paths, capsys, monkeypatch) -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli(["nonsense"], "", capsys, env_for(paths), monkeypatch)
    assert exc.value.code != 0


def test_worker_subcommand_drives_one_run(paths, capsys, monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_run_worker(**kwargs):
        calls.append(kwargs)
        return "completed"

    monkeypatch.setattr("hermes_claude_runner.worker.run_worker", fake_run_worker)
    with Store.open(paths.db_path) as store:
        store.create_run(run_id="rcli00001", project="/p", role="implementer", prompt="p")

    result = run_cli(["worker", "--run-id", "rcli00001", "--resume", "sess-3"], "",
                     capsys, env_for(paths), monkeypatch)
    assert result["code"] == 0
    assert calls[0]["run_id"] == "rcli00001"
    assert calls[0]["resume"] == "sess-3"


def test_worker_subcommand_rejects_a_malformed_run_id(paths, capsys, monkeypatch) -> None:
    result = run_cli(["worker", "--run-id", "../etc"], "", capsys, env_for(paths), monkeypatch)
    assert result["code"] != 0


def test_worker_subcommand_reports_a_failed_run(paths, capsys, monkeypatch) -> None:
    async def fake_run_worker(**kwargs):
        return "failed"

    monkeypatch.setattr("hermes_claude_runner.worker.run_worker", fake_run_worker)
    result = run_cli(["worker", "--run-id", "rcli00001"], "", capsys, env_for(paths), monkeypatch)
    assert result["code"] == 1


def test_sigterm_requests_a_controlled_stop(paths) -> None:
    with Store.open(paths.db_path) as store:
        store.create_run(run_id="rsig00001", project="/p", role="implementer", prompt="p")
        cli._request_stop_on_signal(store, "rsig00001")
        assert store.get_run("rsig00001")["stop_requested"] is True


def test_module_entry_point_exists() -> None:
    assert (Path(cli.__file__).parent / "__main__.py").exists()


# ── signal handling ────────────────────────────────────────────────────────

def test_worker_registers_real_signal_handlers_that_request_a_stop(
    paths, capsys, monkeypatch
) -> None:
    """Deliver a real SIGTERM and prove both stop channels fire."""
    import signal as signal_mod

    captured: dict = {}

    async def fake_run_worker(**kwargs):
        captured.update(kwargs)
        # Handlers must be installed by now, and must not be the defaults.
        handler = signal_mod.getsignal(signal_mod.SIGTERM)
        assert callable(handler)
        assert handler not in (signal_mod.SIG_DFL, signal_mod.SIG_IGN)
        os.kill(os.getpid(), signal_mod.SIGTERM)
        return "stopped"

    monkeypatch.setattr("hermes_claude_runner.worker.run_worker", fake_run_worker)
    with Store.open(paths.db_path) as store:
        store.create_run(run_id="rsigterm1", project="/p", role="implementer", prompt="p")

    before_term = signal_mod.getsignal(signal_mod.SIGTERM)
    before_int = signal_mod.getsignal(signal_mod.SIGINT)
    try:
        result = run_cli(["worker", "--run-id", "rsigterm1"], "", capsys,
                         env_for(paths), monkeypatch)
    finally:
        signal_mod.signal(signal_mod.SIGTERM, before_term)
        signal_mod.signal(signal_mod.SIGINT, before_int)

    assert result["code"] == 0
    assert captured["external_stop"].is_set(), "the in-process stop channel must fire"
    with Store.open(paths.db_path) as store:
        assert store.get_run("rsigterm1")["stop_requested"] is True


def test_worker_restores_the_previous_signal_handlers(paths, capsys, monkeypatch) -> None:
    import signal as signal_mod

    async def fake_run_worker(**kwargs):
        return "completed"

    monkeypatch.setattr("hermes_claude_runner.worker.run_worker", fake_run_worker)
    with Store.open(paths.db_path) as store:
        store.create_run(run_id="rrestore1", project="/p", role="implementer", prompt="p")

    sentinel = signal_mod.getsignal(signal_mod.SIGTERM)
    run_cli(["worker", "--run-id", "rrestore1"], "", capsys, env_for(paths), monkeypatch)
    assert signal_mod.getsignal(signal_mod.SIGTERM) is sentinel
