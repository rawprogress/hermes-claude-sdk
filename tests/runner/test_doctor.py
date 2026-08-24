"""The read-only preflight an installing agent runs before touching anything.

Three properties matter: it must never create or modify state, its verdict
must be machine-readable so an agent can branch on it without parsing prose,
and "Claude is ready" has to mean the CLI answers and a session exists — not
merely that a path is present.

The Claude probes are driven by fake binaries written into ``tmp_path``, so
every state a real machine can be in (signed in, logged out, hung, malformed,
too old) is reachable here without a real installation.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

from hermes_claude_runner import cli, config, db, doctor
from hermes_claude_runner.config import RunnerPaths

REPO_ROOT = Path(__file__).resolve().parents[2]

#: An address and an organisation the doctor must never copy into its report.
#: The domain is RFC 2606 reserved, so the fixture itself carries nobody.
ACCOUNT_EMAIL = "someone@example.com"
ACCOUNT_ORG_ID = "org_0123456789abcdef"
ACCOUNT_ORG_NAME = "Example Organisation"
# Assembled at runtime: a literal here would trip repository push protection.
ACCOUNT_TOKEN = "sk-" + "ant-" + "oat01" + "E" * 64

#: Nothing in here may appear anywhere in a report, in any state, whichever
#: stream the CLI printed it on. Redaction covers the address and the token;
#: an organisation is neither credential-shaped nor address-shaped, so the
#: only thing that protects it is the doctor never echoing the payload.
ACCOUNT_SECRETS = (ACCOUNT_EMAIL, ACCOUNT_ORG_ID, ACCOUNT_ORG_NAME, ACCOUNT_TOKEN)

SIGNED_IN = json.dumps({
    "loggedIn": True,
    "authMethod": "oauth",
    "apiProvider": "anthropic",
    "email": ACCOUNT_EMAIL,
    "orgId": ACCOUNT_ORG_ID,
    "orgName": ACCOUNT_ORG_NAME,
    "subscriptionType": "max",
})
LOGGED_OUT = json.dumps({
    "loggedIn": False, "authMethod": None, "apiProvider": None, "email": None,
})

CLI_VERSION = "2.1.233 (Claude Code)"


def names(report: dict) -> set[str]:
    return {check["name"] for check in report["checks"]}


def check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


# ── fake Claude binaries ───────────────────────────────────────────────────

def _quote(text: str) -> str:
    """Single-quote *text* for /bin/sh."""
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _refuse_real_installation(path: Path) -> None:
    """Never let a fixture write over somebody's real Claude Code.

    The installed CLI is a symlink into a versioned directory, so a plain
    write at that path would replace the operator's install with a stub.
    """
    if path.is_symlink():
        raise AssertionError(f"refusing to write through the symlink at {path}")
    if Path.home() in path.parents:
        raise AssertionError(f"refusing to write inside the real home: {path}")


def fake_claude(
    path: Path,
    *,
    version_stdout: str = CLI_VERSION,
    version_stderr: str = "",
    version_exit: int = 0,
    auth_stdout: str = SIGNED_IN,
    auth_stderr: str = "",
    auth_exit: int = 0,
    hang_on: str = "",
    version_raw: str = "",
    auth_raw: str = "",
) -> Path:
    """Write an executable stand-in for the Claude CLI at *path*.

    It answers exactly the two read-only commands the doctor is allowed to
    run, so a test can pin the machine state the doctor has to classify.
    """
    _refuse_real_installation(path)
    lines = ["#!/bin/sh", 'if [ "$1" = "--version" ]; then']
    if hang_on == "version":
        lines.append("  exec sleep 30")
    if version_raw:
        # A literal line, for output no printf argument can express: raw bytes,
        # or a stream that never ends.
        lines.append(f"  {version_raw}")
    else:
        lines.append(f"  printf '%s\\n' {_quote(version_stdout)}")
    if version_stderr:
        lines.append(f"  printf '%s\\n' {_quote(version_stderr)} >&2")
    lines += [f"  exit {version_exit}", "fi", 'if [ "$1" = "auth" ]; then']
    if hang_on == "auth":
        lines.append("  exec sleep 30")
    if auth_raw:
        lines.append(f"  {auth_raw}")
    elif auth_stdout:
        lines.append(f"  printf '%s\\n' {_quote(auth_stdout)}")
    if auth_stderr:
        lines.append(f"  printf '%s\\n' {_quote(auth_stderr)} >&2")
    lines += [
        f"  exit {auth_exit}", "fi",
        "printf '%s\\n' 'the doctor ran a command this fake does not know' >&2",
        "exit 64",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


@pytest.fixture()
def signed_in_cli(paths: RunnerPaths) -> Path:
    """A Claude CLI that is executable, current and signed in."""
    return fake_claude(paths.claude_cli_path)


def test_the_fixture_paths_are_disposable(paths: RunnerPaths) -> None:
    """Every path a test writes to is built from an explicit throwaway home."""
    assert paths.home != Path.home()
    assert Path.home() not in paths.claude_cli_path.parents
    assert Path.home() not in paths.wrapper_path.parents


def test_a_fake_refuses_to_touch_the_real_installation(tmp_path: Path) -> None:
    """The guard has to fire before the write, not after."""
    real = Path.home() / ".local" / "bin" / "claude"
    with pytest.raises(AssertionError):
        fake_claude(real)

    link = tmp_path / "link"
    link.symlink_to(tmp_path / "versions" / "2.1.233")
    with pytest.raises(AssertionError):
        fake_claude(link)
    assert link.is_symlink(), "the symlink itself must survive"


@pytest.fixture(autouse=True)
def short_probe_timeout(monkeypatch) -> None:
    """No test may wait on a real timeout; the fakes answer immediately."""
    monkeypatch.setattr(doctor, "CLAUDE_PROBE_TIMEOUT_SECONDS", 10.0)


@pytest.fixture(autouse=True)
def refuse_the_real_binary(monkeypatch) -> None:
    """No test in this module may execute the operator's own Claude Code.

    Every probe goes through one Popen, so guarding it covers the whole file
    — including a test that forgets its fake and would silently fall through
    to whatever is on PATH.
    """
    real = shutil.which("claude")
    home = Path.home()
    spawn = doctor._spawn  # noqa: SLF001

    def guarded(argv):  # type: ignore[no-untyped-def]
        target = Path(str(argv[0]))
        assert real is None or str(target) != real, f"a test ran the real CLI: {target}"
        assert home not in target.parents, f"a test ran a binary under {home}: {target}"
        return spawn(argv)

    monkeypatch.setattr(doctor, "_spawn", guarded)


# ── read-only ──────────────────────────────────────────────────────────────

def test_doctor_creates_nothing(paths: RunnerPaths, signed_in_cli: Path) -> None:
    """A preflight that installs half of the thing it inspects is a trap."""
    doctor.diagnose(paths)

    assert not paths.db_path.exists()
    assert not paths.data_dir.exists()
    assert not paths.log_dir.exists()
    assert not paths.worktrees_root.exists()


def test_doctor_leaves_an_existing_database_untouched(
    paths: RunnerPaths, signed_in_cli: Path
) -> None:
    from hermes_claude_runner.store import Store

    with Store.open(paths.db_path) as store:
        store.create_run(run_id="rdoc00001", project="/p", role="implementer", prompt="p")
    before = paths.db_path.read_bytes()

    report = doctor.diagnose(paths)

    assert paths.db_path.read_bytes() == before
    assert check(report, "database")["ok"] is True
    assert check(report, "database")["schema_version"] == db.SCHEMA_VERSION


def test_the_doctor_never_runs_a_command_that_could_mutate_auth(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    """Signing in, signing out and updating are a human's decisions."""
    recorded: list[tuple[str, ...]] = []
    spawn = doctor._spawn  # noqa: SLF001

    def spy(argv):  # type: ignore[no-untyped-def]
        recorded.append(tuple(str(part) for part in argv))
        return spawn(argv)

    monkeypatch.setattr(doctor, "_spawn", spy)
    doctor.diagnose(paths, probe_daemon=False)

    assert recorded, "the doctor never probed the CLI at all"
    for argv in recorded:
        assert argv[1:] in doctor.READ_ONLY_CLAUDE_ARGV, argv
        for forbidden in ("login", "logout", "setup-token", "update", "install"):
            assert forbidden not in argv, argv


def test_the_probe_guard_refuses_anything_that_is_not_read_only() -> None:
    """The allowlist is enforced at the call site, not just documented."""
    for argv in (("auth", "login"), ("auth", "logout"), ("update",), ("setup-token",)):
        with pytest.raises(ValueError):
            doctor._run_claude(Path("/bin/echo"), argv)  # noqa: SLF001


def test_the_read_only_allowlist_is_a_stable_contract() -> None:
    assert doctor.READ_ONLY_CLAUDE_ARGV == frozenset({
        ("--version",), ("auth", "status", "--json"),
    })


# ── verdict ────────────────────────────────────────────────────────────────

def test_report_is_machine_readable(paths: RunnerPaths, signed_in_cli: Path) -> None:
    report = doctor.diagnose(paths)

    json.dumps(report)  # raises if anything is not serializable
    assert isinstance(report["ok"], bool)
    assert report["label"] == paths.launch_agent_label
    assert {"platform", "python", "git", "claude_cli", "claude_auth", "agent_sdk",
            "projects_root", "database", "daemon", "launch_agent", "wrapper"} <= names(report)
    for entry in report["checks"]:
        assert set(entry) >= {"name", "ok", "required", "detail"}


def test_every_reported_state_comes_from_a_declared_set(
    paths: RunnerPaths, signed_in_cli: Path
) -> None:
    """An agent branching on `state` needs the values enumerated up front."""
    declared = {
        "claude_cli": doctor.CLAUDE_CLI_STATES,
        "claude_auth": doctor.CLAUDE_AUTH_STATES,
        "agent_sdk": doctor.AGENT_SDK_STATES,
    }
    report = doctor.diagnose(paths, probe_daemon=False)
    for name, states in declared.items():
        assert check(report, name)["state"] in states


def test_the_state_vocabulary_is_a_stable_contract() -> None:
    assert doctor.CLAUDE_CLI_STATES == (
        "ready", "missing", "not_executable", "timeout", "failed", "unrecognized")
    assert doctor.CLAUDE_AUTH_STATES == (
        "signed_in", "logged_out", "timeout", "incompatible", "unverifiable")
    assert doctor.AGENT_SDK_STATES == ("ready", "missing", "too_old", "unrecognized")


def test_a_missing_install_is_reported_not_hidden(
    paths: RunnerPaths, signed_in_cli: Path
) -> None:
    report = doctor.diagnose(paths)

    assert report["ok"] is False
    assert check(report, "daemon")["ok"] is False
    assert check(report, "launch_agent")["ok"] is False
    assert "launch_agent" in report["problems"]


def test_optional_checks_do_not_decide_the_verdict(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    """Only required checks may fail the preflight."""
    monkeypatch.setattr(doctor, "_probe", lambda *a, **k: True)
    report = doctor.diagnose(paths)
    failing = [c["name"] for c in report["checks"] if not c["ok"]]
    assert report["ok"] == all(not check(report, name)["required"] for name in failing)


def test_a_reachable_daemon_flips_the_verdict(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
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


def test_the_daemon_probe_can_be_skipped(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    """An agent inspecting a machine with no daemon should not wait on a socket."""
    def explode(*_args, **_kwargs):
        raise AssertionError("the socket must not be contacted")

    monkeypatch.setattr(doctor.client, "send_request", explode)
    report = doctor.diagnose(paths, probe_daemon=False)
    assert check(report, "daemon")["ok"] is False
    assert "skipped" in check(report, "daemon")["detail"]


def test_every_problem_carries_a_next_step(
    paths: RunnerPaths, signed_in_cli: Path
) -> None:
    report = doctor.diagnose(paths)
    for name in report["problems"]:
        assert check(report, name)["fix"], f"{name} reports no way forward"


@pytest.mark.parametrize("broken", [
    {"auth_stdout": LOGGED_OUT},
    {"auth_stdout": "", "auth_stderr": "error: unknown command auth", "auth_exit": 1},
    {"auth_stdout": "not json"},
    {"version_stdout": "", "version_stderr": "boom", "version_exit": 1},
    {"version_stdout": "claude, the good one"},
])
def test_every_claude_failure_state_carries_a_next_step(
    paths: RunnerPaths, broken: dict
) -> None:
    """Each way Claude can be unready has to name the command that fixes it."""
    fake_claude(paths.claude_cli_path, **broken)
    report = doctor.diagnose(paths, probe_daemon=False)

    failed = [name for name in ("claude_cli", "claude_auth") if not check(report, name)["ok"]]
    assert failed, f"{broken} was not noticed at all"
    for name in failed:
        entry = check(report, name)
        assert entry["fix"].strip(), f"{name} in state {entry['state']} offers no fix"


# ── the Claude CLI has to answer, not merely exist ─────────────────────────

def test_an_executable_cli_reports_the_version_it_answers_with(
    paths: RunnerPaths, signed_in_cli: Path
) -> None:
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_cli")

    assert entry["ok"] is True
    assert entry["state"] == "ready"
    assert entry["version"] == "2.1.233"
    assert "2.1.233" in entry["detail"]
    assert entry["path"] == str(paths.claude_cli_path)


def test_a_present_but_unexecutable_cli_is_not_ready(paths: RunnerPaths) -> None:
    """The old check passed on any file at the configured path."""
    paths.claude_cli_path.parent.mkdir(parents=True, exist_ok=True)
    paths.claude_cli_path.write_text("#!/bin/sh\n")
    paths.claude_cli_path.chmod(0o644)

    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_cli")

    assert entry["ok"] is False
    assert entry["state"] == "not_executable"
    assert "chmod +x" in entry["fix"]
    assert "claude_cli" in doctor.diagnose(paths, probe_daemon=False)["problems"]


def test_a_missing_cli_says_where_it_looked(paths: RunnerPaths, monkeypatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    report = doctor.diagnose(paths, probe_daemon=False)
    entry = check(report, "claude_cli")

    assert entry["state"] == "missing"
    assert str(paths.claude_cli_path) in entry["detail"]
    assert entry["version"] is None
    assert entry["fix"]
    # Nothing can be asked of a binary that is not there, and saying so beats
    # a second copy of the same failure.
    unverifiable = check(report, "claude_auth")
    assert unverifiable["state"] == "unverifiable"
    assert unverifiable["fix"]


def test_a_cli_that_fails_to_answer_is_reported_with_its_error(
    paths: RunnerPaths
) -> None:
    fake_claude(paths.claude_cli_path, version_stdout="", version_stderr="node not found",
                version_exit=1)
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_cli")

    assert entry["ok"] is False
    assert entry["state"] == "failed"
    assert "node not found" in entry["detail"]


def test_a_cli_whose_version_is_unreadable_is_not_trusted(paths: RunnerPaths) -> None:
    fake_claude(paths.claude_cli_path, version_stdout="claude, the good one")
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_cli")

    assert entry["ok"] is False
    assert entry["state"] == "unrecognized"
    assert entry["version"] is None


def test_a_hung_cli_is_bounded_by_the_timeout(paths: RunnerPaths, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "CLAUDE_PROBE_TIMEOUT_SECONDS", 0.5)
    fake_claude(paths.claude_cli_path, hang_on="version")

    started = time.monotonic()
    report = doctor.diagnose(paths, probe_daemon=False)
    elapsed = time.monotonic() - started

    entry = check(report, "claude_cli")
    assert entry["state"] == "timeout"
    assert elapsed < 10, f"the doctor waited {elapsed:.1f}s on a hung CLI"
    # A CLI that cannot answer must not be asked a second question.
    assert check(report, "claude_auth")["state"] == "unverifiable"


# ── the session has to exist ───────────────────────────────────────────────

def test_a_signed_in_session_passes_and_names_the_mode(
    paths: RunnerPaths, signed_in_cli: Path
) -> None:
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")

    assert entry["ok"] is True
    assert entry["required"] is True
    assert entry["state"] == "signed_in"
    assert "oauth" in entry["detail"] and "anthropic" in entry["detail"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_logged_out_cli_fails_whatever_it_exits_with(
    paths: RunnerPaths, exit_code: int
) -> None:
    """Exit codes vary between Claude Code releases; the payload does not."""
    fake_claude(paths.claude_cli_path, auth_stdout=LOGGED_OUT, auth_exit=exit_code)
    report = doctor.diagnose(paths, probe_daemon=False)
    entry = check(report, "claude_auth")

    assert entry["ok"] is False
    assert entry["state"] == "logged_out"
    assert "claude auth login" in entry["fix"]
    assert "claude_auth" in report["problems"]
    assert check(report, "claude_cli")["ok"] is True, "the binary itself was fine"


def test_a_logged_out_machine_is_not_installable(paths: RunnerPaths) -> None:
    """Installing cannot sign anybody in, so it must not be offered as the fix."""
    fake_claude(paths.claude_cli_path, auth_stdout=LOGGED_OUT)
    report = doctor.diagnose(paths, probe_daemon=False)

    assert report["installable"] is False
    assert not set(report["problems"]) <= doctor.INSTALLABLE_PROBLEMS


def test_a_cli_too_old_for_the_auth_probe_is_reported_as_incompatible(
    paths: RunnerPaths
) -> None:
    fake_claude(paths.claude_cli_path, auth_stdout="",
                auth_stderr="error: unknown command auth", auth_exit=1)
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")

    assert entry["ok"] is False
    assert entry["state"] == "incompatible"
    assert "claude update" in entry["fix"]
    # Enough to act on: which Claude Code, and that it refused the command.
    assert "2.1.233" in entry["detail"] and "exit 1" in entry["detail"]
    # The CLI's own words are not quoted back. On this path they are the auth
    # payload as often as they are an error message.
    assert "unknown command auth" not in entry["detail"]


def test_auth_output_that_is_not_json_is_unverifiable_not_a_verdict(
    paths: RunnerPaths
) -> None:
    """Guessing "logged out" from unreadable output would send a human to a
    login they do not need."""
    fake_claude(paths.claude_cli_path, auth_stdout="Signed in as somebody")
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")

    assert entry["ok"] is False
    assert entry["state"] == "unverifiable"
    assert "claude auth status" in entry["fix"]


def test_json_without_the_answer_is_unverifiable(paths: RunnerPaths) -> None:
    fake_claude(paths.claude_cli_path, auth_stdout=json.dumps({"authMethod": "oauth"}))
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")

    assert entry["state"] == "unverifiable"


def test_a_hung_auth_probe_is_bounded(paths: RunnerPaths, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "CLAUDE_PROBE_TIMEOUT_SECONDS", 0.5)
    fake_claude(paths.claude_cli_path, hang_on="auth")

    started = time.monotonic()
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")
    elapsed = time.monotonic() - started

    assert entry["state"] == "timeout"
    assert "0" in entry["detail"], "the bound has to be stated"
    assert elapsed < 10, f"the doctor waited {elapsed:.1f}s on a hung auth probe"


# ── nothing about the account leaves the CLI ───────────────────────────────

def test_the_report_never_carries_the_accounts_identity(
    paths: RunnerPaths, signed_in_cli: Path
) -> None:
    """The report travels to Hermes and sits in a log; the address stays here."""
    blob = json.dumps(doctor.diagnose(paths, probe_daemon=False))

    for private in ACCOUNT_SECRETS:
        assert private not in blob, f"{private} reached the report"


def test_unreadable_auth_output_is_never_echoed_back(paths: RunnerPaths) -> None:
    """Malformed stdout is classified, not quoted: it can carry an address."""
    fake_claude(
        paths.claude_cli_path,
        auth_stdout=f'oops "email": "{ACCOUNT_EMAIL}" "orgName": "{ACCOUNT_ORG_NAME}"',
    )
    report = doctor.diagnose(paths, probe_daemon=False)

    assert check(report, "claude_auth")["state"] == "unverifiable"
    blob = json.dumps(report)
    assert ACCOUNT_EMAIL not in blob and ACCOUNT_ORG_NAME not in blob


def test_an_address_on_stderr_is_redacted(paths: RunnerPaths) -> None:
    """The version probe's stderr is quoted, so it has to be scrubbed."""
    fake_claude(paths.claude_cli_path, version_stdout="", version_exit=1,
                version_stderr=f"install is broken (account {ACCOUNT_EMAIL})")
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_cli")

    assert entry["state"] == "failed"
    assert "install is broken" in entry["detail"]
    assert ACCOUNT_EMAIL not in entry["detail"]
    assert "[redacted:address]" in entry["detail"]


#: Every way the auth probe can answer with the account's identity in it, on
#: either stream, under either exit code.
HOSTILE_AUTH_ANSWERS = {
    "json-without-a-verdict-on-stderrless-failure": {
        "auth_stdout": json.dumps({
            "email": ACCOUNT_EMAIL, "orgId": ACCOUNT_ORG_ID,
            "orgName": ACCOUNT_ORG_NAME, "authMethod": ACCOUNT_TOKEN,
        }),
        "auth_stderr": "", "auth_exit": 1,
    },
    "identity-on-stderr": {
        "auth_stdout": "",
        "auth_stderr": (f"error: unknown command auth; org {ACCOUNT_ORG_NAME} "
                        f"{ACCOUNT_ORG_ID} {ACCOUNT_EMAIL} {ACCOUNT_TOKEN}"),
        "auth_exit": 1,
    },
    "identity-on-both-streams": {
        "auth_stdout": f"not json {ACCOUNT_ORG_NAME} {ACCOUNT_ORG_ID}",
        "auth_stderr": f"also not json {ACCOUNT_EMAIL} {ACCOUNT_TOKEN}",
        "auth_exit": 1,
    },
    "identity-in-a-clean-exit-that-is-not-json": {
        "auth_stdout": json.dumps({"orgName": ACCOUNT_ORG_NAME, "orgId": ACCOUNT_ORG_ID}),
        "auth_stderr": "", "auth_exit": 0,
    },
    "identity-alongside-a-real-verdict": {
        "auth_stdout": json.dumps({
            "loggedIn": True, "authMethod": "oauth", "email": ACCOUNT_EMAIL,
            "orgId": ACCOUNT_ORG_ID, "orgName": ACCOUNT_ORG_NAME,
        }),
        "auth_stderr": f"warning: {ACCOUNT_ORG_NAME}", "auth_exit": 1,
    },
}


@pytest.mark.parametrize("answer", list(HOSTILE_AUTH_ANSWERS),
                         ids=list(HOSTILE_AUTH_ANSWERS))
def test_no_auth_answer_puts_the_account_in_the_report(
    paths: RunnerPaths, answer: str
) -> None:
    """The payload is classified, never echoed — on every path, not most.

    An exit code with nothing on stderr used to fall back to quoting stdout,
    which is the auth payload itself: the address and the token were redacted
    on the way out, but the organisation has no shape to match and survived.
    """
    fake_claude(paths.claude_cli_path, **HOSTILE_AUTH_ANSWERS[answer])
    report = doctor.diagnose(paths, probe_daemon=False)
    blob = json.dumps(report)

    for private in ACCOUNT_SECRETS:
        assert private not in blob, f"{private} reached the report"
    entry = check(report, "claude_auth")
    assert entry["state"] in doctor.CLAUDE_AUTH_STATES
    assert entry["ok"] or entry["fix"], "a failure with no way forward"
    assert len(entry["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS


def test_a_verdict_still_arrives_when_the_payload_is_hostile(
    paths: RunnerPaths
) -> None:
    """Refusing to quote must not turn into refusing to answer."""
    fake_claude(paths.claude_cli_path,
                **HOSTILE_AUTH_ANSWERS["identity-alongside-a-real-verdict"])
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")

    assert entry["ok"] is True
    assert entry["state"] == "signed_in"
    assert "oauth" in entry["detail"]


def test_a_hostile_signed_in_payload_cannot_smuggle_anything_through(
    paths: RunnerPaths
) -> None:
    """The success path is CLI output too, and gets the same treatment.

    Reporting the mode meant interpolating three strings a compromised or
    simply broken CLI controls; unbounded, they were a way past both the
    redaction and the size cap that every failure path already had.
    """
    # Assembled at runtime: a literal here would trip repository push protection.
    token = "sk-" + "ant-" + "oat01" + "C" * 64
    fake_claude(paths.claude_cli_path, auth_stdout=json.dumps({
        "loggedIn": True,
        "authMethod": token,
        "apiProvider": "P" * 5000,
        "subscriptionType": f"max {ACCOUNT_EMAIL}",
    }))
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")

    assert entry["ok"] is True and entry["state"] == "signed_in"
    assert token not in entry["detail"]
    assert "[redacted:anthropic_key]" in entry["detail"]
    assert ACCOUNT_EMAIL not in entry["detail"]
    assert "P" * (doctor.MAX_AUTH_FIELD_CHARS + 1) not in entry["detail"]
    assert len(entry["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS


def test_no_reported_auth_field_can_grow_the_report(paths: RunnerPaths) -> None:
    """A property of the field list, so a fourth field inherits the cap."""
    fake_claude(paths.claude_cli_path, auth_stdout=json.dumps({
        "loggedIn": True,
        **{key: "y" * 9000 for key, _ in doctor.REPORTED_AUTH_FIELDS},
    }))
    report = doctor.diagnose(paths, probe_daemon=False)
    entry = check(report, "claude_auth")

    assert entry["state"] == "signed_in"
    assert len(entry["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS
    assert len(json.dumps(report)) < 8000, "one field must not dominate the report"


def test_an_absurd_version_cannot_grow_the_report(paths: RunnerPaths) -> None:
    """`claude --version` output is CLI-controlled on the success path too."""
    fake_claude(paths.claude_cli_path, version_stdout="9" * 5000 + ".8.7")
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_cli")

    assert len(entry["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS
    assert len(entry["version"] or "") <= doctor.MAX_AUTH_FIELD_CHARS + 3


def test_an_absurd_sdk_version_cannot_grow_the_report(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    monkeypatch.setattr(doctor, "_sdk_version", lambda: "9" * 5000 + ".8.7")
    entry = check(doctor.diagnose(paths, probe_daemon=False), "agent_sdk")

    assert len(entry["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS


def test_probe_output_is_redacted_before_it_is_bounded(paths: RunnerPaths) -> None:
    """Truncating first can split a token and leak the half that is left."""
    # Assembled at runtime: a literal here would trip repository push protection.
    token = "sk-" + "ant-" + "oat01" + "A" * 64
    fake_claude(paths.claude_cli_path, version_stdout="", version_exit=1,
                version_stderr=f"failed with {token} " + "noise " * 400)
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_cli")

    assert token not in entry["detail"]
    assert "[redacted:anthropic_key]" in entry["detail"]
    assert len(entry["detail"]) < doctor.MAX_PROBE_OUTPUT_CHARS + 300


def test_bounded_output_stays_bounded() -> None:
    assert len(doctor._bounded("x" * 100_000)) <= doctor.MAX_PROBE_OUTPUT_CHARS + 10  # noqa: SLF001


# ── output the doctor did not ask for ──────────────────────────────────────

def test_output_that_is_not_utf8_does_not_crash_the_preflight(
    paths: RunnerPaths
) -> None:
    """One stray byte used to take `doctor --json` down with a traceback.

    A preflight exists to describe a broken machine. Being unable to describe
    one because it is broken in an unusual way is the one failure mode it
    cannot have.
    """
    fake_claude(paths.claude_cli_path, version_raw=r"printf '\377\376 broken'",
                version_exit=1)
    report = doctor.diagnose(paths, probe_daemon=False)

    json.dumps(report)  # a report at all is the point
    entry = check(report, "claude_cli")
    assert entry["ok"] is False
    assert entry["state"] in doctor.CLAUDE_CLI_STATES


def test_auth_output_that_is_not_utf8_still_yields_a_verdict(
    paths: RunnerPaths
) -> None:
    fake_claude(paths.claude_cli_path, auth_raw=r"printf '\377\376'", auth_exit=1)
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")

    assert entry["ok"] is False
    assert entry["state"] in doctor.CLAUDE_AUTH_STATES


def test_json_nested_past_the_recursion_limit_is_contained(
    paths: RunnerPaths
) -> None:
    """The capture cap does not save the parser: 64 KiB of `[` is depth 65536."""
    fake_claude(paths.claude_cli_path, auth_stdout="[" * 40_000 + "]" * 40_000)
    report = doctor.diagnose(paths, probe_daemon=False)

    json.dumps(report)
    entry = check(report, "claude_auth")
    assert entry["ok"] is False
    assert entry["state"] == "unverifiable"


def test_the_parser_itself_survives_what_json_cannot_parse() -> None:
    assert doctor._parse_auth_status("[" * 100_000) is None  # noqa: SLF001
    assert doctor._parse_auth_status("") is None  # noqa: SLF001
    assert doctor._parse_auth_status("null") is None  # noqa: SLF001


def test_a_flood_of_output_is_capped_not_buffered(paths: RunnerPaths) -> None:
    """8 MB used to arrive in memory in full before anything bounded it."""
    fake_claude(
        paths.claude_cli_path,
        version_raw="/usr/bin/yes AAAAAAAAAAAAAAAA | /usr/bin/head -c 8000000",
    )
    started = time.monotonic()
    probe = doctor._run_claude(paths.claude_cli_path, doctor.CLAUDE_VERSION_ARGV)  # noqa: SLF001
    elapsed = time.monotonic() - started

    # The cap is on the captured bytes; decoding with errors="replace" yields
    # at most one character per byte, so the decoded length is bounded too.
    assert len(probe.stdout.encode()) <= doctor.MAX_CAPTURE_BYTES, "capture is unbounded"
    assert probe.answered, "draining must let the child finish, not wedge it"
    assert probe.code == 0
    assert elapsed < 10, f"capping took {elapsed:.1f}s"


def test_a_stream_that_never_ends_is_bounded_by_memory_and_by_time(
    paths: RunnerPaths, monkeypatch
) -> None:
    """Draining keeps the child unblocked; the timeout is what ends this one."""
    monkeypatch.setattr(doctor, "CLAUDE_PROBE_TIMEOUT_SECONDS", 0.5)
    fake_claude(paths.claude_cli_path, version_raw="exec /usr/bin/yes AAAAAAAAAAAAAAAA")

    started = time.monotonic()
    probe = doctor._run_claude(paths.claude_cli_path, doctor.CLAUDE_VERSION_ARGV)  # noqa: SLF001
    elapsed = time.monotonic() - started

    assert probe.timed_out
    assert len(probe.stdout.encode()) <= doctor.MAX_CAPTURE_BYTES
    assert elapsed < 10, f"an endless stream held the preflight for {elapsed:.1f}s"


def test_a_flooding_cli_still_produces_a_bounded_report(paths: RunnerPaths) -> None:
    fake_claude(
        paths.claude_cli_path,
        version_raw="/usr/bin/yes AAAAAAAAAAAAAAAA | /usr/bin/head -c 8000000",
    )
    report = doctor.diagnose(paths, probe_daemon=False)

    assert len(json.dumps(report)) < 20_000, "a flood reached the report"
    for name in ("claude_cli", "claude_auth"):
        assert len(check(report, name)["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS


#: A capture made only of credential-name characters. Scrubbing this used to
#: cost minutes, after the child had already exited — so neither the probe
#: timeout nor the capture cap bounded it.
DENSE_OUTPUT = "a." * 32_768

#: Two orders of magnitude above what bounding a dense capture costs once
#: nothing in the path walks a run of name characters twice.
SCRUB_BUDGET_SECONDS = 3.0


def test_bounding_a_dense_capture_is_not_quadratic() -> None:
    """`_bounded` runs two scrubbers over the whole retained capture.

    The address pattern had the same unanchored-run shape the assignment
    pattern did, and cost 5.5s here on its own.
    """
    started = time.monotonic()
    out = doctor._bounded(DENSE_OUTPUT)  # noqa: SLF001
    elapsed = time.monotonic() - started

    assert elapsed < SCRUB_BUDGET_SECONDS, f"bounding took {elapsed:.1f}s"
    assert len(out) <= doctor.MAX_PROBE_OUTPUT_CHARS


@pytest.mark.parametrize(("text", "expected"), [
    ("mail someone@example.com now", "mail [redacted:address] now"),
    ("someone@example.com", "[redacted:address]"),
    # Assembled at runtime: a whole address at a domain the publication gate
    # does not recognise as reserved would ship as somebody's inbox.
    ("a.b+c@sub." + "example.co.uk.", "[redacted:address]."),
    ("two a@example.com and b@example.org", "two [redacted:address] and [redacted:address]"),
    ("@example.com", "@example.com"),
    ("a@b", "a@b"),
    ("a@123.456", "a@123.456"),
    ("no address here", "no address here"),
])
def test_addresses_are_recognised_the_same_way_they_were(
    text: str, expected: str
) -> None:
    """Anchoring the scan on the @ must not change what counts as an address."""
    assert doctor._bounded(text) == expected  # noqa: SLF001


def test_dense_probe_output_does_not_stall_the_preflight(paths: RunnerPaths) -> None:
    """A CLI that prints 64 KiB of dots is a slow report, not a hung one."""
    fake_claude(paths.claude_cli_path, version_stdout="", version_exit=1,
                version_stderr=DENSE_OUTPUT)

    started = time.monotonic()
    report = doctor.diagnose(paths, probe_daemon=False)
    elapsed = time.monotonic() - started

    assert elapsed < SCRUB_BUDGET_SECONDS, f"scrubbing took {elapsed:.1f}s"
    entry = check(report, "claude_cli")
    assert entry["state"] == "failed"
    assert len(entry["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS


def test_dense_signed_in_fields_do_not_stall_the_preflight(
    paths: RunnerPaths
) -> None:
    """The success path scrubs CLI-supplied text too, and on the same code.

    Each field stays well under the capture cap so the payload still parses;
    the point is the scrubbing cost, not the truncation.
    """
    dense = "a." * 7_500
    fake_claude(paths.claude_cli_path, auth_stdout=json.dumps({
        "loggedIn": True, "authMethod": dense, "apiProvider": dense,
        "subscriptionType": dense,
    }))

    started = time.monotonic()
    entry = check(doctor.diagnose(paths, probe_daemon=False), "claude_auth")
    elapsed = time.monotonic() - started

    assert elapsed < SCRUB_BUDGET_SECONDS, f"scrubbing took {elapsed:.1f}s"
    assert entry["state"] == "signed_in", entry["detail"]
    assert len(entry["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS


def test_a_binary_that_cannot_be_spawned_is_reported_not_raised(
    paths: RunnerPaths, monkeypatch
) -> None:
    """Every failure to run becomes a verdict, never an exception."""
    fake_claude(paths.claude_cli_path)

    def refuse(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(doctor, "_spawn", refuse)
    report = doctor.diagnose(paths, probe_daemon=False)

    json.dumps(report)
    assert check(report, "claude_cli")["ok"] is False


def test_an_unexpected_spawn_failure_still_yields_a_report(
    paths: RunnerPaths, monkeypatch
) -> None:
    """Not every way Popen fails is an OSError, and none may escape."""
    fake_claude(paths.claude_cli_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("something no version of this ever saw")

    monkeypatch.setattr(doctor, "_spawn", explode)
    report = doctor.diagnose(paths, probe_daemon=False)

    json.dumps(report)
    assert check(report, "claude_cli")["state"] == "not_executable"


@pytest.mark.parametrize("broken", [
    {"version_stdout": "", "version_stderr": "x" * 9000, "version_exit": 1},
    {"version_stdout": "y" * 9000},
    {"auth_stdout": "z" * 9000},
    {"auth_stdout": "", "auth_stderr": "w" * 9000, "auth_exit": 1},
])
def test_no_check_detail_ever_exceeds_the_cap(
    paths: RunnerPaths, broken: dict
) -> None:
    """One bound, uniformly, so a reader never has to ask which path they are on."""
    fake_claude(paths.claude_cli_path, **broken)
    report = doctor.diagnose(paths, probe_daemon=False)

    for name in ("claude_cli", "claude_auth", "agent_sdk"):
        entry = check(report, name)
        assert len(entry["detail"]) <= doctor.MAX_PROBE_OUTPUT_CHARS, name


# ── the Agent SDK the runner actually imports ──────────────────────────────

def test_the_installed_agent_sdk_is_reported(
    paths: RunnerPaths, signed_in_cli: Path
) -> None:
    entry = check(doctor.diagnose(paths, probe_daemon=False), "agent_sdk")

    assert entry["ok"] is True
    assert entry["state"] == "ready"
    assert entry["required"] is True
    assert entry["version"] and entry["version"] in entry["detail"]


def test_a_missing_agent_sdk_is_a_prerequisite_failure(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    monkeypatch.setattr(doctor, "_sdk_version", lambda: None)
    report = doctor.diagnose(paths, probe_daemon=False)
    entry = check(report, "agent_sdk")

    assert entry["ok"] is False
    assert entry["state"] == "missing"
    assert "uv sync" in entry["fix"]
    assert "agent_sdk" in report["problems"]
    assert report["installable"] is False


def below(floor: tuple[int, ...]) -> str:
    """The greatest version string strictly below *floor*."""
    parts = list(floor)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] > 0:
            parts[index] -= 1
            return ".".join(str(part) for part in parts)
    raise AssertionError("nothing is below 0.0.0")


def test_an_agent_sdk_below_the_floor_is_incompatible(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    old = below(doctor.MINIMUM_AGENT_SDK)
    monkeypatch.setattr(doctor, "_sdk_version", lambda: old)
    entry = check(doctor.diagnose(paths, probe_daemon=False), "agent_sdk")

    assert entry["ok"] is False
    assert entry["state"] == "too_old"
    assert old in entry["detail"]
    assert "uv sync" in entry["fix"]


def test_an_agent_sdk_at_the_floor_is_accepted(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    floor = ".".join(str(part) for part in doctor.MINIMUM_AGENT_SDK)
    monkeypatch.setattr(doctor, "_sdk_version", lambda: floor)
    assert check(doctor.diagnose(paths, probe_daemon=False), "agent_sdk")["ok"] is True


def test_an_unreadable_agent_sdk_version_is_not_assumed_good(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    monkeypatch.setattr(doctor, "_sdk_version", lambda: "unreleased")
    entry = check(doctor.diagnose(paths, probe_daemon=False), "agent_sdk")

    assert entry["ok"] is False
    assert entry["state"] == "unrecognized"


def test_the_agent_sdk_floor_matches_the_declared_dependency() -> None:
    """One declared minimum. A doctor that invents its own would lie to an agent."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r"claude-agent-sdk>=([\d.]+)", pyproject)
    assert declared, "pyproject no longer pins claude-agent-sdk"
    assert doctor.MINIMUM_AGENT_SDK == tuple(
        int(part) for part in declared.group(1).split(".")
    )


def test_the_sdk_check_survives_a_broken_import(monkeypatch) -> None:
    """A venv this broken is exactly what the check exists to name."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "claude_agent_sdk":
            raise ImportError("no module named claude_agent_sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    monkeypatch.delitem(sys.modules, "claude_agent_sdk", raising=False)
    assert doctor._sdk_version() is None  # noqa: SLF001


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
        config.ENV_CLAUDE_CLI: str(fake_claude(tmp_path / "bin" / "claude")),
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "hermes_claude_runner", "doctor", "--json"],
        capture_output=True, text=True, env=env, timeout=120,
    )

    payload = json.loads(completed.stdout)  # raises if progress text leaked
    assert payload["ok"] is False
    assert completed.returncode == 1
    # This one runs the doctor out of process, where the module-wide guard
    # cannot reach. The configured path exists, so it wins before PATH is
    # consulted: the child probed the fake, not the caller's Claude Code.
    probed = next(c for c in payload["checks"] if c["name"] == "claude_cli")["path"]
    assert probed == str(tmp_path / "bin" / "claude")


def test_doctor_subcommand_is_human_readable_without_json(
    paths, signed_in_cli, capsys, monkeypatch
) -> None:
    from tests.runner.test_cli import env_for, run_cli

    result = run_cli(["doctor"], "", capsys, env_for(paths), monkeypatch)
    assert "daemon" in result["out"]
    assert result["code"] == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(result["out"])


def test_the_rendered_report_names_the_claude_state(paths: RunnerPaths) -> None:
    fake_claude(paths.claude_cli_path, auth_stdout=LOGGED_OUT)
    rendered = doctor.render(doctor.diagnose(paths, probe_daemon=False))

    assert "claude_auth" in rendered
    assert "claude auth login" in rendered
    assert ACCOUNT_EMAIL not in rendered


def test_doctor_is_registered_in_the_parser() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["doctor", "--json"])
    assert args.command == "doctor" and args.json is True


# ── the criteria the agent contract branches on ────────────────────────────

def test_uv_is_required_because_every_documented_command_needs_it(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    """The installer runs `uv sync` unconditionally; a warning would mislead."""
    report = doctor.diagnose(paths, probe_daemon=False)
    assert check(report, "uv")["required"] is True

    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    without_uv = doctor.diagnose(paths, probe_daemon=False)
    assert "uv" in without_uv["problems"]
    assert "uv" in check(without_uv, "uv")["fix"]


def test_node_is_reported_but_never_blocks(
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
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
    paths: RunnerPaths, signed_in_cli: Path, monkeypatch
) -> None:
    """INSTALL_FOR_AGENTS phase 1 must be satisfiable before anything is installed.

    The old contract said `report.ok == true`, which is unreachable on a first
    install: the service is exactly what is not there yet.
    """
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
