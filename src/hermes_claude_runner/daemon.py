"""The long-lived daemon: owns runs, workers and the unix socket.

Runs under a user LaunchAgent, so it must survive shell disconnects, launch
workers in their own process session, and reconcile anything it lost.
"""

from __future__ import annotations

import calendar
import logging
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import models, rpc
from .config import RunnerPaths
from .store import Store

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 4 * 1024 * 1024
RECONCILE_INTERVAL_SECONDS = 30.0
# A worker that was spawned moments ago may not be visible to ``ps`` yet.
RECONCILE_GRACE_SECONDS = 20.0
# ``ps`` elapsed time has second resolution; allow for spawn latency.
START_TIME_TOLERANCE_SECONDS = 90.0

WORKER_MARKER = "hermes_claude_runner"


def _parse_etime(raw: str) -> int | None:
    """Parse ``ps -o etime=`` (``[[dd-]hh:]mm:ss``) into seconds.

    Elapsed time is timezone-free, so unlike ``lstart`` it stays correct
    across a DST transition.
    """
    text = (raw or "").strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        if not head.isdigit():
            return None
        days = int(head)
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return days * 86_400 + seconds


def _process_elapsed_seconds(pid: int) -> int | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", "-p", str(pid), "-o", "etime="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _parse_etime(completed.stdout)


# The store writes whole seconds; the schema DEFAULT writes fractional
# seconds. Both are UTC and both have to parse, or a row created by the
# default would lose its reconciliation grace period.
_STAMP_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_stamp(stamp: str | None) -> float | None:
    """Parse a UTC store timestamp into epoch seconds, or return None."""
    if not stamp or not isinstance(stamp, str):
        return None
    text = stamp.strip()
    for fmt in _STAMP_FORMATS:
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        seconds = float(calendar.timegm(parsed))
        head, dot, tail = text.rpartition(".")
        if dot and tail.endswith("Z"):
            fraction = tail[:-1]
            if fraction.isdigit():
                seconds += int(fraction) / (10 ** len(fraction))
        return seconds
    return None


def _process_command(pid: int) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _worker_process_group(pid: int) -> int | None:
    """The process group *pid* leads, or ``None`` when it provably leads none.

    ``spawn`` starts every worker with ``start_new_session=True``, so a genuine
    worker leads both its session and its group: ``sid == pgid == pid``. Any
    other shape means the group holds processes this run does not own, so it is
    never signalled — a pid that merely shares a group with strangers, or the
    daemon's own group, falls back to a single-pid signal.
    """
    if pid <= 0:
        return None
    try:
        pgid = os.getpgid(pid)
        sid = os.getsid(pid)
    except (OSError, ValueError):
        # Gone, or owned by somebody else: we cannot prove what the group holds.
        return None
    if pgid != pid or sid != pid:
        return None
    if pgid == os.getpgid(0):  # our own group: signalling it would stop the daemon
        return None
    return pgid


class ProcessSpawner:
    """Launches ``hermes-claude-runner worker`` in a detached process session."""

    def __init__(self, paths: RunnerPaths, python_executable: str | None = None) -> None:
        self.paths = paths
        self.python_executable = python_executable or sys.executable

    def worker_argv(self, run_id: str, resume: str | None = None) -> list[str]:
        argv = [self.python_executable, "-m", WORKER_MARKER, "worker", "--run-id", run_id]
        if resume:
            argv += ["--resume", resume]
        return argv

    def spawn(self, run_id: str, *, resume: str | None = None) -> tuple[int, float]:
        self.paths.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.paths.log_dir / f"worker-{run_id}.log"
        handle = log_path.open("ab", buffering=0)
        started_at = time.time()
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self.worker_argv(run_id, resume),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,  # survives shell and daemon disconnects
                cwd=str(self.paths.data_dir),
            )
        finally:
            handle.close()
        return process.pid, started_at

    def is_alive(
        self,
        pid: int | None,
        started_at: float | None,
        run_id: str | None = None,
    ) -> bool:
        """True only when *pid* is this run's worker, still running.

        Identity is proven from the command line (marker plus the run id),
        which survives pid reuse without relying on a wall clock. Elapsed time
        is a second, timezone-free sanity check.
        """
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError, PermissionError):
            return False

        command = _process_command(pid)
        if not command:
            # ``ps`` told us nothing; fall back to the weaker elapsed-time proof.
            return self._elapsed_matches(pid, started_at)
        if WORKER_MARKER not in command:
            return False
        if run_id is not None and run_id not in command:
            return False
        return self._elapsed_matches(pid, started_at)

    @staticmethod
    def _elapsed_matches(pid: int, started_at: float | None) -> bool:
        if started_at is None:
            return True
        elapsed = _process_elapsed_seconds(pid)
        if elapsed is None:
            return True
        expected = time.time() - started_at
        return abs(expected - elapsed) <= START_TIME_TOLERANCE_SECONDS

    def signal_stop(
        self,
        pid: int | None,
        started_at: float | None,
        run_id: str | None = None,
    ) -> bool:
        """SIGTERM the run's whole worker session so a stop lands completely.

        The worker spawns the Claude CLI as its own child; signalling only the
        worker pid would leave that subprocess alive against the run's
        worktree. The group is therefore the target — but only the one the
        worker provably leads, and only once identity is proven, so a recycled
        pid or a group full of strangers is never signalled.
        """
        if not pid or pid <= 0:
            return False
        pid = int(pid)
        # Resolved before the identity proof so that proof is the last thing to
        # happen before the signal, leaving the narrowest possible window.
        pgid = _worker_process_group(pid)
        if not self.is_alive(pid, started_at, run_id):
            return False

        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass  # the group went away; the single-pid attempt below decides
            else:
                logger.info("sent SIGTERM to worker process group %s for run %s", pgid, run_id)
                return True
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        logger.info("sent SIGTERM to worker pid %s for run %s", pid, run_id)
        return True


class _Handler(socketserver.BaseRequestHandler):
    """One request, one response, one connection."""

    def handle(self) -> None:
        daemon: Daemon = self.server.daemon  # type: ignore[attr-defined]
        self.request.settimeout(120)
        chunks: list[bytes] = []
        received = 0
        try:
            while chunk := self.request.recv(65536):
                chunks.append(chunk)
                received += len(chunk)
                if received > MAX_REQUEST_BYTES:
                    self._reply(rpc.RunnerError(
                        "invalid_request", "request exceeded the size limit"
                    ).to_envelope())
                    return
        except (TimeoutError, OSError):
            return

        raw = b"".join(chunks).decode("utf-8", "replace").strip()
        try:
            request = rpc.parse_request(raw)
        except rpc.RunnerError as exc:
            self._reply(exc.to_envelope())
            return

        store = Store.open(daemon.paths.db_path)
        try:
            runtime = rpc.Runtime(paths=daemon.paths, store=store, spawner=daemon.spawner)
            self._reply(rpc.handle_request(request, runtime))
        finally:
            store.close()

    def _reply(self, envelope: dict[str, Any]) -> None:
        with_newline = rpc.serialize_response(envelope) + "\n"
        try:
            self.request.sendall(with_newline.encode())
        except OSError:
            logger.warning("client disconnected before the response was written")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    # socketserver defaults to a backlog of 5; Hermes can easily open more
    # connections at once, and a refused connect looks like daemon_unavailable.
    request_queue_size = 128


class Daemon:
    """Owns the socket, the spawner and periodic reconciliation."""

    def __init__(self, paths: RunnerPaths, spawner: Any | None = None) -> None:
        self.paths = paths
        self.spawner: Any = spawner if spawner is not None else ProcessSpawner(paths)
        self._server: _Server | None = None
        self._stopping = threading.Event()

    # -- reconciliation ----------------------------------------------------

    def reconcile(self) -> list[str]:
        """Mark active runs whose worker is gone as ``unknown``.

        Never invents success: an absent worker without a final result is
        explicitly unknown.
        """
        reconciled: list[str] = []
        store = Store.open(self.paths.db_path)
        try:
            now = time.time()
            for run in store.active_runs():
                started_at = run["worker_started_at"]
                # A run caught between create_run and spawn has no worker time
                # yet; fall back to when it was last touched so a sweep landing
                # in that window cannot declare it lost.
                reference = started_at if started_at is not None else _parse_stamp(
                    run["updated_at"]
                )
                if reference is not None and now - reference < RECONCILE_GRACE_SECONDS:
                    continue
                if self.spawner.is_alive(run["worker_pid"], started_at, run["run_id"]):
                    continue
                detail = (
                    f"worker pid {run['worker_pid']} is gone; last status was {run['status']}"
                )
                store.append_event(run["run_id"], "reconciled",
                                   {"previous_status": run["status"], "detail": detail})
                store.update_run(run["run_id"], status=models.STATUS_UNKNOWN, error=detail)
                reconciled.append(run["run_id"])
                logger.warning("reconciled run %s to unknown: %s", run["run_id"], detail)
        finally:
            store.close()
        return reconciled

    def _reconcile_loop(self) -> None:
        while not self._stopping.wait(RECONCILE_INTERVAL_SECONDS):
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001 - a bad sweep must not kill the daemon
                logger.exception("reconciliation sweep failed")

    # -- serving -----------------------------------------------------------

    def _prepare_socket(self) -> None:
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.paths.socket_path
        if path.exists() or path.is_symlink():
            if path.is_socket() and self._socket_is_live(path):
                raise RuntimeError(f"another daemon is already listening on {path}")
            path.unlink()

    @staticmethod
    def _socket_is_live(path: Path) -> bool:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            try:
                probe.connect(str(path))
            except OSError:
                return False
        return True

    def serve_forever(self) -> None:
        """Bind the socket and serve until :meth:`shutdown`."""
        Store.open(self.paths.db_path).close()  # ensure the schema exists
        self._prepare_socket()
        previous_umask = os.umask(0o077)  # the socket is private to this user
        try:
            server = _Server(str(self.paths.socket_path), _Handler)
        finally:
            os.umask(previous_umask)
        server.daemon = self  # type: ignore[attr-defined]
        self._server = server

        try:
            self.reconcile()
        except Exception:  # noqa: BLE001 - startup sweep is best effort
            logger.exception("startup reconciliation failed")

        threading.Thread(target=self._reconcile_loop, daemon=True).start()
        logger.info("daemon listening on %s", self.paths.socket_path)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()
            self._cleanup_socket()

    def shutdown(self) -> None:
        self._stopping.set()
        if self._server is not None:
            self._server.shutdown()

    def _cleanup_socket(self) -> None:
        try:
            if self.paths.socket_path.is_socket():
                self.paths.socket_path.unlink()
        except OSError:  # pragma: no cover - best effort
            logger.warning("could not remove %s", self.paths.socket_path)
