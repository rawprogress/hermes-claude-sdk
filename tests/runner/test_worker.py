"""The per-run worker loop, driven by a fake SDK client."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from hermes_claude_runner import worker
from hermes_claude_runner.config import RunnerPaths
from hermes_claude_runner.store import Store

from . import fakes

FAST = {"poll_interval": 0.005, "block_timeout": 2.0}


@pytest.fixture()
def store(paths: RunnerPaths) -> Store:
    s = Store.open(paths.db_path)
    yield s
    s.close()


def seed(store: Store, paths: RunnerPaths, tmp_path: Path, **kw) -> str:
    work = tmp_path / "worktree"
    work.mkdir(exist_ok=True)
    params = dict(run_id="rtest0001", project=str(tmp_path / "repo"), role="implementer",
                  prompt="fix the failing test")
    params.update(kw)
    store.create_run(**params)
    store.update_run(params["run_id"], worktree=str(work), branch="hermes/test0001",
                     base_sha="a" * 40)
    return str(params["run_id"])


async def run(store: Store, paths: RunnerPaths, run_id: str, script, **kw) -> str:
    make = fakes.factory(script)
    status = await worker.run_worker(
        run_id=run_id, store=store, paths=paths, client_factory=make, **{**FAST, **kw}
    )
    return status, make.clients[0]  # type: ignore[return-value]


async def _answer(store: Store, run_id: str, text: str) -> None:
    for _ in range(600):
        if store.get_run(run_id)["status"] == "blocked":
            store.enqueue_if_active(run_id, text)
            return
        await asyncio.sleep(0.005)
    raise AssertionError("run never reached blocked")


def kinds(store: Store, run_id: str) -> list[str]:
    return [e["kind"] for e in store.get_events(run_id, limit=500)]


# ── happy path ─────────────────────────────────────────────────────────────

async def test_single_turn_run_completes(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    status, client = await run(store, paths, run_id, [
        [fakes.init("sess-9"), fakes.say("Fixed it."), fakes.result("3 tests pass", "sess-9")],
    ])

    assert status == "completed"
    record = store.get_run(run_id)
    assert record["status"] == "completed"
    assert record["claude_session_id"] == "sess-9"
    assert record["result"] == "3 tests pass"
    assert record["finished_at"]
    assert client.connected and client.disconnected
    assert client.queries == ["[hermes role: implementer]\n\nfix the failing test"]
    assert kinds(store, run_id) == ["system", "assistant_text", "result", "worker_exit"]


async def test_worker_runs_in_the_worktree(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    _status, client = await run(store, paths, run_id, [[fakes.result()]])
    assert Path(client.options.cwd) == tmp_path / "worktree"


async def test_status_is_working_while_the_turn_runs(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    seen: list[str] = []

    async def peek(_client):
        seen.append(store.get_run(run_id)["status"])
        return None

    await run(store, paths, run_id, [[peek, fakes.result()]])
    assert seen == ["working"]


async def test_tool_activity_is_recorded(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    await run(store, paths, run_id, [
        [fakes.use_tool("Bash", command="pytest -q"), fakes.result()],
    ])
    assert "tool_use" in kinds(store, run_id)


# ── mailbox ────────────────────────────────────────────────────────────────

async def test_pending_follow_ups_become_further_turns(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)

    async def queue_follow_up(_client):
        store.enqueue_message(run_id, "also add a changelog")
        return None

    _status, client = await run(store, paths, run_id, [
        [fakes.init(), queue_follow_up, fakes.result("first turn")],
        [fakes.say("Added it."), fakes.result("second turn")],
    ])
    assert client.queries == [
        "[hermes role: implementer]\n\nfix the failing test",
        "also add a changelog",
    ]
    assert store.get_run(run_id)["result"] == "second turn"


async def test_several_follow_ups_are_delivered_in_order(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)

    async def queue_two(_client):
        store.enqueue_message(run_id, "one")
        store.enqueue_message(run_id, "two")
        return None

    _status, client = await run(store, paths, run_id, [
        [queue_two, fakes.result()], [fakes.result()], [fakes.result()],
    ])
    assert client.queries[1:] == ["one", "two"]


# ── resume ─────────────────────────────────────────────────────────────────

async def test_resume_uses_the_stored_session_and_the_mailbox_prompt(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)
    store.update_run(run_id, status="stopped", claude_session_id="sess-7")
    store.enqueue_message(run_id, "now add a second tested behaviour")

    status, client = await run(store, paths, run_id, [[fakes.result("done", "sess-7")]],
                               resume="sess-7")

    assert status == "completed"
    assert client.options.resume == "sess-7"
    assert client.queries == ["now add a second tested behaviour"]


async def test_resume_without_a_queued_message_still_prompts(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    store.update_run(run_id, status="stopped", claude_session_id="sess-7")
    _status, client = await run(store, paths, run_id, [[fakes.result()]], resume="sess-7")
    assert client.queries and client.queries[0].strip()


async def test_session_id_is_updated_when_the_sdk_changes_it(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    store.update_run(run_id, claude_session_id="old-session")
    await run(store, paths, run_id, [[fakes.init("brand-new"), fakes.result("x", "brand-new")]])
    assert store.get_run(run_id)["claude_session_id"] == "brand-new"


# ── stop ───────────────────────────────────────────────────────────────────

async def test_stop_interrupts_and_preserves_state(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)

    async def request_stop_and_wait(client):
        store.request_stop(run_id)
        for _ in range(600):
            if client.interrupted:
                return None
            await asyncio.sleep(0.005)
        raise AssertionError("stop flag never reached the client")

    status, client = await run(store, paths, run_id, [
        [fakes.init("sess-3"), request_stop_and_wait, fakes.say("never sent")],
    ])

    assert status == "stopped"
    record = store.get_run(run_id)
    assert record["status"] == "stopped"
    assert record["claude_session_id"] == "sess-3", "session must survive a stop"
    assert record["worktree"] and Path(record["worktree"]).is_dir()
    assert client.interrupted >= 1
    assert "stopped" in kinds(store, run_id)


async def test_stop_requested_before_start_finishes_immediately(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    store.request_stop(run_id)
    status, client = await run(store, paths, run_id, [[fakes.result()]])
    assert status == "stopped"
    assert client.queries == []


async def test_stop_between_turns_prevents_the_next_prompt(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)

    async def queue_and_stop(_client):
        store.enqueue_message(run_id, "another turn please")
        store.request_stop(run_id)
        return None

    status, client = await run(store, paths, run_id, [
        [queue_and_stop, fakes.result()], [fakes.result()],
    ])
    assert status == "stopped"
    assert len(client.queries) == 1


# ── failure handling ───────────────────────────────────────────────────────

async def test_error_result_fails_the_run(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    status, _ = await run(store, paths, run_id, [
        [fakes.result("hit the turn limit", is_error=True, subtype="error_max_turns")],
    ])
    assert status == "failed"
    record = store.get_run(run_id)
    assert record["status"] == "failed"
    assert "hit the turn limit" in record["error"]


async def test_sdk_exception_fails_the_run_and_never_invents_success(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)

    async def explode(_client):
        raise RuntimeError('cli died with token ' + 'sk' + '-ant-api03-LEAKYLEAKYLEAKY123')

    status, _ = await run(store, paths, run_id, [[explode]])
    record = store.get_run(run_id)
    assert status == "failed"
    assert record["status"] == "failed"
    assert record["result"] is None
    assert "sk-ant" not in (record["error"] or "")
    assert "error" in kinds(store, run_id)


async def test_missing_run_is_reported_as_unknown(store, paths) -> None:
    make = fakes.factory([[fakes.result()]])
    status = await worker.run_worker(run_id="rghost0001", store=store, paths=paths,
                                     client_factory=make, **FAST)
    assert status == "unknown"


async def test_missing_worktree_fails_before_connecting(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    store.update_run(run_id, worktree=str(tmp_path / "gone"))
    make = fakes.factory([[fakes.result()]])
    status = await worker.run_worker(run_id=run_id, store=store, paths=paths,
                                     client_factory=make, **FAST)
    assert status == "failed"
    assert make.clients == []  # type: ignore[attr-defined]


async def test_worker_always_disconnects(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)

    async def explode(_client):
        raise RuntimeError("boom")

    _status, client = await run(store, paths, run_id, [[explode]])
    assert client.disconnected


# ── escalated tools (AskUserQuestion) ──────────────────────────────────────

QUESTION_INPUT = {
    "questions": [
        {
            "question": "Which database should the new service use?",
            "header": "Database",
            "multiSelect": False,
            "options": [
                {"label": "Postgres", "description": "Matches the rest of the fleet"},
                {"label": "SQLite", "description": "Simplest for a prototype"},
            ],
        }
    ]
}


def hook_of(client) -> object:
    return client.options.hooks["PreToolUse"][0].hooks[0]


async def test_ordinary_tools_pass_through_the_hook_untouched(store, paths, tmp_path) -> None:
    decisions: list[object] = []

    async def use_bash(client):
        decisions.append(await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": "pytest -q"}, "tool_use_id": "tu_1"},
            "tu_1", {"signal": None},
        ))
        return None

    run_id = seed(store, paths, tmp_path)
    status, _ = await run(store, paths, run_id, [[use_bash, fakes.result()]])
    assert status == "completed"
    assert decisions == [{}], "bypassPermissions must stay bypassed for ordinary tools"
    assert "blocked" not in kinds(store, run_id)


async def test_ask_user_question_blocks_with_the_structured_question(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)
    decisions: list[dict] = []

    async def ask(client):
        task = asyncio.create_task(_answer(store, run_id, "Postgres"))
        decisions.append(await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
             "tool_input": QUESTION_INPUT, "tool_use_id": "tu_q"},
            "tu_q", {"signal": None},
        ))
        await task
        return None

    status, _ = await run(store, paths, run_id, [[ask, fakes.result()]])

    assert status == "completed"
    blocked = next(e for e in store.get_events(run_id, limit=500) if e["kind"] == "blocked")
    payload = blocked["payload"]
    assert payload["tool"] == "AskUserQuestion"
    assert payload["tool_use_id"] == "tu_q"
    question = payload["questions"][0]
    assert question["question"] == "Which database should the new service use?"
    assert question["header"] == "Database"
    assert question["multi_select"] is False
    assert [o["label"] for o in question["options"]] == ["Postgres", "SQLite"]
    assert question["options"][0]["description"] == "Matches the rest of the fleet"


async def test_the_mailbox_answer_is_delivered_to_the_model(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    decisions: list[dict] = []

    async def ask(client):
        task = asyncio.create_task(_answer(store, run_id, "Postgres"))
        decisions.append(await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
             "tool_input": QUESTION_INPUT, "tool_use_id": "tu_q"},
            "tu_q", {"signal": None},
        ))
        await task
        return None

    await run(store, paths, run_id, [[ask, fakes.result()]])

    specific = decisions[0]["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "Postgres" in specific["permissionDecisionReason"]
    unblocked = next(e for e in store.get_events(run_id, limit=500) if e["kind"] == "unblocked")
    assert unblocked["payload"]["answered"] is True


async def test_the_run_is_blocked_while_it_waits_and_working_afterwards(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)

    async def ask(client):
        task = asyncio.create_task(_answer(store, run_id, "SQLite"))
        await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
             "tool_input": QUESTION_INPUT, "tool_use_id": "tu_q"},
            "tu_q", {"signal": None},
        )
        await task
        assert store.get_run(run_id)["status"] == "working"
        return None

    await run(store, paths, run_id, [[ask, fakes.result()]])
    assert store.get_run(run_id)["status"] == "completed"


async def test_an_unanswered_question_denies_after_the_block_timeout(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)
    decisions: list[dict] = []

    async def ask(client):
        decisions.append(await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
             "tool_input": QUESTION_INPUT, "tool_use_id": "tu_q"},
            "tu_q", {"signal": None},
        ))
        return None

    await run(store, paths, run_id, [[ask, fakes.result()]], block_timeout=0.05)

    specific = decisions[0]["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "no answer" in specific["permissionDecisionReason"].lower()
    unblocked = next(e for e in store.get_events(run_id, limit=500) if e["kind"] == "unblocked")
    assert unblocked["payload"]["answered"] is False
    assert store.get_run(run_id)["status"] == "completed"


async def test_the_escalation_set_is_configurable(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    decisions: list[dict] = []

    async def ask(client):
        task = asyncio.create_task(_answer(store, run_id, "go ahead"))
        decisions.append(await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": "rm -rf /"}, "tool_use_id": "tu_b"},
            "tu_b", {"signal": None},
        ))
        await task
        return None

    await run(store, paths, run_id, [[ask, fakes.result()]], escalate_tools=("Bash",))
    assert decisions[0]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "blocked" in kinds(store, run_id)


async def test_a_question_payload_is_redacted_and_bounded(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    secret = 'gh' + 'p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'

    async def ask(client):
        task = asyncio.create_task(_answer(store, run_id, "ok"))
        await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
             "tool_input": {"questions": [{"question": f"use {secret}?", "header": "h",
                                           "options": [{"label": "x" * 5000}]}]},
             "tool_use_id": "tu_q"},
            "tu_q", {"signal": None},
        )
        await task
        return None

    await run(store, paths, run_id, [[ask, fakes.result()]])
    blocked = next(e for e in store.get_events(run_id, limit=500) if e["kind"] == "blocked")
    dumped = json.dumps(blocked["payload"])
    assert secret not in dumped
    assert len(dumped) < 20_000


async def test_a_malformed_question_payload_still_blocks(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)

    async def ask(client):
        task = asyncio.create_task(_answer(store, run_id, "ok"))
        await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
             "tool_input": {"questions": "not a list"}, "tool_use_id": "tu_q"},
            "tu_q", {"signal": None},
        )
        await task
        return None

    await run(store, paths, run_id, [[ask, fakes.result()]])
    blocked = next(e for e in store.get_events(run_id, limit=500) if e["kind"] == "blocked")
    assert blocked["payload"]["questions"] == []
    assert blocked["payload"]["raw_input"]


async def test_a_stop_during_a_question_releases_the_hook(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    decisions: list[dict] = []

    async def ask(client):
        store.request_stop(run_id)
        decisions.append(await hook_of(client)(
            {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
             "tool_input": QUESTION_INPUT, "tool_use_id": "tu_q"},
            "tu_q", {"signal": None},
        ))
        return None

    status, _ = await run(store, paths, run_id, [[ask, fakes.result()]], block_timeout=30)
    assert status == "stopped"
    assert decisions[0]["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_sdk_warning_is_suppressed_in_the_worker() -> None:
    """The shadowed-callback warning is solved by removing the callback."""
    source = Path(worker.__file__).read_text()
    assert "filterwarnings" not in source
    assert "CanUseToolShadowedWarning" not in source




# ── redaction of durable columns ───────────────────────────────────────────

async def test_a_secret_in_the_result_is_not_stored_verbatim(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    secret = 'sk' + '-ant-api03-MUSTNOTREACHTHEDB1234'
    await run(store, paths, run_id, [[fakes.result(f"all done with {secret}")]])
    record = store.get_run(run_id)
    assert secret not in (record["result"] or "")
    assert "[redacted" in record["result"]


async def test_a_secret_in_an_error_result_is_not_stored_verbatim(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)
    secret = 'gh' + 'p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'
    await run(store, paths, run_id, [[fakes.result(f"crashed: {secret}", is_error=True)]])
    assert secret not in (store.get_run(run_id)["error"] or "")


# ── external (signal-driven) stop ──────────────────────────────────────────

async def test_an_external_stop_event_stops_the_run_before_the_first_query(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)
    event = threading.Event()
    event.set()
    status, client = await run(store, paths, run_id, [[fakes.result()]], external_stop=event)
    assert status == "stopped"
    assert client.queries == []


async def test_an_external_stop_event_interrupts_a_live_turn(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    event = threading.Event()

    async def signal_then_wait(client):
        event.set()  # what the SIGTERM handler does, without touching the database
        for _ in range(600):
            if client.interrupted:
                return None
            await asyncio.sleep(0.005)
        raise AssertionError("the external stop never reached the client")

    status, client = await run(store, paths, run_id, [
        [fakes.init("sess-5"), signal_then_wait, fakes.say("never sent")],
    ], external_stop=event)

    assert status == "stopped"
    record = store.get_run(run_id)
    assert record["claude_session_id"] == "sess-5"
    assert Path(record["worktree"]).is_dir()
    assert client.interrupted >= 1


# ── exactly one completion writer ──────────────────────────────────────────

async def test_a_follow_up_landing_at_completion_time_is_never_lost(
    store, paths, tmp_path
) -> None:
    """The mailbox is claimed in the same transaction that would complete."""
    run_id = seed(store, paths, tmp_path)

    async def queue_at_the_last_moment(_client):
        store.enqueue_if_active(run_id, "one more thing")
        return None

    status, client = await run(store, paths, run_id, [
        [queue_at_the_last_moment, fakes.result("first")],
        [fakes.result("second")],
    ])
    assert status == "completed"
    assert client.queries[-1] == "one more thing"
    assert store.pending_message_count(run_id) == 0


async def test_a_completed_run_refuses_further_follow_ups(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    await run(store, paths, run_id, [[fakes.result("done")]])
    assert store.enqueue_if_active(run_id, "too late") == (False, "completed")


async def test_completion_is_written_exactly_once(store, paths, tmp_path) -> None:
    run_id = seed(store, paths, tmp_path)
    await run(store, paths, run_id, [[fakes.result("3 tests pass")]])
    run_record = store.get_run(run_id)
    assert run_record["status"] == "completed"
    assert run_record["result"] == "3 tests pass"
    assert [e["kind"] for e in store.get_events(run_id)].count("worker_exit") == 1


# ── a turn without a result is never a completion ──────────────────────────

async def test_a_turn_without_a_result_message_fails_honestly(
    store, paths, tmp_path
) -> None:
    """The SDK stream ending with no ResultMessage is not evidence of success."""
    run_id = seed(store, paths, tmp_path)

    async def say_only(_client):
        return fakes.say("I did some work")

    status, _ = await run(store, paths, run_id, [[fakes.init("sess-1"), say_only]])

    assert status == "failed"
    record = store.get_run(run_id)
    assert record["status"] == "failed"
    assert record["result"] is None, "no result may be invented"
    assert "without a result" in record["error"]
    assert record["claude_session_id"] == "sess-1", "the session must survive for a resume"


async def test_a_resumed_turn_without_a_result_keeps_the_earlier_result(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)
    store.update_run(run_id, status="completed", result="3 tests pass at a1b2c3d",
                     claude_session_id="sess-7")

    async def nothing(_client):
        return None

    status, _ = await run(store, paths, run_id, [[nothing]], resume="sess-7")

    assert status == "failed"
    record = store.get_run(run_id)
    assert record["result"] == "3 tests pass at a1b2c3d", "earlier evidence was erased"
    assert record["error"]


async def test_an_empty_turn_does_not_silently_complete_via_the_mailbox(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)

    async def nothing(_client):
        return None

    await run(store, paths, run_id, [[nothing]])
    assert store.get_run(run_id)["status"] != "completed"
    assert "worker_exit" in kinds(store, run_id)


async def test_a_stopped_turn_without_a_result_is_still_stopped(
    store, paths, tmp_path
) -> None:
    run_id = seed(store, paths, tmp_path)

    async def stop_then_wait(client):
        store.request_stop(run_id)
        for _ in range(600):
            if client.interrupted:
                return None
            await asyncio.sleep(0.005)
        raise AssertionError("stop never reached the client")

    status, _ = await run(store, paths, run_id, [[stop_then_wait, fakes.say("unsent")]])
    assert status == "stopped"
