"""RPC validation, envelopes and handler state transitions (no socket, no SDK)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_claude_runner import models, rpc
from hermes_claude_runner.config import RunnerPaths
from hermes_claude_runner.store import Store

from .conftest import make_repo, requires_git


class FakeSpawner:
    """Stands in for the daemon's worker-process launcher."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.signals: list[dict] = []
        self.liveness_probes: list[dict] = []
        self.fail = fail
        self.alive = False
        self._next_pid = 1000

    def spawn(self, run_id: str, *, resume: str | None = None) -> tuple[int, float]:
        if self.fail:
            raise OSError("no fork for you")
        self.calls.append({"run_id": run_id, "resume": resume})
        self._next_pid += 1
        return self._next_pid, 1234.5

    def is_alive(self, pid, started_at, run_id: str | None = None) -> bool:
        self.liveness_probes.append({"pid": pid, "run_id": run_id})
        return self.alive

    def signal_stop(self, pid, started_at, run_id: str | None = None) -> bool:
        self.signals.append({"pid": pid, "run_id": run_id})
        return self.alive


@pytest.fixture()
def runtime(paths: RunnerPaths, tmp_path: Path):
    store = Store.open(paths.db_path)
    rt = rpc.Runtime(paths=paths, store=store, spawner=FakeSpawner())
    yield rt
    store.close()


@pytest.fixture()
def repo(paths: RunnerPaths) -> Path:
    return make_repo(paths.projects_root / "demo")


def call(runtime: rpc.Runtime, **request) -> dict:
    return rpc.handle_request(request, runtime)


def ok(response: dict) -> dict:
    assert response["ok"] is True, response
    return response["result"]


def err(response: dict) -> str:
    assert response["ok"] is False, response
    assert isinstance(response["detail"], str)
    return response["error"]


# ── envelope and validation ────────────────────────────────────────────────

def test_unknown_action_fails_closed(runtime: rpc.Runtime) -> None:
    assert err(call(runtime, action="rm_rf")) == "invalid_action"


def test_missing_action_fails_closed(runtime: rpc.Runtime) -> None:
    assert err(call(runtime)) == "invalid_action"


@pytest.mark.parametrize("request_obj", [[], "start", 5, None, True])
def test_non_object_request_is_rejected(runtime: rpc.Runtime, request_obj: object) -> None:
    assert err(rpc.handle_request(request_obj, runtime)) == "invalid_request"


def test_parse_request_rejects_non_json() -> None:
    with pytest.raises(rpc.RunnerError) as exc:
        rpc.parse_request("not json at all")
    assert exc.value.code == "invalid_request"


def test_parse_request_rejects_json_that_is_not_an_object() -> None:
    with pytest.raises(rpc.RunnerError) as exc:
        rpc.parse_request("[1,2,3]")
    assert exc.value.code == "invalid_request"


def test_parse_request_accepts_a_json_object() -> None:
    assert rpc.parse_request('{"action":"health"}') == {"action": "health"}


def test_response_serializes_to_one_json_line() -> None:
    line = rpc.serialize_response({"ok": True, "result": {"a": "ü"}})
    assert "\n" not in line
    assert json.loads(line)["result"]["a"] == "ü"


def test_health_reports_liveness(runtime: rpc.Runtime) -> None:
    result = ok(call(runtime, action="health"))
    assert result["status"] == "ok"
    assert result["version"]
    assert result["schema_version"] >= 1
    assert result["pid"] > 0
    assert result["active_runs"] == 0


# ── start ──────────────────────────────────────────────────────────────────

@requires_git
def test_start_creates_run_worktree_and_spawns_worker(runtime: rpc.Runtime, repo: Path) -> None:
    result = ok(call(runtime, action="start", project=str(repo), prompt="fix it",
                     hermes_session_id="hs-1", hermes_task_id="ht-1"))

    assert models.is_valid_run_id(result["run_id"])
    assert result["status"] in ("preparing", "working")
    assert result["worktree"].endswith(f"demo/{result['run_id']}")
    assert len(result["base_sha"]) == 40
    assert result["branch"] == models.branch_for_run(result["run_id"])
    assert result["mode"] == "worktree"
    assert Path(result["worktree"]).is_dir()
    assert runtime.spawner.calls == [{"run_id": result["run_id"], "resume": None}]

    run = runtime.store.get_run(result["run_id"])
    assert run["hermes_session_id"] == "hs-1"
    assert run["hermes_task_id"] == "ht-1"
    assert run["role"] == "implementer"
    assert run["worker_pid"] == 1001


@requires_git
def test_start_records_creation_events(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix it"))["run_id"]
    kinds = [e["kind"] for e in runtime.store.get_events(run_id)]
    assert kinds[:3] == ["run_created", "worktree_ready", "prompt"]


@requires_git
def test_start_without_worktree_uses_the_checkout_directly(
    runtime: rpc.Runtime, repo: Path
) -> None:
    result = ok(call(runtime, action="start", project=str(repo), prompt="fix",
                     create_worktree=False))
    assert result["mode"] == "direct"
    assert result["worktree"] == str(repo)
    assert result["branch"] is None
    assert len(result["base_sha"]) == 40


@requires_git
def test_start_accepts_a_custom_role(runtime: rpc.Runtime, repo: Path) -> None:
    result = ok(call(runtime, action="start", project=str(repo), prompt="review",
                     role="reviewer"))
    assert runtime.store.get_run(result["run_id"])["role"] == "reviewer"


@requires_git
@pytest.mark.parametrize("prompt", ["", "   ", None, 42, ["a"]])
def test_start_rejects_a_bad_prompt(runtime: rpc.Runtime, repo: Path, prompt: object) -> None:
    assert err(call(runtime, action="start", project=str(repo), prompt=prompt)) == "invalid_params"


@requires_git
def test_start_rejects_an_oversized_prompt(runtime: rpc.Runtime, repo: Path) -> None:
    huge = "x" * (rpc.MAX_PROMPT_CHARS + 1)
    assert err(call(runtime, action="start", project=str(repo), prompt=huge)) == "invalid_params"


@requires_git
@pytest.mark.parametrize("role", ["bad role", "a/b", 7, "x" * 100])
def test_start_rejects_a_bad_role(runtime: rpc.Runtime, repo: Path, role: object) -> None:
    assert err(call(runtime, action="start", project=str(repo), prompt="p",
                    role=role)) == "invalid_params"


@requires_git
# JSON null means "not supplied" and falls back to the documented default.
@pytest.mark.parametrize("flag", ["yes", 1, 0, "true", []])
def test_start_rejects_a_non_boolean_create_worktree(
    runtime: rpc.Runtime, repo: Path, flag: object
) -> None:
    assert err(call(runtime, action="start", project=str(repo), prompt="p",
                    create_worktree=flag)) == "invalid_params"


def test_start_rejects_a_project_outside_the_root(runtime: rpc.Runtime, tmp_path: Path) -> None:
    assert err(call(runtime, action="start", project="/etc", prompt="p")) == "invalid_project"


def test_start_rejects_a_non_git_project(runtime: rpc.Runtime, paths: RunnerPaths) -> None:
    plain = paths.projects_root / "plain"
    plain.mkdir()
    assert err(call(runtime, action="start", project=str(plain),
                    prompt="p")) == "not_a_git_repository"


@requires_git
def test_start_rejects_oversized_hermes_identifiers(runtime: rpc.Runtime, repo: Path) -> None:
    assert err(call(runtime, action="start", project=str(repo), prompt="p",
                    hermes_session_id="x" * 500)) == "invalid_params"


@requires_git
def test_start_marks_the_run_failed_when_the_worker_cannot_launch(
    runtime: rpc.Runtime, repo: Path
) -> None:
    runtime.spawner.fail = True
    response = call(runtime, action="start", project=str(repo), prompt="p")
    assert err(response) == "worker_spawn_failed"
    run = runtime.store.list_runs()[0]
    assert run["status"] == "failed"
    assert run["error"]


# ── status / events / list ─────────────────────────────────────────────────

@requires_git
def test_status_returns_durable_state(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="working", claude_session_id="sess-9")
    runtime.store.append_event(run_id, "assistant_text", {"text": "working on it"})

    result = ok(call(runtime, action="status", run_id=run_id))
    assert result["run_id"] == run_id
    assert result["status"] == "working"
    assert result["claude_session_id"] == "sess-9"
    assert result["event_high_water"] == runtime.store.event_high_water(run_id)
    assert result["activity"]["kind"] == "assistant_text"
    assert result["worktree"] and result["base_sha"]
    assert result["pending_messages"] == 0
    assert result["stop_requested"] is False
    assert "prompt" in result


def test_status_of_unknown_run_fails_closed(runtime: rpc.Runtime) -> None:
    assert err(call(runtime, action="status", run_id="rdeadbeef")) == "unknown_run"


@pytest.mark.parametrize("run_id", ["", "a b", "../x", None, 5, "x" * 200])
def test_status_rejects_a_malformed_run_id(runtime: rpc.Runtime, run_id: object) -> None:
    assert err(call(runtime, action="status", run_id=run_id)) == "invalid_params"


@requires_git
def test_events_paginate_with_a_cursor(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    for i in range(10):
        runtime.store.append_event(run_id, "assistant_text", {"text": f"step {i}"})

    first = ok(call(runtime, action="events", run_id=run_id, after=0, limit=4))
    assert [e["seq"] for e in first["events"]] == [1, 2, 3, 4]
    assert first["next_cursor"] == 4
    assert first["high_water"] == 13

    second = ok(call(runtime, action="events", run_id=run_id, after=first["next_cursor"]))
    assert second["events"][0]["seq"] == 5
    assert second["next_cursor"] == 13
    assert ok(call(runtime, action="events", run_id=run_id, after=13))["events"] == []


@requires_git
def test_events_limit_is_capped(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    for _ in range(rpc.MAX_EVENT_LIMIT + 50):
        runtime.store.append_event(run_id, "assistant_text", {"text": "x"})
    result = ok(call(runtime, action="events", run_id=run_id, limit=100_000))
    assert len(result["events"]) <= rpc.MAX_EVENT_LIMIT


@requires_git
def test_events_response_is_size_capped(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    for _ in range(200):
        runtime.store.append_event(run_id, "tool_result", {"content": "y" * 7000})

    result = ok(call(runtime, action="events", run_id=run_id, limit=200))
    assert len(json.dumps(result).encode()) <= rpc.MAX_RESPONSE_BYTES
    assert result["truncated"] is True
    assert result["events"], "at least one event must always come back"
    # The cursor must still advance past exactly what was returned.
    assert result["next_cursor"] == result["events"][-1]["seq"]


@pytest.mark.parametrize("after", [-1, "0", 1.5, True, []])
def test_events_rejects_a_bad_cursor(runtime: rpc.Runtime, after: object) -> None:
    assert err(call(runtime, action="events", run_id="rabc", after=after)) == "invalid_params"


@requires_git
def test_list_filters_and_orders(runtime: rpc.Runtime, paths: RunnerPaths) -> None:
    first = make_repo(paths.projects_root / "one")
    second = make_repo(paths.projects_root / "two")
    a = ok(call(runtime, action="start", project=str(first), prompt="p"))["run_id"]
    b = ok(call(runtime, action="start", project=str(second), prompt="p"))["run_id"]
    runtime.store.update_run(a, status="completed")

    everything = ok(call(runtime, action="list"))
    assert [r["run_id"] for r in everything["runs"]] == [b, a]
    assert everything["count"] == 2
    assert [r["run_id"] for r in ok(call(runtime, action="list",
                                         project=str(first)))["runs"]] == [a]
    assert [r["run_id"] for r in ok(call(runtime, action="list",
                                         status="completed"))["runs"]] == [a]


def test_list_rejects_an_unknown_status(runtime: rpc.Runtime) -> None:
    assert err(call(runtime, action="list", status="melting")) == "invalid_params"


def test_list_rejects_an_uncontained_project(runtime: rpc.Runtime) -> None:
    assert err(call(runtime, action="list", project="/etc")) == "invalid_project"


@requires_git
def test_list_entries_stay_summaries(runtime: rpc.Runtime, repo: Path) -> None:
    ok(call(runtime, action="start", project=str(repo), prompt="x" * 5000))
    entry = ok(call(runtime, action="list"))["runs"][0]
    assert len(entry["prompt"]) <= rpc.MAX_SUMMARY_CHARS + 32
    assert {"run_id", "project", "status", "role", "worktree", "created_at"} <= set(entry)


def test_list_limit_is_capped(runtime: rpc.Runtime) -> None:
    result = ok(call(runtime, action="list", limit=10_000))
    assert result["limit"] <= rpc.MAX_LIST_LIMIT


# ── send ───────────────────────────────────────────────────────────────────

@requires_git
def test_send_queues_a_follow_up(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    result = ok(call(runtime, action="send", run_id=run_id, message="also add docs"))
    assert result["queued"] is True
    assert result["pending_messages"] == 1
    assert runtime.store.pending_message_count(run_id) == 1
    assert "follow_up" in [e["kind"] for e in runtime.store.get_events(run_id)]


def test_send_to_unknown_run_fails_closed(runtime: rpc.Runtime) -> None:
    assert err(call(runtime, action="send", run_id="rabc", message="hi")) == "unknown_run"


@requires_git
@pytest.mark.parametrize("message", ["", "  ", None, 3])
def test_send_rejects_a_bad_message(runtime: rpc.Runtime, repo: Path, message: object) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    assert err(call(runtime, action="send", run_id=run_id, message=message)) == "invalid_params"


@requires_git
def test_send_unblocks_a_blocked_run(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="blocked")
    ok(call(runtime, action="send", run_id=run_id, message="yes, go ahead"))
    assert runtime.store.get_run(run_id)["status"] == "blocked"  # the worker clears it
    assert runtime.store.pending_message_count(run_id) == 1


# ── stop ───────────────────────────────────────────────────────────────────

@requires_git
def test_stop_sets_the_flag_and_preserves_state(runtime: rpc.Runtime, repo: Path) -> None:
    started = ok(call(runtime, action="start", project=str(repo), prompt="fix"))
    run_id = started["run_id"]
    runtime.store.update_run(run_id, status="working")

    result = ok(call(runtime, action="stop", run_id=run_id))
    assert result["stop_requested"] is True
    run = runtime.store.get_run(run_id)
    assert run["stop_requested"] is True
    assert run["status"] == "working", "status is the worker's to change"
    assert Path(started["worktree"]).is_dir(), "stop must never remove the worktree"
    assert "stop_requested" in [e["kind"] for e in runtime.store.get_events(run_id)]


@requires_git
def test_stop_on_a_finished_run_is_a_no_op(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="completed")
    result = ok(call(runtime, action="stop", run_id=run_id))
    assert result["already_final"] is True
    assert runtime.store.get_run(run_id)["status"] == "completed"


def test_stop_of_unknown_run_fails_closed(runtime: rpc.Runtime) -> None:
    assert err(call(runtime, action="stop", run_id="rabc")) == "unknown_run"


# ── resume ─────────────────────────────────────────────────────────────────

@requires_git
def test_resume_respawns_by_stored_session_id(runtime: rpc.Runtime, repo: Path) -> None:
    started = ok(call(runtime, action="start", project=str(repo), prompt="fix"))
    run_id = started["run_id"]
    (Path(started["worktree"]) / "work.txt").write_text("progress\n")
    runtime.store.update_run(run_id, status="stopped", claude_session_id="sess-7",
                             stop_requested=True)

    result = ok(call(runtime, action="resume", run_id=run_id, message="now add a second test"))

    assert result["run_id"] == run_id
    assert result["claude_session_id"] == "sess-7"
    assert result["status"] in ("preparing", "working")
    assert runtime.spawner.calls[-1] == {"run_id": run_id, "resume": "sess-7"}
    assert runtime.store.pending_message_count(run_id) == 1
    assert runtime.store.get_run(run_id)["stop_requested"] is False
    assert (Path(started["worktree"]) / "work.txt").read_text() == "progress\n"
    assert result["worktree"] == started["worktree"]


@requires_git
@pytest.mark.parametrize("status", ["stopped", "failed", "blocked", "completed", "unknown"])
def test_resume_accepts_every_resumable_status(
    runtime: rpc.Runtime, repo: Path, status: str
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status=status, claude_session_id="s", worker_pid=None)
    assert ok(call(runtime, action="resume", run_id=run_id, message="go"))["run_id"] == run_id


@requires_git
def test_resume_refuses_a_live_run(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="working", claude_session_id="s")
    assert err(call(runtime, action="resume", run_id=run_id,
                    message="go")) == "run_not_resumable"


@requires_git
def test_resume_without_a_session_id_fails_closed(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="failed")
    assert err(call(runtime, action="resume", run_id=run_id,
                    message="go")) == "no_claude_session"


def test_resume_of_unknown_run_fails_closed(runtime: rpc.Runtime) -> None:
    assert err(call(runtime, action="resume", run_id="rabc", message="go")) == "unknown_run"


@requires_git
def test_resume_records_an_event(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="completed", claude_session_id="s")
    ok(call(runtime, action="resume", run_id=run_id, message="second behaviour"))
    assert "follow_up" in [e["kind"] for e in runtime.store.get_events(run_id)]


# ── failure containment ────────────────────────────────────────────────────

def test_unexpected_handler_failure_becomes_an_internal_error(
    runtime: rpc.Runtime, monkeypatch
) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError('secret detail ' + 'sk' + '-ant-api03-SHOULDNOTLEAKAAAA1234')

    monkeypatch.setattr(runtime.store, "list_runs", boom)
    response = call(runtime, action="list")
    assert err(response) == "internal_error"
    assert "sk-ant" not in response["detail"]


def test_every_brief_action_is_registered() -> None:
    assert rpc.ACTIONS == frozenset(
        {"start", "send", "status", "events", "list", "stop", "resume", "health"}
    )


# ── redaction of operator-supplied text ────────────────────────────────────

SECRET = 'gh' + 'p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'


# Token-shaped material is refused outright (see the gate tests below); the
# looser assignment shapes still reach the store, so summaries stay scrubbed.
HEURISTIC_SECRET = "MY_SERVICE_TOKEN=hunter2hunter2"


@requires_git
def test_a_heuristic_secret_in_the_prompt_is_scrubbed_from_events_and_status(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo),
                     prompt=f"deploy with {HEURISTIC_SECRET} please"))["run_id"]

    events = ok(call(runtime, action="events", run_id=run_id, limit=100))
    status = ok(call(runtime, action="status", run_id=run_id))
    listing = ok(call(runtime, action="list"))

    assert "hunter2hunter2" not in json.dumps(events)
    assert "hunter2hunter2" not in json.dumps(status)
    assert "hunter2hunter2" not in json.dumps(listing)
    assert "[redacted" in status["prompt"]


@requires_git
def test_a_heuristic_secret_in_a_follow_up_is_scrubbed_from_events(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    ok(call(runtime, action="send", run_id=run_id, message=f"token is {HEURISTIC_SECRET}"))
    assert "hunter2hunter2" not in json.dumps(ok(call(runtime, action="events", run_id=run_id)))


@requires_git
def test_a_secret_in_a_stored_result_never_reaches_status(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="completed", result=f"done, used {SECRET}",
                             error=f"failed with {SECRET}")
    status = ok(call(runtime, action="status", run_id=run_id))
    assert SECRET not in json.dumps(status)


# ── secrets are rejected before anything durable is written ────────────────

TOKEN = 'sk' + '-ant-api03-REJECTMEREJECTMEREJECT1234'


@requires_git
def test_start_rejects_a_prompt_that_carries_a_credential(
    runtime: rpc.Runtime, repo: Path
) -> None:
    response = call(runtime, action="start", project=str(repo),
                    prompt=f"deploy using {TOKEN}")
    assert err(response) == "secret_in_payload"
    assert "anthropic_key" in response["detail"]
    assert runtime.store.list_runs() == [], "nothing may be persisted"
    assert runtime.spawner.calls == []


@requires_git
def test_start_keeps_ordinary_prose_about_secrets(runtime: rpc.Runtime, repo: Path) -> None:
    prompt = "fix the 'wrong password: try again' message and rename API_KEY_HEADER"
    run_id = ok(call(runtime, action="start", project=str(repo), prompt=prompt))["run_id"]
    assert runtime.store.get_run(run_id)["prompt"] == prompt, "the prompt must not be corrupted"


@requires_git
def test_send_rejects_a_message_that_carries_a_credential(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    assert err(call(runtime, action="send", run_id=run_id,
                    message=f"use {TOKEN}")) == "secret_in_payload"
    assert runtime.store.pending_message_count(run_id) == 0


@requires_git
def test_resume_rejects_a_message_that_carries_a_credential(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="completed", claude_session_id="s",
                             worker_pid=None)
    before = len(runtime.spawner.calls)
    assert err(call(runtime, action="resume", run_id=run_id,
                    message=f"use {TOKEN}")) == "secret_in_payload"
    assert len(runtime.spawner.calls) == before


@requires_git
def test_no_rejected_credential_reaches_the_database_file(
    runtime: rpc.Runtime, repo: Path, paths
) -> None:
    call(runtime, action="start", project=str(repo), prompt=f"deploy using {TOKEN}")
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    call(runtime, action="send", run_id=run_id, message=f"use {TOKEN}")

    runtime.store.connection.execute("PRAGMA wal_checkpoint(FULL)")
    blob = paths.db_path.read_bytes()
    for suffix in ("-wal", "-shm"):
        sidecar = paths.db_path.with_name(paths.db_path.name + suffix)
        if sidecar.exists():
            blob += sidecar.read_bytes()
    assert TOKEN.encode() not in blob


# ── resume never spawns a second live worker ───────────────────────────────

@requires_git
@pytest.mark.parametrize("status", ["stopped", "failed", "blocked", "completed", "unknown"])
def test_resume_refuses_while_the_stored_worker_is_still_alive(
    runtime: rpc.Runtime, repo: Path, status: str
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status=status, claude_session_id="s")
    before = len(runtime.spawner.calls)

    runtime.spawner.alive = True
    response = call(runtime, action="resume", run_id=run_id, message="go")

    assert err(response) == "run_not_resumable"
    assert len(runtime.spawner.calls) == before, "no second worker may be spawned"


@requires_git
def test_a_second_resume_cannot_spawn_a_second_worker(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="stopped", claude_session_id="s",
                             worker_pid=None, worker_started_at=None)

    first = call(runtime, action="resume", run_id=run_id, message="go")
    spawns_after_first = len(runtime.spawner.calls)
    second = call(runtime, action="resume", run_id=run_id, message="go again")

    assert first["ok"] is True
    assert err(second) == "run_not_resumable"
    assert len(runtime.spawner.calls) == spawns_after_first


@requires_git
def test_resume_liveness_check_uses_the_run_id(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="unknown", claude_session_id="s")
    runtime.spawner.alive = True
    call(runtime, action="resume", run_id=run_id, message="go")
    assert runtime.spawner.liveness_probes[-1]["run_id"] == run_id


# ── stop reaches the worker ────────────────────────────────────────────────

@requires_git
def test_stop_signals_the_live_worker(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="working")
    runtime.spawner.alive = True

    result = ok(call(runtime, action="stop", run_id=run_id))

    assert result["stop_requested"] is True
    assert result["signalled"] is True
    assert runtime.spawner.signals[-1]["run_id"] == run_id
    assert runtime.store.get_run(run_id)["stop_requested"] is True


@requires_git
def test_stop_without_a_live_worker_still_sets_the_flag(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="working")
    result = ok(call(runtime, action="stop", run_id=run_id))
    assert result["stop_requested"] is True
    assert result["signalled"] is False


# ── send needs a live run ──────────────────────────────────────────────────

@requires_git
@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "unknown"])
def test_send_refuses_a_finished_run(runtime: rpc.Runtime, repo: Path, status: str) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status=status)
    response = call(runtime, action="send", run_id=run_id, message="too late")
    assert err(response) == "run_not_live"
    assert "resume" in response["detail"]
    assert runtime.store.pending_message_count(run_id) == 0


@requires_git
@pytest.mark.parametrize("status", ["queued", "preparing", "working", "blocked"])
def test_send_accepts_every_live_status(runtime: rpc.Runtime, repo: Path, status: str) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status=status)
    result = ok(call(runtime, action="send", run_id=run_id, message="carry on"))
    assert result["queued"] is True
    assert result["status"] == status, "send reports the state after enqueueing"
    assert result["pending_messages"] == 1


@requires_git
def test_send_never_reports_queued_for_a_completed_run(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    runtime.store.update_run(run_id, status="completed")
    response = call(runtime, action="send", run_id=run_id, message="hello?")
    assert response["ok"] is False
    assert "queued" not in json.dumps(response.get("result", {}))


# ── event fitting: linear, and honest about the cursor ─────────────────────

@requires_git
def test_a_returned_cursor_never_runs_ahead_of_the_returned_events(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    for _ in range(120):
        runtime.store.append_event(run_id, "tool_result", {"content": "z" * 6000})

    after = 0
    seen = 0
    for _ in range(60):
        page = ok(call(runtime, action="events", run_id=run_id, after=after, limit=200))
        if not page["events"]:
            break
        assert page["events"][0]["seq"] == after + 1, "an event was skipped"
        assert page["next_cursor"] == page["events"][-1]["seq"]
        seen += len(page["events"])
        after = page["next_cursor"]
    assert after == runtime.store.event_high_water(run_id)
    assert seen == after


@requires_git
def test_a_single_oversized_event_is_still_delivered_as_a_stub(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    high_water = runtime.store.event_high_water(run_id)
    runtime.store.append_event(run_id, "blocked", {"questions": ["q" * 400_000]})

    page = ok(call(runtime, action="events", run_id=run_id, after=high_water, limit=10))

    assert len(page["events"]) == 1
    assert page["events"][0]["kind"] == "blocked"
    assert page["events"][0]["payload"]["truncated"] is True
    assert page["truncated"] is True
    assert page["next_cursor"] == high_water + 1, "progress must be possible"


@requires_git
def test_event_fitting_does_not_reserialize_the_whole_page(
    runtime: rpc.Runtime, repo: Path, monkeypatch
) -> None:
    """Counts serialized characters, not wall clock: quadratic work shows up here.

    Dropping one event at a time and re-serializing the remaining list costs
    O(n^2) characters; sizing each event once costs O(n).
    """
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    for _ in range(500):
        runtime.store.append_event(run_id, "tool_result", {"content": "y" * 2000})

    total_payload = sum(
        len(json.dumps(e)) for e in runtime.store.get_events(run_id, limit=500)
    )
    serialized = {"chars": 0}
    real_dumps = rpc.json.dumps

    def counting_dumps(*args, **kwargs):
        rendered = real_dumps(*args, **kwargs)
        serialized["chars"] += len(rendered)
        return rendered

    monkeypatch.setattr(rpc.json, "dumps", counting_dumps)
    page = ok(call(runtime, action="events", run_id=run_id, after=0, limit=500))

    budget = total_payload * 3
    assert serialized["chars"] <= budget, (
        f"quadratic shrinking: serialized {serialized['chars']} chars "
        f"for a {total_payload}-char page"
    )
    assert page["events"]
    assert page["next_cursor"] == page["events"][-1]["seq"]
    assert len(json.dumps(page).encode()) <= rpc.MAX_RESPONSE_BYTES


@requires_git
def test_five_hundred_events_page_through_completely(
    runtime: rpc.Runtime, repo: Path
) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    for i in range(500):
        runtime.store.append_event(run_id, "assistant_text", {"text": f"step {i}"})

    after, collected = 0, []
    while True:
        page = ok(call(runtime, action="events", run_id=run_id, after=after, limit=500))
        if not page["events"]:
            break
        collected += [e["seq"] for e in page["events"]]
        after = page["next_cursor"]
    assert collected == list(range(1, runtime.store.event_high_water(run_id) + 1))


# ── the runner's cap is exact too ──────────────────────────────────────────

@requires_git
@pytest.mark.parametrize("payload_size", [60, 150, 400, 900, 3000])
def test_the_serialized_events_envelope_never_exceeds_the_cap(
    runtime: rpc.Runtime, repo: Path, payload_size: int
) -> None:
    """The bound must hold on what actually goes out, not on an approximation."""
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    for _ in range(rpc.MAX_EVENT_LIMIT):
        runtime.store.append_event(run_id, "tool_result", {"content": "y" * payload_size})

    response = call(runtime, action="events", run_id=run_id, after=0,
                    limit=rpc.MAX_EVENT_LIMIT)
    wire = rpc.serialize_response(response)

    assert response["ok"] is True
    assert len(wire.encode()) <= rpc.MAX_RESPONSE_BYTES, len(wire.encode())
    result = response["result"]
    assert result["events"]
    assert result["next_cursor"] == result["events"][-1]["seq"]


@requires_git
def test_the_runner_counts_list_separators_at_their_real_width(
    runtime: rpc.Runtime, repo: Path
) -> None:
    assert rpc._LIST_SEPARATOR_BYTES == len(
        rpc.serialize_response([0, 0])  # type: ignore[arg-type]
    ) - len(rpc.serialize_response([0])) - 1  # type: ignore[arg-type]


@requires_git
def test_non_ascii_events_respect_the_runner_cap(runtime: rpc.Runtime, repo: Path) -> None:
    run_id = ok(call(runtime, action="start", project=str(repo), prompt="fix"))["run_id"]
    for _ in range(400):
        runtime.store.append_event(run_id, "assistant_text", {"text": "ü" * 900})

    response = call(runtime, action="events", run_id=run_id, after=0, limit=400)

    assert len(rpc.serialize_response(response).encode()) <= rpc.MAX_RESPONSE_BYTES
    assert response["result"]["events"]
