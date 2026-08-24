"""Command line entry point.

``rpc`` is the contract Hermes depends on: one JSON object in on stdin, one
JSON envelope out on stdout, and nothing else on stdout ever.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading
from typing import Any

from . import __version__, client, config, models, rpc
from .errors import RunnerError
from .store import Store

LOG_LEVEL_ENV = "HERMES_CLAUDE_RUNNER_LOG_LEVEL"

logger = logging.getLogger("hermes_claude_runner")


def _configure_logging() -> None:
    """All logging goes to stderr; stdout belongs to the RPC envelope."""
    level = os.environ.get(LOG_LEVEL_ENV, "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _emit(envelope: dict[str, Any]) -> None:
    sys.stdout.write(rpc.serialize_response(envelope) + "\n")
    sys.stdout.flush()


def _cmd_rpc(_args: argparse.Namespace, paths: config.RunnerPaths) -> int:
    raw = sys.stdin.read()
    try:
        request = rpc.parse_request(raw)
    except RunnerError as exc:
        _emit(exc.to_envelope())
        return 0
    _emit(client.send_request(paths.socket_path, request, timeout=client.DEFAULT_TIMEOUT_SECONDS))
    return 0


def _cmd_health(_args: argparse.Namespace, paths: config.RunnerPaths) -> int:
    envelope = client.send_request(paths.socket_path, {"action": "health"}, timeout=10)
    _emit(envelope)
    return 0 if envelope.get("ok") else 1


def _cmd_daemon(_args: argparse.Namespace, paths: config.RunnerPaths) -> int:
    from .daemon import Daemon

    server = Daemon(paths)

    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("received signal %s; shutting down", signum)
        server.shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _shutdown)

    logger.info("starting daemon (pid %s)", os.getpid())
    server.serve_forever()
    return 0


def _request_stop_on_signal(
    store: Store, run_id: str, external_stop: threading.Event | None = None
) -> None:
    """Turn a SIGTERM into the same controlled stop the RPC surface requests.

    The in-process event fires first so a signal lands even when the database
    is momentarily busy.
    """
    if external_stop is not None:
        external_stop.set()
    try:
        store.request_stop(run_id)
    except Exception:  # noqa: BLE001 - the in-process channel already fired
        logger.warning("could not persist the stop flag for %s", run_id, exc_info=True)


def _cmd_worker(args: argparse.Namespace, paths: config.RunnerPaths) -> int:
    if not models.is_valid_run_id(args.run_id):
        logger.error("invalid run id: %r", args.run_id)
        return 2

    from . import worker

    store = Store.open(paths.db_path)
    signal_store = Store.open(paths.db_path)
    external_stop = threading.Event()

    def _on_signal(signum: int, _frame: Any) -> None:
        logger.info("worker received signal %s; requesting a controlled stop", signum)
        _request_stop_on_signal(signal_store, args.run_id, external_stop)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous:
        signal.signal(sig, _on_signal)

    try:
        status = asyncio.run(
            worker.run_worker(
                run_id=args.run_id, store=store, paths=paths, resume=args.resume,
                external_stop=external_stop,
            )
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        store.close()
        signal_store.close()

    logger.info("run %s finished with status %s", args.run_id, status)
    return 0 if status in (models.STATUS_COMPLETED, models.STATUS_STOPPED) else 1


def _cmd_version(_args: argparse.Namespace, _paths: config.RunnerPaths) -> int:
    sys.stdout.write(f"hermes-claude-runner {__version__}\n")
    return 0


def _cmd_doctor(args: argparse.Namespace, paths: config.RunnerPaths) -> int:
    """Read-only preflight. Creates nothing, starts nothing."""
    from . import doctor

    report = doctor.diagnose(paths, probe_daemon=not args.no_daemon_probe)
    if args.json:
        sys.stdout.write(json.dumps(report, default=str) + "\n")
    else:
        sys.stdout.write(doctor.render(report) + "\n")
    return 0 if report["ok"] else 1


def _cmd_install(args: argparse.Namespace, paths: config.RunnerPaths) -> int:
    from . import launchd

    report = launchd.install(paths, dry_run=args.dry_run, repo_root=args.repo_root)
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-claude-runner",
        description="Mac-side runner that drives Claude Code for Hermes Agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("rpc", help="read one JSON request on stdin, write one JSON response")
    sub.add_parser("health", help="ask the daemon whether it is alive")
    sub.add_parser("daemon", help="run the supervising daemon (LaunchAgent target)")
    sub.add_parser("version", help="print the runner version")

    worker_parser = sub.add_parser("worker", help="drive one run (spawned by the daemon)")
    worker_parser.add_argument("--run-id", required=True)
    worker_parser.add_argument("--resume", default=None)

    doctor_parser = sub.add_parser("doctor", help="read-only preflight; changes nothing")
    doctor_parser.add_argument("--json", action="store_true", help="machine-readable report")
    doctor_parser.add_argument("--no-daemon-probe", action="store_true",
                               help="do not contact the daemon socket")

    install_parser = sub.add_parser("install", help="write the wrapper and LaunchAgent plist")
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument("--repo-root", default=None)

    return parser


_COMMANDS = {
    "rpc": _cmd_rpc,
    "health": _cmd_health,
    "daemon": _cmd_daemon,
    "worker": _cmd_worker,
    "version": _cmd_version,
    "doctor": _cmd_doctor,
    "install": _cmd_install,
}


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = build_parser().parse_args(argv)
    paths = config.paths_from_env()
    return _COMMANDS[args.command](args, paths)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
