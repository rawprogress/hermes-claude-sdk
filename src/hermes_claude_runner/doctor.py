"""Read-only preflight.

Answers two questions — "is this machine ready?" and, when it is not, "is
the only thing missing the service this installer is about to create?" —
without creating, migrating or starting anything.

Both answers are single booleans, because an installing agent has to branch
on them: ``report["installable"]`` before installing, ``report["ok"]``
afterwards. A human reads the same report as a table.

Readiness for Claude means more than a path: the resolved binary has to be
executable, name its version, and report a session. That last answer can
only come from the CLI itself, so the doctor runs two of its commands — and
only two, both read-only, both bounded by a timeout, with their output
redacted before anything reaches the report. Nothing here signs in, signs
out, opens a browser or reads a credential file.
"""

from __future__ import annotations

import json
import os
import platform
import re
import selectors
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__, client, db, redaction
from .config import RunnerPaths

# Kept in step with pyproject's requires-python by
# tests/test_portability.py::test_the_doctor_knows_the_real_python_floor.
MINIMUM_PYTHON = (3, 12)

# Kept in step with pyproject's dependency pin by
# tests/runner/test_doctor.py::test_the_agent_sdk_floor_matches_the_declared_dependency.
MINIMUM_AGENT_SDK = (0, 2, 144)

#: Failures that installing is expected to fix. A machine whose problems are a
#: subset of these is ready to install; anything else is a prerequisite the
#: installer cannot supply for you. Signing in is deliberately not here: no
#: installer can do it, and offering one as the fix would send an agent past
#: the one step that needs a human.
INSTALLABLE_PROBLEMS = frozenset({"daemon", "launch_agent", "wrapper"})

#: How long one Claude CLI probe may take. Generous enough for a cold start on
#: a busy Mac, short enough that a wedged install cannot hang a preflight.
CLAUDE_PROBE_TIMEOUT_SECONDS = 20.0

#: How much captured output may reach the report. A diagnosis needs the first
#: line of an error, not a screenful.
MAX_PROBE_OUTPUT_CHARS = 400

#: How much of each stream is retained. The rest is read and dropped rather
#: than left in the pipe: a child whose pipe fills stops writing and never
#: exits, so a reader that walks away at the cap turns a CLI that merely says
#: too much into a timeout. This is what bounds memory, and what keeps the
#: redaction pass off a stream that could otherwise be gigabytes.
MAX_CAPTURE_BYTES = 64 * 1024

_READ_CHUNK_BYTES = 8192

#: A killed child is reaped promptly or not at all; this only exists so a
#: pathological case cannot park the preflight in wait().
_REAP_TIMEOUT_SECONDS = 5.0

#: How much of a single CLI-supplied field may reach the report. A version or
#: an auth mode is a word; anything longer is not one, and must not be able to
#: crowd the rest of the line out of the report-wide cap.
MAX_AUTH_FIELD_CHARS = 40

#: The only argument vectors the doctor may hand the Claude CLI. Both report;
#: neither writes. ``_run_claude`` refuses everything else, so "the doctor
#: never mutates a session" is enforced rather than merely intended.
CLAUDE_VERSION_ARGV = ("--version",)
CLAUDE_AUTH_ARGV = ("auth", "status", "--json")
READ_ONLY_CLAUDE_ARGV = frozenset({CLAUDE_VERSION_ARGV, CLAUDE_AUTH_ARGV})

#: The vocabulary of ``state`` on the three Claude-related checks. Declared so
#: an agent can enumerate the branches instead of matching on prose.
CLAUDE_CLI_STATES = ("ready", "missing", "not_executable", "timeout", "failed",
                     "unrecognized")
CLAUDE_AUTH_STATES = ("signed_in", "logged_out", "timeout", "incompatible",
                      "unverifiable")
AGENT_SDK_STATES = ("ready", "missing", "too_old", "unrecognized")

#: The only auth-status fields that may be reported. The CLI also prints the
#: account's address and organisation; a report that travels to Hermes and
#: sits in a log has no business carrying either.
REPORTED_AUTH_FIELDS = (
    ("authMethod", "method"), ("apiProvider", "provider"), ("subscriptionType", "plan"),
)

# Digit counts are bounded: an unbounded \d+ turns CLI output into an integer
# of arbitrary size, which Python 3.12 refuses to parse at all above 4300
# digits — an uncaught ValueError inside a preflight that must not crash.
_VERSION_RE = re.compile(r"\b(\d{1,9})\.(\d{1,9})(?:\.(\d{1,9}))?\b")

# An address is not credential-shaped, so redaction.py — which exists to catch
# tokens — never sees it. The auth probe is the one place one can turn up.
#
# Anchored on the ``@``, for the reason redaction._redact_assignments is
# anchored on the credential word: a pattern that opens with an unbounded run
# of local-part characters walks that run and back at every position, which
# on 64 KiB of ``a.`` cost 5.5 seconds — inside a function the doctor calls
# on whatever a CLI printed.
_AT_SIGN = re.compile("@")
_LOCAL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-"
)
#: The domain half of the pattern this replaced, unchanged, and matched at the
#: character after the ``@`` rather than searched for. Left as a regex on
#: purpose: rewriting it by hand quietly lost addresses whose run of domain
#: characters ends in something that is not a top-level domain. The engine
#: backtracks to the last label that is one; a hand-rolled walk took the run
#: whole, and so lost every address with a trailing label after the domain.
_DOMAIN_RE = re.compile(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _redact_addresses(text: str) -> str:
    """Replace e-mail addresses in *text*.

    Linear: an ``@`` is the only place an address can be, so the scan visits
    each one and walks left over the local part, instead of asking the engine
    to find where that part began. An ``@`` belongs to neither half, so the
    walks and the domain matches cannot overlap, and the total stays
    proportional to the input.

    The local part is taken maximally where the pattern this replaced wanted
    a word boundary, so a leading ``.`` is swallowed too. That direction is
    the safe one: it removes a character that was not part of the address,
    never leaves one that was.
    """
    pieces: list[str] = []
    cursor = 0
    for at in _AT_SIGN.finditer(text):
        start = at.start()
        while start > cursor and text[start - 1] in _LOCAL_CHARS:
            start -= 1
        domain = _DOMAIN_RE.match(text, at.end())
        if start == at.start() or domain is None:
            continue
        pieces.append(text[cursor:start])
        pieces.append("[redacted:address]")
        cursor = domain.end()
    if not pieces:
        return text
    pieces.append(text[cursor:])
    return "".join(pieces)


def _probe(name: str) -> bool:
    """Is *name* on PATH? Isolated so a test can pin the environment."""
    return shutil.which(name) is not None


def _entry(
    name: str, ok: bool, detail: str, *, required: bool = True, fix: str = "", **extra: Any
) -> dict[str, Any]:
    return {"name": name, "ok": ok, "required": required, "detail": detail,
            "fix": "" if ok else fix, **extra}


def _bounded(text: str, limit: int = MAX_PROBE_OUTPUT_CHARS) -> str:
    """Collapse, redact and then truncate *text* for the report.

    Every string the CLI supplies goes through here, on the success path as
    well as the failure paths: "signed in" is CLI output too, and a field
    interpolated raw would be a way past both the redaction and the cap.

    Redaction comes before truncation on purpose: cutting first can split a
    token across the boundary so the pattern no longer matches, and half a
    credential is still a credential.
    """
    cleaned = redaction.redact_text(" ".join(text.split()))
    cleaned = _redact_addresses(cleaned)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(limit - 3, 0)] + "..."


def _version_tuple(text: str) -> tuple[int, ...] | None:
    """The first dotted version in *text*, or None if there is none."""
    match = _VERSION_RE.search(text or "")
    if match is None:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


@dataclass(frozen=True)
class _Probe:
    """One finished CLI probe. ``code`` is None when it never answered."""

    argv: tuple[str, ...]
    code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""

    @property
    def answered(self) -> bool:
        return self.code is not None

    @property
    def message(self) -> str:
        """The bounded, redacted line that best explains a failure."""
        return _bounded(self.stderr.strip() or self.stdout.strip())


def _decode(raw: bytes) -> str:
    """Turn a captured stream into text, whatever is in it.

    Strict decoding — what ``text=True`` gives you — let a single byte of
    something other than UTF-8 end the preflight with a traceback instead of
    a verdict. Describing a broken machine is the one thing this must not
    fail at, so undecodable bytes become replacement characters.
    """
    return raw.decode("utf-8", errors="replace")


def _describe_exception(exc: BaseException) -> str:
    """Name a failure without letting its text into the report unbounded."""
    return _bounded(f"{type(exc).__name__}: {exc}")


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    """Create the child process. The one seam a test pins.

    Bytes, not text: decoding happens after the capture is bounded. bufsize=0
    for the reason in :func:`_drain`.
    """
    return subprocess.Popen(  # noqa: S603 - allowlisted argv, no shell
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0,
    )


def _drain(
    process: subprocess.Popen[bytes], deadline: float
) -> tuple[bytes, bytes, bool]:
    """Read both streams to EOF, retaining at most MAX_CAPTURE_BYTES of each.

    Returns the retained prefixes and whether the deadline ran out first.

    Reading past the cap and discarding is deliberate. Stopping at the cap
    would leave the child blocked on a full pipe, which turns "this CLI
    printed too much" into "this CLI hung" — a wrong diagnosis, and a
    guaranteed timeout instead of a prompt answer.

    ``bufsize=0`` and raw ``os.read`` matter: a BufferedReader can pull bytes
    out of the pipe into userspace, after which the selector never reports
    them again and the loop waits for data it is already holding.
    """
    kept = {"out": bytearray(), "err": bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for name, pipe in (("out", process.stdout), ("err", process.stderr)):
            if pipe is not None:
                selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return bytes(kept["out"]), bytes(kept["err"]), True
            for event, _mask in selector.select(timeout=remaining):
                chunk = os.read(event.fd, _READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(event.fileobj)
                    continue
                buffer = kept[str(event.data)]
                if len(buffer) < MAX_CAPTURE_BYTES:
                    buffer += chunk[: MAX_CAPTURE_BYTES - len(buffer)]
    finally:
        selector.close()
    return bytes(kept["out"]), bytes(kept["err"]), False


def _reap(process: subprocess.Popen[bytes]) -> None:
    """Kill and collect the child, closing the pipes it might be holding."""
    try:
        if process.poll() is None:
            process.kill()
    except OSError:  # already gone
        pass
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        process.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - reaping must not raise into a verdict
        pass


def _run_claude(cli: Path, argv: tuple[str, ...]) -> _Probe:
    """Run one allowlisted, read-only Claude CLI command.

    Four things keep this safe to run on an operator's machine: the
    allowlist, which cannot express a command that writes; a closed stdin, so
    nothing can stop and prompt; a deadline covering the capture and the wait
    together; and a capture that is bounded in memory however much the child
    decides to print.

    Every way this can fail becomes a :class:`_Probe`, never an exception.
    The doctor's contract is that it always emits one machine-readable
    report, and a preflight that raises has broken it however good its
    reason.
    """
    if argv not in READ_ONLY_CLAUDE_ARGV:
        raise ValueError(
            f"the doctor only runs read-only Claude commands, not {' '.join(argv)!r}"
        )
    deadline = time.monotonic() + CLAUDE_PROBE_TIMEOUT_SECONDS
    try:
        process = _spawn([str(cli), *argv])
    except Exception as exc:  # noqa: BLE001 - a spawn failure is a verdict
        return _Probe(argv, None, error=_describe_exception(exc))
    try:
        out, err, timed_out = _drain(process, deadline)
        if timed_out:
            # Retained output is dropped with the verdict: a prefix of what a
            # wedged CLI managed to say is not a diagnosis.
            return _Probe(argv, None, timed_out=True)
        try:
            code = process.wait(timeout=max(deadline - time.monotonic(), 0.0))
        except subprocess.TimeoutExpired:
            return _Probe(argv, None, timed_out=True)
    except Exception as exc:  # noqa: BLE001 - so does anything during capture
        return _Probe(argv, None, error=_describe_exception(exc))
    finally:
        _reap(process)
    return _Probe(argv, code, _decode(out), _decode(err))


def _platform_check() -> dict[str, Any]:
    system = platform.system()
    return _entry(
        "platform", system == "Darwin", f"{system} {platform.release()}",
        fix="the runner is a macOS LaunchAgent; install it on the Mac that runs Claude Code",
    )


def _python_check() -> dict[str, Any]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    floor = ".".join(str(part) for part in MINIMUM_PYTHON)
    return _entry(
        "python", sys.version_info >= MINIMUM_PYTHON, version,
        fix=f"install Python {floor} or newer (uv python install {floor})",
        executable=sys.executable,
    )


def _binary_check(
    name: str, label: str, fix: str, *, required: bool = True
) -> dict[str, Any]:
    found = shutil.which(name)
    detail = found or f"{name} was not found on PATH"
    if not required and not found:
        detail = f"{detail} — {fix}"
    return _entry(
        label, _probe(name), detail, required=required, fix=fix, path=found,
    )


def _sdk_version() -> str | None:
    """The installed claude-agent-sdk version, or None if there is none.

    Imported here rather than at module scope: a virtualenv too broken to
    import the SDK is exactly what this check exists to name, and a top-level
    import would take the doctor down with it.
    """
    try:
        import claude_agent_sdk
    except Exception:  # noqa: BLE001 - any import failure means "no usable SDK"
        return None
    declared = getattr(claude_agent_sdk, "__version__", None)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    try:
        from importlib.metadata import version as distribution_version

        return distribution_version("claude-agent-sdk")
    except Exception:  # noqa: BLE001 - an installed package with no metadata
        return None


def _agent_sdk_check() -> dict[str, Any]:
    """The library the worker imports, not the CLI it drives."""
    floor = ".".join(str(part) for part in MINIMUM_AGENT_SDK)
    raw = _sdk_version()
    if raw is not None:
        raw = _bounded(raw, MAX_AUTH_FIELD_CHARS)
    if raw is None:
        return _entry(
            "agent_sdk", False, "claude-agent-sdk is not importable",
            fix=f"uv sync (the runner needs claude-agent-sdk {floor} or newer)",
            version=None, state="missing",
        )
    parsed = _version_tuple(raw)
    if parsed is None:
        return _entry(
            "agent_sdk", False,
            f"claude-agent-sdk reports an unreadable version: {raw}",
            fix=f"uv sync to reinstall claude-agent-sdk {floor} or newer",
            version=raw, state="unrecognized",
        )
    if parsed < MINIMUM_AGENT_SDK:
        return _entry(
            "agent_sdk", False,
            f"claude-agent-sdk {raw} is older than the required {floor}",
            fix="uv sync, or uv lock --upgrade-package claude-agent-sdk",
            version=raw, state="too_old",
        )
    return _entry(
        "agent_sdk", True, f"claude-agent-sdk {raw} (needs {floor} or newer)",
        version=raw, state="ready",
    )


def _resolve_claude_cli(paths: RunnerPaths) -> Path | None:
    """The configured binary wins; PATH is the documented fallback."""
    if paths.claude_cli_path.exists():
        return paths.claude_cli_path
    found = shutil.which("claude")
    return Path(found) if found else None


def _claude_cli_check(paths: RunnerPaths) -> dict[str, Any]:
    """A path is not an install: the binary has to run and name itself."""
    install = "install Claude Code and sign in once: https://claude.com/claude-code"
    cli = _resolve_claude_cli(paths)
    if cli is None:
        return _entry(
            "claude_cli", False,
            _bounded(f"no claude CLI at {paths.claude_cli_path} and none on PATH"),
            fix=install, path=None, version=None, state="missing",
        )
    if not cli.is_file() or not os.access(cli, os.X_OK):
        return _entry(
            "claude_cli", False, _bounded(f"{cli} is not an executable file"),
            fix=f"chmod +x {cli}, or point HERMES_CLAUDE_RUNNER_CLAUDE_CLI at the binary",
            path=str(cli), version=None, state="not_executable",
        )
    probe = _run_claude(cli, CLAUDE_VERSION_ARGV)
    if probe.timed_out:
        return _entry(
            "claude_cli", False,
            _bounded(f"{cli} did not answer `claude --version` within "
                     f"{CLAUDE_PROBE_TIMEOUT_SECONDS:.0f}s"),
            fix="run `claude --version` by hand; an install that hangs cannot drive a run",
            path=str(cli), version=None, state="timeout",
        )
    if not probe.answered:
        return _entry(
            "claude_cli", False, _bounded(f"{cli} could not be run: {probe.error}"),
            fix=f"check that {cli} is the Claude Code binary, or reinstall it: {install}",
            path=str(cli), version=None, state="not_executable",
        )
    if probe.code != 0:
        return _entry(
            "claude_cli", False,
            _bounded(f"{cli} exited {probe.code} on `claude --version`: "
                     f"{probe.message}"),
            fix=f"reinstall Claude Code: {install}",
            path=str(cli), version=None, state="failed",
        )
    version = _version_tuple(probe.stdout)
    if version is None:
        return _entry(
            "claude_cli", False,
            _bounded(f"{cli} answered `claude --version` with no version in it: "
                     f"{probe.stdout}"),
            fix="point HERMES_CLAUDE_RUNNER_CLAUDE_CLI at the Claude Code binary, "
                f"or reinstall it: {install}",
            path=str(cli), version=None, state="unrecognized",
        )
    printable = _bounded(".".join(str(part) for part in version), MAX_AUTH_FIELD_CHARS)
    return _entry(
        "claude_cli", True, _bounded(f"{cli} (Claude Code {printable})"),
        path=str(cli), version=printable, state="ready",
    )


def _parse_auth_status(stdout: str) -> dict[str, Any] | None:
    """The CLI's answer, or None when it did not give one this doctor can read.

    RecursionError is not theoretical and the capture cap does not prevent
    it: 64 KiB of ``[`` is a nesting depth of 65536, which the parser hits
    long before it runs out of input.
    """
    try:
        payload = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("loggedIn"), bool):
        return None
    return payload


def _cli_label(cli_entry: dict[str, Any]) -> str:
    """Name the binary by its version, which is parsed rather than quoted."""
    version = cli_entry.get("version")
    return f"Claude Code {version}" if version else "this Claude Code"


def _describe_session(status: dict[str, Any]) -> str:
    """The session's mode, and nothing that identifies whose it is.

    Each field is bounded on its own so no one of them can fill the line, and
    the composed result is bounded again so the total holds however many
    fields REPORTED_AUTH_FIELDS grows to.
    """
    parts = [
        f"{label} {_bounded(status[key], MAX_AUTH_FIELD_CHARS)}"
        for key, label in REPORTED_AUTH_FIELDS
        if isinstance(status.get(key), str) and status[key].strip()
    ]
    return _bounded("signed in" + (f" ({', '.join(parts)})" if parts else ""))


def _claude_auth_check(cli_entry: dict[str, Any]) -> dict[str, Any]:
    """Ask the CLI whether a session exists, without ever handling one.

    ``claude auth status --json`` reports; it does not sign in and it opens
    nothing. Only ``loggedIn`` and the mode fields are read, and the raw
    answer is never echoed back on any path: it carries the account's
    address, and its organisation, which no redaction can recognise.
    """
    login = ("run `claude auth login` on this Mac and complete the sign-in; "
             "the doctor never signs in for you")
    if not cli_entry["ok"]:
        return _entry(
            "claude_auth", False, "not checked: the Claude CLI is not usable",
            fix=cli_entry["fix"] or "install Claude Code: https://claude.com/claude-code",
            state="unverifiable",
        )
    probe = _run_claude(Path(cli_entry["path"]), CLAUDE_AUTH_ARGV)
    if probe.timed_out:
        return _entry(
            "claude_auth", False,
            "`claude auth status` did not answer within "
            f"{CLAUDE_PROBE_TIMEOUT_SECONDS:.0f}s",
            fix="run `claude auth status` in a terminal to see what it is waiting for",
            state="timeout",
        )
    if not probe.answered:
        return _entry(
            "claude_auth", False,
            _bounded(f"`claude auth status` could not be run: {probe.error}"),
            fix="run `claude auth status` in a terminal", state="unverifiable",
        )
    status = _parse_auth_status(probe.stdout)
    if status is None:
        # Deliberately exit-code-agnostic up to here: releases differ on what
        # a logged-out session exits with, so the payload decides. Only when
        # there is no payload does the exit code separate "too old to answer"
        # from "answered in something this doctor cannot read".
        #
        # Neither branch quotes the CLI. Whatever this command printed, on
        # either stream, is the auth payload or something next to it, and it
        # carries the account's organisation as readily as its address —
        # which redaction cannot help with, because an organisation has no
        # shape to match. So the answer is classified and the report says
        # which binary refused and how, which is what the fix needs anyway.
        if probe.code != 0:
            return _entry(
                "claude_auth", False,
                f"{_cli_label(cli_entry)} does not answer "
                f"`claude auth status --json` (exit {probe.code})",
                fix="update Claude Code: claude update", state="incompatible",
            )
        return _entry(
            "claude_auth", False,
            f"{_cli_label(cli_entry)} answered `claude auth status --json` with "
            f"{len(probe.stdout)} bytes that are not JSON",
            fix="run `claude auth status` in a terminal and confirm the session by hand",
            state="unverifiable",
        )
    if not status["loggedIn"]:
        return _entry("claude_auth", False, "no signed-in Claude Code session",
                      fix=login, state="logged_out")
    return _entry("claude_auth", True, _describe_session(status), state="signed_in")


def _projects_root_check(paths: RunnerPaths) -> dict[str, Any]:
    root = paths.projects_root
    return _entry(
        "projects_root", root.is_dir(), str(root),
        fix=f"create {root}, or point HERMES_CLAUDE_RUNNER_PROJECTS_ROOT at your code",
    )


def _database_check(paths: RunnerPaths) -> dict[str, Any]:
    """Report the schema version without ever creating or migrating the file."""
    path = paths.db_path
    if not path.exists():
        return _entry(
            "database", False, f"no database at {path}", required=False,
            fix="the daemon creates it on first start; nothing to do before installing",
        )
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _entry("database", False, f"{path} is unreadable: {exc}",
                      fix="inspect the file by hand; the runner never deletes state")
    return _entry(
        "database", version <= db.SCHEMA_VERSION,
        f"{path} (schema {version}, runner speaks {db.SCHEMA_VERSION})",
        fix="the database is newer than this checkout; update the runner",
        schema_version=version,
    )


def _daemon_check(paths: RunnerPaths, *, probe: bool) -> dict[str, Any]:
    if not probe:
        return _entry("daemon", False, "skipped (--no-daemon-probe)", required=False,
                      fix="run without --no-daemon-probe to contact the socket")
    if not paths.socket_path.exists():
        return _entry(
            "daemon", False, f"no socket at {paths.socket_path}",
            fix="./scripts/install_runner.sh, then hermes-claude-runner health",
        )
    envelope = client.send_request(paths.socket_path, {"action": "health"}, timeout=10)
    if not envelope.get("ok"):
        return _entry(
            "daemon", False, str(envelope.get("detail") or envelope.get("error")),
            fix="launchctl kickstart -k gui/$(id -u)/<label>",
        )
    result = envelope.get("result") or {}
    return _entry(
        "daemon", True,
        f"version {result.get('version')}, {result.get('active_runs')} active run(s)",
        active_runs=result.get("active_runs"),
    )


def _launch_agent_check(paths: RunnerPaths) -> dict[str, Any]:
    exists = paths.plist_path.is_file()
    return _entry(
        "launch_agent", exists, str(paths.plist_path),
        fix="./scripts/install_runner.sh writes it (idempotently, with a backup)",
    )


def _wrapper_check(paths: RunnerPaths) -> dict[str, Any]:
    path = paths.wrapper_path
    ok = path.is_file() and os.access(path, os.X_OK)
    return _entry(
        "wrapper", ok, str(path),
        fix="./scripts/install_runner.sh writes and chmods it",
    )


def diagnose(paths: RunnerPaths, *, probe_daemon: bool = True) -> dict[str, Any]:
    """Inspect the machine and return a JSON-serializable verdict."""
    # The auth probe is only worth spending on a binary that already answered,
    # so the CLI check runs first and hands its verdict on.
    claude_cli = _claude_cli_check(paths)
    checks = [
        _platform_check(),
        _python_check(),
        _binary_check("git", "git", "install git (xcode-select --install)"),
        # Every documented command starts with `uv run`, and install_runner.sh
        # runs `uv sync` unconditionally, so a warning here would send an agent
        # two phases further to an unmapped "uv: command not found".
        _binary_check("uv", "uv", "install uv: https://docs.astral.sh/uv/"),
        # Claude Code is the requirement; Node is an implementation detail of
        # some Claude Code installs and absent from others.
        _binary_check("node", "node",
                      "only needed if your Claude Code install requires Node.js",
                      required=False),
        _agent_sdk_check(),
        claude_cli,
        _claude_auth_check(claude_cli),
        _projects_root_check(paths),
        _database_check(paths),
        _daemon_check(paths, probe=probe_daemon),
        _launch_agent_check(paths),
        _wrapper_check(paths),
    ]
    problems = [c["name"] for c in checks if c["required"] and not c["ok"]]
    return {
        "ok": not problems,
        # True when installing is all that stands between this machine and ok.
        "installable": set(problems) <= INSTALLABLE_PROBLEMS,
        "runner_version": __version__,
        "label": paths.launch_agent_label,
        "paths": {
            "projects_root": str(paths.projects_root),
            "worktrees_root": str(paths.worktrees_root),
            "database": str(paths.db_path),
            "socket": str(paths.socket_path),
            "logs": str(paths.log_dir),
            "wrapper": str(paths.wrapper_path),
            "launch_agent": str(paths.plist_path),
        },
        "checks": checks,
        "problems": problems,
    }


def render(report: dict[str, Any]) -> str:
    """The same verdict as a human-readable table."""
    width = max(len(c["name"]) for c in report["checks"])
    lines = [f"hermes-claude-runner {report['runner_version']}  ({report['label']})", ""]
    for entry in report["checks"]:
        mark = "ok  " if entry["ok"] else ("FAIL" if entry["required"] else "warn")
        lines.append(f"  [{mark}] {entry['name']:<{width}}  {entry['detail']}")
        if entry["fix"]:
            lines.append(f"         {' ' * width}  -> {entry['fix']}")
    lines.append("")
    if report["ok"]:
        lines.append("READY")
    elif report["installable"]:
        lines.append(
            "READY TO INSTALL: only the service is missing "
            f"({', '.join(report['problems'])}). Run ./scripts/install_runner.sh"
        )
    else:
        lines.append(f"NOT READY: {', '.join(report['problems'])}")
    return "\n".join(lines)
