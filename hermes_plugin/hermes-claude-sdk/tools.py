"""Handlers and the ssh transport.

Standard library only: the Hermes venv must not grow a dependency for this.
Every handler returns a JSON string, including on every failure path, so the
model always receives a parseable envelope.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SSH_HOST = "macbook"
# Home-relative on purpose: ssh hands the command to the Mac's login shell,
# which expands the tilde for whichever account runs the runner. An absolute
# path is still accepted and is the better choice when pinning an install.
DEFAULT_REMOTE_COMMAND = "~/.local/bin/hermes-claude-runner"
# The runner's start handler runs `git worktree add` inline (120s) and the rpc
# client waits on the daemon socket for it (180s), so the ssh round trip has to
# outlast both — otherwise a run that really started comes back as ssh_timeout.
# This is deliberately longer than the runner's own 180s socket timeout, not
# equal to it: the outer wait has to survive the inner one expiring.
MIN_TIMEOUT_SECONDS = 240
DEFAULT_TIMEOUT_SECONDS = 240

# ssh reads a destination starting with "-" as an option, so a hostile setting
# could smuggle in -oProxyCommand. Both settings are matched against strict
# shapes and fall back to the documented defaults when they do not fit.
_SSH_HOST_RE = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._@:-]*\Z")
# "~/" (the runner's own account) or an absolute path. "~other/" is refused:
# the remote shell would resolve it against a different account's home.
_REMOTE_COMMAND_RE = re.compile(r"\A(~/|/)[A-Za-z0-9._+/-]+\Z")

# Reaching the Mac is the one step a fresh install has to get right by hand,
# so every transport failure names the settings that decide it.
_HINT = (
    "Check `ssh <host> true` from this machine and the plugin's ssh_host / "
    "remote_command settings in ~/.hermes/config.yaml"
)

MAX_OUTPUT_CHARS = 24_000
MAX_PROMPT_CHARS = 100_000
MAX_MESSAGE_CHARS = 100_000

# json.dumps separates list items with ", " by default, not ",". Deriving the
# width keeps the size accounting exact even if that default ever changes.
_LIST_SEPARATOR_CHARS = len(json.dumps([0, 0])) - len(json.dumps([0])) - 1

# Populated by register(); None means "use the documented defaults".
_settings: dict[str, Any] | None = None


def _render(value: Any) -> str:
    """Serialize exactly the way _dump does, so sizing matches the wire."""
    return json.dumps(value, ensure_ascii=False, default=str)


def configure(settings: dict[str, Any] | None) -> None:
    """Install the plugin settings the transport should use."""
    global _settings
    _settings = dict(settings) if isinstance(settings, dict) else None


def _setting(key: str, default: Any, kind: type, pattern: re.Pattern[str] | None = None) -> Any:
    value = (_settings or {}).get(key, default)
    if not isinstance(value, kind) or isinstance(value, bool) != (kind is bool):
        return default
    if kind is str:
        if not value.strip():
            return default
        if pattern is not None and not pattern.match(value):
            logger.warning("ignoring unsafe plugin setting %s=%r; using the default", key, value)
            return default
    if kind is int and value <= 0:
        return default
    return value


def ssh_argv() -> list[str]:
    """The one command shape this plugin is allowed to run."""
    return [
        "ssh", "-o", "BatchMode=yes",
        _setting("ssh_host", DEFAULT_SSH_HOST, str, _SSH_HOST_RE),
        _setting("remote_command", DEFAULT_REMOTE_COMMAND, str, _REMOTE_COMMAND_RE),
        "rpc",
    ]


def _envelope(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "detail": detail[:2000]}


def _dump(payload: dict[str, Any]) -> str:
    text = _render(payload)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return json.dumps(_envelope(
        "response_too_large",
        f"the runner returned {len(text)} characters; narrow the request "
        f"(fewer events, smaller limit)",
    ), ensure_ascii=False)


def _shrink_events(payload: dict[str, Any], after: int) -> dict[str, Any]:
    """Drop trailing events until the envelope fits, keeping the cursor honest.

    Each event is sized once rather than re-serializing the whole payload per
    drop. When even the first event does not fit, the list comes back empty and
    the cursor stays at the caller's ``after`` — reporting a cursor past an
    event nobody received would silently skip it.
    """
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("events"), list):
        return payload

    events = list(result["events"])
    if not events:
        return payload

    # Budget for the events themselves: everything else in the envelope has to
    # fit too, so measure the payload once with the events removed. The
    # "truncated" key is included in that measurement because shortening the
    # page adds it, and its own bytes count against the same cap.
    result["events"] = []
    result.setdefault("truncated", False)
    overhead = len(_render(payload))
    # The measured overhead already contains the empty list's brackets, so the
    # remaining budget covers the items plus the separators between them.
    budget = MAX_OUTPUT_CHARS - overhead

    total = 0
    kept = 0
    for index, event in enumerate(events):
        addition = len(_render(event)) + (_LIST_SEPARATOR_CHARS if index else 0)
        if total + addition > budget:
            break
        total += addition
        kept += 1

    if kept == len(events):
        result["events"] = events
        return payload

    result["events"] = events[:kept]
    result["truncated"] = True
    if kept:
        result["next_cursor"] = events[kept - 1].get("seq", result.get("next_cursor"))
    else:
        # The cursor stays put: reporting progress past an event nobody
        # received would silently skip it. Retrying at limit=1 would return
        # the very same event (the runner only stubs above its own, much
        # larger cap) and claude_status carries no event payloads, so an
        # explicit skip is genuinely the only way forward from Hermes.
        oversized = events[0]
        seq = oversized.get("seq")
        result["next_cursor"] = after
        result["dropped_oversized_event"] = True
        result["oversized_seq"] = seq
        result["kind"] = oversized.get("kind")
        result["hint"] = (
            f"event {seq} exceeds what this transport can carry; its full content is "
            f"readable only on the Mac (runner database and worker log). To move on, "
            f"call claude_events again with after={seq} to skip it deliberately."
        )
    return payload


def call_runner(request: dict[str, Any]) -> dict[str, Any]:
    """Send one request over ssh and return the runner's envelope."""
    argv = ssh_argv()
    # A shorter configured timeout would only ever hide a live run behind a
    # misleading error, so the floor wins.
    timeout = max(
        _setting("timeout_seconds", DEFAULT_TIMEOUT_SECONDS, int), MIN_TIMEOUT_SECONDS
    )
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, never a shell
            argv,
            input=json.dumps(request, ensure_ascii=False, default=str),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope("ssh_timeout", f"no answer from {argv[3]} within {timeout}s. {_HINT}")
    except FileNotFoundError:
        return _envelope("ssh_unavailable", "the ssh binary was not found on this machine")
    except Exception as exc:  # noqa: BLE001 - the model must still get JSON
        logger.exception("claude_sdk transport failed")
        return _envelope("plugin_error", f"{exc.__class__.__name__}: {exc}")

    stdout = (completed.stdout or "").strip()
    if completed.returncode != 0 and not stdout:
        detail = (completed.stderr or "").strip() or f"ssh exited {completed.returncode}"
        return _envelope("ssh_failed", f"{detail}. {_HINT}")
    if not stdout:
        return _envelope("bad_response", "the runner returned nothing on stdout")
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return _envelope("bad_response", f"runner stdout was not JSON: {stdout[:500]}")
    if not isinstance(parsed, dict) or "ok" not in parsed:
        return _envelope("bad_response", f"unexpected envelope: {stdout[:500]}")
    return parsed


def _hermes_identity(kwargs: dict[str, Any]) -> tuple[str | None, str | None]:
    session_id = kwargs.get("session_id") or kwargs.get("task_id")
    task_id = kwargs.get("task_id")
    return (session_id or None), (task_id or None)


def _text(args: dict[str, Any], key: str, max_chars: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required and must be a non-empty string")
    if len(value) > max_chars:
        raise ValueError(f"{key} exceeds {max_chars} characters")
    return value


def _index(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _optional_text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string when supplied")
    return value


def _run_id(args: dict[str, Any]) -> str:
    return _text(args, "run_id", 128)


def _dispatch(request: dict[str, Any], *, shrink_after: int | None = None) -> str:
    response = call_runner(request)
    if shrink_after is not None:
        response = _shrink_events(response, shrink_after)
    return _dump(response)


def _guard(handler):
    """Turn any handler failure into a JSON envelope."""

    def wrapped(args: Any, **kwargs: Any) -> str:
        if not isinstance(args, dict):
            return json.dumps(_envelope("invalid_params", "arguments must be an object"))
        try:
            return handler(args, **kwargs)
        except ValueError as exc:
            return json.dumps(_envelope("invalid_params", str(exc)))
        except Exception as exc:  # noqa: BLE001 - never raise into the agent loop
            logger.exception("claude_sdk handler failed")
            return json.dumps(_envelope("plugin_error", f"{exc.__class__.__name__}: {exc}"))

    wrapped.__name__ = getattr(handler, "__name__", "handler")
    wrapped.__doc__ = handler.__doc__
    return wrapped


@_guard
def handle_claude_start(args: dict[str, Any], **kwargs: Any) -> str:
    """Start a Claude run on the Mac."""
    session_id, task_id = _hermes_identity(kwargs)
    create_worktree = args.get("create_worktree", True)
    if not isinstance(create_worktree, bool):
        raise ValueError("create_worktree must be a boolean")
    role = args.get("role") or "implementer"
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")
    return _dispatch({
        "action": "start",
        "project": _text(args, "project", 4096),
        "prompt": _text(args, "prompt", MAX_PROMPT_CHARS),
        "role": role,
        "create_worktree": create_worktree,
        "hermes_session_id": session_id,
        "hermes_task_id": task_id,
    })


@_guard
def handle_claude_send(args: dict[str, Any], **kwargs: Any) -> str:
    """Queue a follow-up for a live run."""
    return _dispatch({
        "action": "send",
        "run_id": _run_id(args),
        "message": _text(args, "message", MAX_MESSAGE_CHARS),
    })


@_guard
def handle_claude_status(args: dict[str, Any], **kwargs: Any) -> str:
    """Durable status of one run."""
    return _dispatch({"action": "status", "run_id": _run_id(args)})


@_guard
def handle_claude_events(args: dict[str, Any], **kwargs: Any) -> str:
    """Ordered structured events for one run."""
    after = _index(args, "after", 0)
    return _dispatch({
        "action": "events",
        "run_id": _run_id(args),
        "after": after,
        "limit": _index(args, "limit", 100),
    }, shrink_after=after)


@_guard
def handle_claude_list(args: dict[str, Any], **kwargs: Any) -> str:
    """List recent and active runs."""
    return _dispatch({
        "action": "list",
        "project": _optional_text(args, "project"),
        "status": _optional_text(args, "status"),
        "limit": _index(args, "limit", 50),
    })


@_guard
def handle_claude_stop(args: dict[str, Any], **kwargs: Any) -> str:
    """Request a controlled stop."""
    return _dispatch({"action": "stop", "run_id": _run_id(args)})


@_guard
def handle_claude_resume(args: dict[str, Any], **kwargs: Any) -> str:
    """Respawn a finished run by its stored Claude session id."""
    return _dispatch({
        "action": "resume",
        "run_id": _run_id(args),
        "message": _text(args, "message", MAX_MESSAGE_CHARS),
    })


HANDLERS = {
    "claude_start": handle_claude_start,
    "claude_send": handle_claude_send,
    "claude_status": handle_claude_status,
    "claude_events": handle_claude_events,
    "claude_list": handle_claude_list,
    "claude_stop": handle_claude_stop,
    "claude_resume": handle_claude_resume,
}
