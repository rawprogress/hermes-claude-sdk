"""Minimal unix-socket client used by ``hermes-claude-runner rpc``.

Kept dependency-free and side-effect-free so the ``rpc`` process stays a thin
pipe between Hermes' ssh invocation and the daemon.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from .errors import RunnerError

# ``start`` runs `git worktree add` inline, which may take up to
# worktree.GIT_TIMEOUT_SECONDS; a shorter timeout here would report
# daemon_unavailable for a run that actually started.
DEFAULT_TIMEOUT_SECONDS = 180.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def send_request(
    socket_path: Path | str,
    request: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Send one request and return the response envelope.

    Transport failures become a ``daemon_unavailable`` envelope so callers
    always receive well-formed JSON.
    """
    payload = json.dumps(request, ensure_ascii=False, default=str).encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            received = 0
            while chunk := sock.recv(65536):
                chunks.append(chunk)
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    raise RunnerError("daemon_unavailable", "response exceeded the size limit")
    except RunnerError as exc:
        return exc.to_envelope()
    except (OSError, TimeoutError) as exc:
        return RunnerError(
            "daemon_unavailable",
            f"cannot reach the runner daemon at {socket_path}: {exc}",
        ).to_envelope()

    raw = b"".join(chunks).decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return RunnerError("daemon_unavailable", "daemon returned malformed JSON").to_envelope()
    if not isinstance(parsed, dict):
        return RunnerError("daemon_unavailable", "daemon returned a non-object").to_envelope()
    return parsed
