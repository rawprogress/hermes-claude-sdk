"""Durable state: runs, monotonic events, mailbox."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hermes_claude_runner.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "data.db")
    yield s
    s.close()


def _run(store: Store, run_id: str = "run-1", **kw) -> dict:
    params = dict(
        run_id=run_id, project="/Users/x/Projects/demo", role="implementer",
        prompt="fix the failing test", hermes_session_id="hs-1", hermes_task_id="ht-1",
        create_worktree=True,
    )
    params.update(kw)
    return store.create_run(**params)


def test_create_run_persists_every_field(store: Store) -> None:
    run = _run(store)
    assert run["status"] == "queued"
    assert run["hermes_session_id"] == "hs-1"
    assert run["hermes_task_id"] == "ht-1"
    assert run["create_worktree"] is True
    assert run["created_at"] and run["updated_at"]
    assert store.get_run("run-1") == run


def test_get_unknown_run_returns_none(store: Store) -> None:
    assert store.get_run("nope") is None


def test_create_run_rejects_duplicate_id(store: Store) -> None:
    _run(store)
    with pytest.raises(ValueError, match="exists"):
        _run(store)


def test_update_run_touches_updated_at(store: Store) -> None:
    created = _run(store)
    updated = store.update_run("run-1", status="working", worker_pid=4242)
    assert updated["status"] == "working"
    assert updated["worker_pid"] == 4242
    assert updated["updated_at"] >= created["updated_at"]


def test_update_run_rejects_unknown_column(store: Store) -> None:
    _run(store)
    with pytest.raises(ValueError, match="column"):
        store.update_run("run-1", nonsense=1)


def test_update_unknown_run_returns_none(store: Store) -> None:
    assert store.update_run("ghost", status="working") is None


def test_terminal_status_sets_finished_at(store: Store) -> None:
    _run(store)
    assert store.update_run("run-1", status="working")["finished_at"] is None
    assert store.update_run("run-1", status="completed")["finished_at"]


def test_working_status_sets_started_at_once(store: Store) -> None:
    _run(store)
    first = store.update_run("run-1", status="working")["started_at"]
    assert first
    store.update_run("run-1", status="blocked")
    assert store.update_run("run-1", status="working")["started_at"] == first


def test_list_runs_is_newest_first_and_filterable(store: Store) -> None:
    _run(store, "run-1", project="/p/a")
    _run(store, "run-2", project="/p/b")
    _run(store, "run-3", project="/p/a")
    store.update_run("run-2", status="completed")

    assert [r["run_id"] for r in store.list_runs()] == ["run-3", "run-2", "run-1"]
    assert [r["run_id"] for r in store.list_runs(project="/p/a")] == ["run-3", "run-1"]
    assert [r["run_id"] for r in store.list_runs(status="completed")] == ["run-2"]
    assert [r["run_id"] for r in store.list_runs(limit=1)] == ["run-3"]


def test_append_event_sequences_monotonically_per_run(store: Store) -> None:
    _run(store, "run-1")
    _run(store, "run-2")
    assert store.append_event("run-1", "system", {"a": 1}) == 1
    assert store.append_event("run-1", "assistant_text", {"text": "hi"}) == 2
    assert store.append_event("run-2", "system", {}) == 1
    assert store.event_high_water("run-1") == 2
    assert store.event_high_water("run-2") == 1


def test_event_high_water_of_empty_run_is_zero(store: Store) -> None:
    _run(store)
    assert store.event_high_water("run-1") == 0


def test_get_events_paginates_with_after_cursor(store: Store) -> None:
    _run(store)
    for i in range(5):
        store.append_event("run-1", "system", {"i": i})

    page = store.get_events("run-1", after=0, limit=3)
    assert [e["seq"] for e in page] == [1, 2, 3]
    assert page[0]["payload"] == {"i": 0}
    assert page[0]["kind"] == "system"
    assert [e["seq"] for e in store.get_events("run-1", after=3, limit=100)] == [4, 5]
    assert store.get_events("run-1", after=5) == []


def test_concurrent_writers_never_collide_on_seq(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    with Store.open(path) as bootstrap:
        _run(bootstrap)

    seqs: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        with Store.open(path) as s:
            for _ in range(20):
                seq = s.append_event("run-1", "system", {})
                with lock:
                    seqs.append(seq)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(seqs) == list(range(1, 81))


def test_mailbox_round_trip(store: Store) -> None:
    _run(store)
    store.enqueue_message("run-1", "please also add docs")
    store.enqueue_message("run-1", "and a changelog")
    assert store.pending_message_count("run-1") == 2

    taken = store.take_pending_messages("run-1")
    assert [m["body"] for m in taken] == ["please also add docs", "and a changelog"]
    assert store.take_pending_messages("run-1") == []
    assert store.pending_message_count("run-1") == 0


def test_take_pending_messages_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    with Store.open(path) as s:
        _run(s)
        for i in range(50):
            s.enqueue_message("run-1", f"m{i}")

    seen: list[str] = []
    lock = threading.Lock()

    def drain() -> None:
        with Store.open(path) as s:
            for _ in range(10):
                for msg in s.take_pending_messages("run-1"):
                    with lock:
                        seen.append(msg["body"])

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(seen) == sorted(f"m{i}" for i in range(50))


def test_request_stop_sets_flag(store: Store) -> None:
    _run(store)
    assert store.request_stop("run-1") is True
    assert store.get_run("run-1")["stop_requested"] is True
    assert store.request_stop("ghost") is False


def test_claim_active_runs_for_reconciliation(store: Store) -> None:
    _run(store, "active-1")
    _run(store, "done-1")
    store.update_run("active-1", status="working", worker_pid=1)
    store.update_run("done-1", status="completed")
    assert [r["run_id"] for r in store.active_runs()] == ["active-1"]


def test_claim_for_resume_admits_exactly_one_caller(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    with Store.open(path) as bootstrap:
        _run(bootstrap)
        bootstrap.update_run("run-1", status="stopped", claude_session_id="s")

    wins: list[bool] = []
    lock = threading.Lock()

    def claim() -> None:
        with Store.open(path) as s:
            won = s.claim_for_resume("run-1")
            with lock:
                wins.append(won)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wins.count(True) == 1, "a race must never spawn two workers"
    assert Store.open(path).get_run("run-1")["status"] == "preparing"


def test_claim_for_resume_refuses_a_live_status(store: Store) -> None:
    _run(store)
    store.update_run("run-1", status="working")
    assert store.claim_for_resume("run-1") is False
    assert store.get_run("run-1")["status"] == "working"


def test_claim_for_resume_clears_the_stop_flag_and_error(store: Store) -> None:
    _run(store)
    store.update_run("run-1", status="failed", error="boom", stop_requested=True)
    assert store.claim_for_resume("run-1") is True
    run = store.get_run("run-1")
    assert run["stop_requested"] is False
    assert run["error"] is None
    assert run["finished_at"] is None


def test_claim_for_resume_drops_the_previous_worker_identity(store: Store) -> None:
    """Otherwise a reconcile sweep between the claim and the spawn sees a dead
    pid with an expired grace period and marks the run unknown for no reason."""
    _run(store)
    store.update_run("run-1", status="stopped", worker_pid=999_999,
                     worker_started_at=1_000.0)
    assert store.claim_for_resume("run-1") is True
    run = store.get_run("run-1")
    assert run["worker_pid"] is None
    assert run["worker_started_at"] is None


def test_claim_for_resume_of_unknown_run_is_false(store: Store) -> None:
    assert store.claim_for_resume("ghost") is False


# ── send / completion race ─────────────────────────────────────────────────

def test_enqueue_if_active_accepts_a_live_run(store: Store) -> None:
    _run(store)
    store.update_run("run-1", status="working")
    assert store.enqueue_if_active("run-1", "hello") == (True, "working")
    assert store.pending_message_count("run-1") == 1


def test_enqueue_if_active_accepts_a_blocked_run(store: Store) -> None:
    # This is how an answer reaches a run waiting on a question.
    _run(store)
    store.update_run("run-1", status="blocked")
    assert store.enqueue_if_active("run-1", "allow") == (True, "blocked")


@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "unknown"])
def test_enqueue_if_active_refuses_a_finished_run(store: Store, status: str) -> None:
    _run(store)
    store.update_run("run-1", status=status)
    assert store.enqueue_if_active("run-1", "hello") == (False, status)
    assert store.pending_message_count("run-1") == 0


def test_enqueue_if_active_refuses_an_unknown_run(store: Store) -> None:
    assert store.enqueue_if_active("ghost", "hello") == (False, None)


def test_claim_pending_or_finalize_returns_queued_messages(store: Store) -> None:
    _run(store)
    store.update_run("run-1", status="working")
    store.enqueue_message("run-1", "one")
    messages, final = store.claim_pending_or_finalize("run-1", result="ignored")
    assert [m["body"] for m in messages] == ["one"]
    assert final is None
    assert store.get_run("run-1")["status"] == "working"


def test_claim_pending_or_finalize_completes_an_empty_mailbox(store: Store) -> None:
    _run(store)
    store.update_run("run-1", status="working")
    messages, final = store.claim_pending_or_finalize("run-1", result="3 tests pass")
    assert messages == []
    assert final == "completed"
    run = store.get_run("run-1")
    assert run["status"] == "completed"
    assert run["result"] == "3 tests pass"
    assert run["finished_at"]


def test_claim_pending_or_finalize_honours_a_stop_request(store: Store) -> None:
    _run(store)
    store.update_run("run-1", status="working")
    store.request_stop("run-1")
    store.enqueue_message("run-1", "ignored while stopping")
    messages, final = store.claim_pending_or_finalize("run-1", result="x")
    assert messages == []
    assert final == "stopped"
    assert store.get_run("run-1")["status"] == "working", "the worker writes the final status"


def test_claim_pending_or_finalize_reports_a_vanished_run(store: Store) -> None:
    assert store.claim_pending_or_finalize("ghost", result=None) == ([], "unknown")


def _race_send(path: Path, run_id: str, barrier: threading.Barrier, out: dict) -> None:
    with Store.open(path) as s:
        barrier.wait()
        out["send"] = s.enqueue_if_active(run_id, "late follow-up")


def _race_finish(path: Path, run_id: str, barrier: threading.Barrier, out: dict) -> None:
    with Store.open(path) as s:
        barrier.wait()
        out["finish"] = s.claim_pending_or_finalize(run_id, result="done")


def test_send_and_completion_never_both_win(tmp_path: Path) -> None:
    """The invariant: a queued message implies the run was not finalized."""
    path = tmp_path / "data.db"
    violations: list[str] = []

    for attempt in range(40):
        run_id = f"race-{attempt}"
        with Store.open(path) as s:
            _run(s, run_id)
            s.update_run(run_id, status="working")

        results: dict[str, object] = {}
        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(target=_race_send, args=(path, run_id, barrier, results)),
            threading.Thread(target=_race_finish, args=(path, run_id, barrier, results)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        queued, _status = results["send"]  # type: ignore[misc]
        messages, final = results["finish"]  # type: ignore[misc]
        with Store.open(path) as s:
            run_status = s.get_run(run_id)["status"]
            still_pending = s.pending_message_count(run_id)

        if queued and run_status == "completed" and still_pending:
            violations.append(f"{run_id}: queued into a completed run")
        if not queued and final is None and not messages:
            violations.append(f"{run_id}: message lost")

    assert violations == [], violations


def test_finalizing_without_a_new_result_keeps_the_previous_one(store: Store) -> None:
    _run(store)
    store.update_run("run-1", status="completed", result="first run evidence")
    store.update_run("run-1", status="working")
    messages, final = store.claim_pending_or_finalize("run-1", result=None)
    assert (messages, final) == ([], "completed")
    assert store.get_run("run-1")["result"] == "first run evidence"


def test_finalizing_with_a_new_result_replaces_the_previous_one(store: Store) -> None:
    _run(store)
    store.update_run("run-1", status="working", result="old")
    store.claim_pending_or_finalize("run-1", result="new")
    assert store.get_run("run-1")["result"] == "new"
