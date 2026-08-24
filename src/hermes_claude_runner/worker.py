"""One OS process per run: drives a long-lived ``ClaudeSDKClient``.

The worker owns the run's status while it is alive. It never invents a
result: an exception fails the run, a stop flag stops it, and a lost process
is reconciled to ``unknown`` by the daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import events as event_serializer
from . import models, sdk_adapter
from .config import RunnerPaths
from .redaction import redact_text
from .store import Store

POLL_INTERVAL_SECONDS = 1.0
# The SDK caps a hook at its matcher timeout, so this also bounds how long a
# blocked run can wait. For a longer pause use claude_stop plus claude_resume.
BLOCK_TIMEOUT_SECONDS = 900.0
MAX_QUESTIONS = 10
MAX_OPTIONS = 20
MAX_QUESTION_CHARS = 2000

ClientFactory = Callable[[Any], Any]


class _Session:
    """Mutable per-run state shared between the turn loop and its watchers."""

    def __init__(
        self,
        run_id: str,
        store: Store,
        poll_interval: float,
        external_stop: threading.Event | None = None,
    ) -> None:
        self.run_id = run_id
        self.store = store
        self.poll_interval = poll_interval
        self.stop_requested = False
        # Set by the SIGTERM handler, so a signalled stop lands without waiting
        # for the database poll — and without needing the database at all.
        self.external_stop = external_stop or threading.Event()

    def stop_flag_set(self) -> bool:
        if self.external_stop.is_set():
            return True
        run = self.store.get_run(self.run_id)
        return bool(run and run["stop_requested"])

    def stop_on_record(self) -> bool:
        """Like :meth:`stop_flag_set`, but safe to ask while handling a failure.

        Whatever just broke may be the store itself, so an unreadable flag
        means "no stop was requested" — a genuine failure is never rewritten
        into a stop.
        """
        if self.stop_requested or self.external_stop.is_set():
            return True
        try:
            return self.stop_flag_set()
        except Exception:  # noqa: BLE001 - a broken store must not mask the failure
            return False

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.store.append_event(self.run_id, kind, payload)


async def _watch_for_stop(session: _Session, client: Any) -> None:
    """Interrupt the live turn as soon as a stop is requested."""
    while True:
        await asyncio.sleep(session.poll_interval)
        if session.stop_requested or session.external_stop.is_set() or session.stop_flag_set():
            session.stop_requested = True
            with contextlib.suppress(Exception):
                await client.interrupt()
            return


def _questions_from_input(tool_input: Any) -> list[dict[str, Any]]:
    """Extract AskUserQuestion's structured questions, redacted and bounded."""
    raw = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(raw, list):
        return []
    questions: list[dict[str, Any]] = []
    for entry in raw[:MAX_QUESTIONS]:
        if not isinstance(entry, dict):
            continue
        options = []
        for option in (entry.get("options") or [])[:MAX_OPTIONS]:
            if isinstance(option, dict):
                options.append({
                    "label": _clean_field(option.get("label")),
                    "description": _clean_field(option.get("description")),
                })
            else:
                options.append({"label": _clean_field(option), "description": ""})
        questions.append({
            "question": _clean_field(entry.get("question")),
            "header": _clean_field(entry.get("header")),
            "multi_select": bool(entry.get("multiSelect", False)),
            "options": options,
        })
    return questions


def _clean_field(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return event_serializer.truncate(redact_text(text), MAX_QUESTION_CHARS)[0]


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _make_pre_tool_use_hook(
    session: _Session,
    block_timeout: float,
    escalate_tools: tuple[str, ...],
) -> Callable[..., Any]:
    """Intercept escalated tools and route them through the Hermes mailbox.

    A PreToolUse hook is the interception point that still fires under
    ``bypassPermissions`` — ``can_use_tool`` is auto-approved before it runs.
    Ordinary tools pass straight through, so bypass stays bypass.

    The operator's answer is delivered as ``permissionDecisionReason`` on a
    ``deny``: the tool itself would block on a human at the Mac, while the
    reason is handed back to the model as the answer it asked for.
    """
    escalated = {name.lower() for name in escalate_tools}

    async def pre_tool_use(payload: Any, tool_use_id: str | None, _context: Any) -> Any:
        tool_name = ""
        tool_input: Any = {}
        if isinstance(payload, dict):
            tool_name = str(payload.get("tool_name") or "")
            tool_input = payload.get("tool_input") or {}
            tool_use_id = tool_use_id or payload.get("tool_use_id")
        if tool_name.lower() not in escalated:
            return {}

        questions = _questions_from_input(tool_input)
        session.record("blocked", {
            "tool": tool_name,
            "tool_use_id": str(tool_use_id or ""),
            "questions": questions,
            "raw_input": _clean_field(
                _stringify_input(tool_input) if not questions else ""
            ),
            "reason": "escalated_tool",
        })
        session.store.update_run(session.run_id, status=models.STATUS_BLOCKED)

        answer = await _wait_for_answer(session, block_timeout)
        session.record("unblocked", {
            "tool": tool_name,
            "answered": answer is not None,
            "answer": _clean_field(answer) if answer else "",
        })
        session.store.update_run(session.run_id, status=models.STATUS_WORKING)

        if answer is None:
            return _deny(
                "No answer from Hermes within the block timeout. Proceed with your "
                "best judgement or stop and report what you needed."
            )
        return _deny(f"Hermes answered: {answer}")

    return pre_tool_use


def _stringify_input(tool_input: Any) -> str:
    try:
        return json.dumps(tool_input, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(tool_input)


async def _wait_for_answer(session: _Session, block_timeout: float) -> str | None:
    """Poll the mailbox until an answer arrives, a stop lands, or time runs out."""
    waited = 0.0
    while waited < block_timeout:
        if session.stop_requested or session.stop_flag_set():
            return None
        pending = session.store.take_pending_messages(session.run_id)
        if pending:
            return str(pending[0]["body"])
        await asyncio.sleep(session.poll_interval)
        waited += session.poll_interval
    return None


async def _consume_turn(session: _Session, client: Any) -> dict[str, Any] | None:
    """Persist every message of one turn; return the final result payload."""
    final: dict[str, Any] | None = None
    async for message in client.receive_response():
        session_id = event_serializer.session_id_from_message(message)
        if session_id:
            run = session.store.get_run(session.run_id)
            if run is not None and run["claude_session_id"] != session_id:
                session.store.update_run(session.run_id, claude_session_id=session_id)
        for event in event_serializer.events_from_message(message):
            session.record(event["kind"], event["payload"])
        if type(message).__name__ == "ResultMessage":
            final = {
                "is_error": bool(getattr(message, "is_error", False)),
                "text": getattr(message, "result", None) or "",
                "subtype": getattr(message, "subtype", ""),
            }
    return final


async def run_worker(
    *,
    run_id: str,
    store: Store,
    paths: RunnerPaths,
    resume: str | None = None,
    client_factory: ClientFactory = sdk_adapter.default_client_factory,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    block_timeout: float = BLOCK_TIMEOUT_SECONDS,
    escalate_tools: tuple[str, ...] | None = None,
    external_stop: threading.Event | None = None,
) -> str:
    """Drive one run to a terminal status and return that status."""
    run = store.get_run(run_id)
    if run is None:
        return models.STATUS_UNKNOWN

    workdir = Path(run["worktree"] or run["project"])
    if not workdir.is_dir():
        detail = f"work directory is missing: {workdir}"
        store.append_event(run_id, "error", {"code": "worktree_failed", "detail": detail})
        store.update_run(run_id, status=models.STATUS_FAILED, error=detail)
        return models.STATUS_FAILED

    session = _Session(run_id, store, poll_interval, external_stop)
    tools_to_escalate = (
        escalate_tools if escalate_tools is not None else sdk_adapter.ESCALATE_TOOLS
    )
    spec = sdk_adapter.SessionSpec(
        cwd=workdir,
        role=run["role"],
        resume=resume,
        claude_cli_path=paths.claude_cli_path,
        escalate_tools=tools_to_escalate,
        block_timeout_seconds=block_timeout,
    )
    options = sdk_adapter.build_options(
        spec, pre_tool_use=_make_pre_tool_use_hook(session, block_timeout, tools_to_escalate)
    )

    prompts: deque[str] = deque()
    if resume is None:
        prompts.append(sdk_adapter.build_initial_prompt(run["role"], run["prompt"]))
    else:
        queued = [m["body"] for m in store.take_pending_messages(run_id)]
        prompts.extend(queued or [sdk_adapter.build_initial_prompt(run["role"], run["prompt"])])

    final_status = models.STATUS_UNKNOWN
    final_text: str | None = None
    error_detail: str | None = None
    already_finalized = False
    client: Any = None

    try:
        if session.stop_flag_set():
            session.stop_requested = True

        store.update_run(run_id, status=models.STATUS_WORKING, error=None)
        client = client_factory(options)
        await client.connect()

        while True:
            if session.stop_requested or session.stop_flag_set():
                session.stop_requested = True
                final_status = models.STATUS_STOPPED
                break
            if not prompts:
                # Claiming the mailbox and completing the run share one
                # transaction, so a follow-up arriving right now is either
                # picked up here or refused by the RPC surface — never lost,
                # and never queued into a completed run.
                queued, mailbox_outcome = store.claim_pending_or_finalize(
                    run_id, result=final_text
                )
                if mailbox_outcome is not None:
                    final_status = mailbox_outcome
                    already_finalized = mailbox_outcome == models.STATUS_COMPLETED
                    break
                prompts.extend(m["body"] for m in queued)

            prompt = prompts.popleft()
            await client.query(prompt)
            watcher = asyncio.create_task(_watch_for_stop(session, client))
            try:
                outcome = await _consume_turn(session, client)
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher

            if session.stop_requested or session.stop_flag_set():
                session.stop_requested = True
                final_status = models.STATUS_STOPPED
                break
            if outcome is None:
                # The stream ended without a ResultMessage and nobody asked to
                # stop. That is not evidence of success, and it must not reach
                # the mailbox claim, which would finalize the run as completed.
                final_status = models.STATUS_FAILED
                error_detail = "the SDK ended the turn without a result"
                session.record("error", {"code": "no_sdk_result", "detail": error_detail})
                break
            # The durable columns are scrubbed just like the event payloads.
            final_text = redact_text(outcome["text"]) or final_text
            if outcome["is_error"]:
                final_status = models.STATUS_FAILED
                error_detail = (
                    redact_text(outcome["text"])
                    or outcome["subtype"]
                    or "SDK reported an error"
                )
                break

    except asyncio.CancelledError:
        # A cancelled worker never finished; only a requested stop is a "stop".
        final_status = (
            models.STATUS_STOPPED if session.stop_on_record() else models.STATUS_UNKNOWN
        )
        session.record("error", {"code": "worker_cancelled", "detail": final_status})
    except BaseException as exc:  # noqa: BLE001 - every failure is recorded, never swallowed
        detail = event_serializer.truncate(
            redact_text(f"{exc.__class__.__name__}: {exc}"), 2000
        )[0]
        if session.stop_on_record():
            # A controlled stop tears down the worker's whole process group, so
            # the CLI dies under the SDK and the turn raises. That exception is
            # the stop landing, not a failure: the run stopped as asked, and the
            # worktree and session are preserved exactly as on any other stop.
            session.stop_requested = True
            final_status = models.STATUS_STOPPED
            session.record("error", {"code": "worker_stopped_mid_turn", "detail": detail})
        else:
            final_status = models.STATUS_FAILED
            error_detail = detail
            session.record("error", {"code": "worker_failed", "detail": detail})
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    if final_status == models.STATUS_STOPPED:
        session.record("stopped", {"preserved_worktree": str(workdir)})
    if not already_finalized:
        # claim_pending_or_finalize is the only other writer of a completion,
        # so exactly one of the two records the terminal state.
        updates: dict[str, Any] = {"status": final_status}
        if final_status == models.STATUS_COMPLETED:
            updates["result"] = final_text
        if error_detail:
            updates["error"] = error_detail
        store.update_run(run_id, **updates)
    elif error_detail:
        store.update_run(run_id, error=error_detail)
    session.record("worker_exit", {"status": final_status})
    return final_status
