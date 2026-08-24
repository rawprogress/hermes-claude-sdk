"""The read-only preflight an installing agent runs before touching anything.

Two properties matter: it must never create or modify state, and its verdict
must be machine-readable so an agent can branch on it without parsing prose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hermes_claude_runner import cli, config, db, doctor
from hermes_claude_runner.config import RunnerPaths


def names(report: dict) -> set[str]:
    return {check["name"] for check in report["checks"]}


def check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


# ── read-only ──────────────────────────────────────────────────────────────

def test_doctor_creates_nothing(paths: RunnerPaths) -> None:
    """A preflight that installs half of the thing it inspects is a trap."""
    doctor.diagnose(paths)

    assert not paths.db_path.exists()
    assert not paths.data_dir.exists()
    assert not paths.log_dir.exists()
    assert not paths.worktrees_root.exists()


def test_doctor_leaves_an_existing_database_untouched(paths: RunnerPaths) -> None:
    from hermes_claude_runner.store import Store

    with Store.open(paths.db_path) as store:
        store.create_run(run_id="rdoc00001", project="/p", role="implementer", prompt="p")
    before = paths.db_path.read_bytes()

    report = doctor.diagnose(paths)

    assert paths.db_path.read_bytes() == before
    assert check(report, "database")["ok"] is True
    assert check(report, "database")["schema_version"] == db.SCHEMA_VERSION


# ── verdict ────────────────────────────────────────────────────────────────

def test_report_is_machine_readable(paths: RunnerPaths) -> None:
    report = doctor.diagnose(paths)

    json.dumps(report)  # raises if anything is not serializable
    assert isinstance(report["ok"], bool)
    assert report["label"] == paths.launch_agent_label
    assert {"platform", "python", "git", "claude_cli", "projects_root",
            "database", "daemon", "launch_agent", "wrapper"} <= names(report)
    for entry in report["checks"]:
        assert set(entry) >= {"name", "ok", "required", "detail"}


def test_a_missing_install_is_reported_not_hidden(paths: RunnerPaths) -> None:
    report = doctor.diagnose(paths)

    assert report["ok"] is False
    assert check(report, "daemon")["ok"] is False
    assert check(report, "launch_agent")["ok"] is False
    assert "launch_agent" in report["problems"]


def test_optional_checks_do_not_decide_the_verdict(paths: RunnerPaths, monkeypatch) -> None:
    """Only required checks may fail the preflight."""
    monkeypatch.setattr(doctor, "_probe", lambda *a, **k: True)
    report = doctor.diagnose(paths)
    failing = [c["name"] for c in report["checks"] if not c["ok"]]
    assert report["ok"] == all(not check(report, name)["required"] for name in failing)


def test_a_reachable_daemon_flips_the_verdict(paths: RunnerPaths, monkeypatch) -> None:
    # _claude_cli_check prefers the configured binary and never consults
    # _probe, so without this the test would only pass on a machine that
    # happens to have Claude Code on its PATH.
    paths.claude_cli_path.parent.mkdir(parents=True, exist_ok=True)
    paths.claude_cli_path.touch()
    monkeypatch.setattr(doctor, "_probe", lambda *a, **k: True)
    monkeypatch.setattr(
        doctor.client, "send_request",
        lambda *a, **k: {"ok": True, "result": {"status": "ok", "version": "0.1.0",
                                                "active_runs": 0}},
    )
    paths.socket_path.parent.mkdir(parents=True, exist_ok=True)
    paths.socket_path.touch()  # the doctor checks the socket exists before dialing
    paths.plist_path.parent.mkdir(parents=True, exist_ok=True)
    paths.plist_path.write_text("<plist/>")
    paths.wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    paths.wrapper_path.write_text("#!/bin/sh\n")
    paths.wrapper_path.chmod(0o755)

    report = doctor.diagnose(paths)

    assert check(report, "daemon")["ok"] is True
    assert report["ok"] is True, report["problems"]
    assert report["problems"] == []


def test_the_daemon_probe_can_be_skipped(paths: RunnerPaths, monkeypatch) -> None:
    """An agent inspecting a machine with no daemon should not wait on a socket."""
    def explode(*_args, **_kwargs):
        raise AssertionError("the socket must not be contacted")

    monkeypatch.setattr(doctor.client, "send_request", explode)
    report = doctor.diagnose(paths, probe_daemon=False)
    assert check(report, "daemon")["ok"] is False
    assert "skipped" in check(report, "daemon")["detail"]


def test_every_problem_carries_a_next_step(paths: RunnerPaths) -> None:
    report = doctor.diagnose(paths)
    for name in report["problems"]:
        assert check(report, name)["fix"], f"{name} reports no way forward"


# ── CLI surface ────────────────────────────────────────────────────────────

def test_doctor_subcommand_prints_json_and_exits_nonzero_when_broken(
    paths: RunnerPaths, tmp_path: Path
) -> None:
    import os
    import subprocess

    env = {
        **os.environ,
        config.ENV_HOME: str(paths.data_dir),
        config.ENV_PROJECTS_ROOT: str(paths.projects_root),
        config.ENV_SOCKET: str(paths.socket_path),
        config.ENV_LOG_DIR: str(paths.log_dir),
        config.ENV_CLAUDE_CLI: str(tmp_path / "no-claude"),
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "hermes_claude_runner", "doctor", "--json"],
        capture_output=True, text=True, env=env, timeout=120,
    )

    payload = json.loads(completed.stdout)  # raises if progress text leaked
    assert payload["ok"] is False
    assert completed.returncode == 1


def test_doctor_subcommand_is_human_readable_without_json(paths, capsys, monkeypatch) -> None:
    from tests.runner.test_cli import env_for, run_cli

    result = run_cli(["doctor"], "", capsys, env_for(paths), monkeypatch)
    assert "daemon" in result["out"]
    assert result["code"] == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(result["out"])


def test_doctor_is_registered_in_the_parser() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["doctor", "--json"])
    assert args.command == "doctor" and args.json is True


# ── the criteria the agent contract branches on ────────────────────────────

def test_uv_is_required_because_every_documented_command_needs_it(
    paths: RunnerPaths, monkeypatch
) -> None:
    """The installer runs `uv sync` unconditionally; a warning would mislead."""
    report = doctor.diagnose(paths, probe_daemon=False)
    assert check(report, "uv")["required"] is True

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    without_uv = doctor.diagnose(paths, probe_daemon=False)
    assert "uv" in without_uv["problems"]
    assert "uv" in check(without_uv, "uv")["fix"]


def test_node_is_reported_but_never_blocks(paths: RunnerPaths, monkeypatch) -> None:
    """Claude Code is the requirement; Node is an implementation detail of it.

    A missing optional check has to say why it does not block, or the reader
    cannot tell it apart from one that does.
    """
    monkeypatch.setattr(doctor.shutil, "which",
                        lambda name: None if name == "node" else f"/usr/bin/{name}")
    report = doctor.diagnose(paths, probe_daemon=False)
    entry = check(report, "node")

    assert entry["required"] is False
    assert "node" not in report["problems"]
    assert "claude code" in entry["detail"].lower(), entry["detail"]


def test_a_fresh_machine_meets_the_documented_preflight_criterion(
    paths: RunnerPaths, monkeypatch
) -> None:
    """INSTALL_FOR_AGENTS phase 1 must be satisfiable before anything is installed.

    The old contract said `report.ok == true`, which is unreachable on a first
    install: the service is exactly what is not there yet.
    """
    paths.claude_cli_path.parent.mkdir(parents=True, exist_ok=True)
    paths.claude_cli_path.touch()
    monkeypatch.setattr(doctor, "_probe", lambda *a, **k: True)

    report = doctor.diagnose(paths, probe_daemon=False)

    assert report["ok"] is False, "nothing is installed yet"
    assert set(report["problems"]) <= doctor.INSTALLABLE_PROBLEMS
    assert report["installable"] is True, (
        "a machine that only lacks the service must be reported as ready to install"
    )


def test_a_machine_missing_a_prerequisite_is_not_installable(
    paths: RunnerPaths, monkeypatch
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    report = doctor.diagnose(paths, probe_daemon=False)

    assert report["installable"] is False
    assert not set(report["problems"]) <= doctor.INSTALLABLE_PROBLEMS


def test_installable_is_the_only_thing_phase_one_has_to_read() -> None:
    """One flag, so the contract cannot be read two ways."""
    assert doctor.INSTALLABLE_PROBLEMS == frozenset({"daemon", "launch_agent", "wrapper"})
