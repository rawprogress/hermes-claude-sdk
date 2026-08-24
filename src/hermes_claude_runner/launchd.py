"""LaunchAgent plist generation and idempotent installation.

The installed service never depends on the source checkout. ``install``
provisions a *managed runtime* — a virtualenv holding a non-editable copy of
this package — under a stable path the config owns, points a ``current``
symlink at it, and wires the LaunchAgent to a wrapper that follows that
symlink. Moving, renaming or deleting the checkout afterwards changes nothing.

Installation only ever adds files, and always keeps a timestamped copy of
anything it replaces. Uninstalling never touches the database or worktrees.
"""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from . import __version__, config
from .config import RunnerPaths

#: Written into every generation. It records where the code came from, and it
#: is the proof that a generation finished provisioning: a generation without
#: it is half-built and is never activated.
RUNTIME_MANIFEST_NAME = "hermes-runtime.json"

#: Held for the whole critical section — allocate, sync, activate — so two
#: installers cannot interleave their generations or their symlink swaps.
RUNTIME_LOCK_NAME = "install.lock"

#: Provisioning resolves and downloads dependencies, so it gets minutes, not
#: seconds — but never forever, because the installer waits on it.
PROVISION_TIMEOUT_SECONDS = 900

#: Staging names carry it so that no two writers can pick the same one.
_STAGING_COUNTER = itertools.count()

#: Which lock files this thread already holds. ``flock`` is per open file
#: description, so re-opening the same lock inside a locked section would
#: deadlock against this very process.
_LOCK_STATE = threading.local()

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# launchd hands the job a minimal PATH, so the daemon needs an explicit one:
# git, node and the claude CLI all have to be findable from it.
_SYSTEM_PATH_ENTRIES = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def path_entries(home: Path) -> tuple[str, ...]:
    """PATH for the LaunchAgent, rooted in the installing account's home."""
    return (str(home / ".local/bin"), *_SYSTEM_PATH_ENTRIES)


def default_repo_root() -> Path:
    """Repository root of this checkout (``src/hermes_claude_runner/`` up two).

    Only ever a *source* for provisioning. Once installed, the runner runs
    from the managed runtime, where this points at a site-packages parent and
    :func:`is_source_checkout` rejects it.
    """
    return Path(__file__).resolve().parents[2]


def is_source_checkout(path: Path) -> bool:
    """Whether *path* is a tree the runtime can be provisioned from."""
    return (path / "pyproject.toml").is_file()


def wrapper_script(paths: RunnerPaths) -> str:
    """Shell wrapper installed at ``~/.local/bin/hermes-claude-runner``."""
    entrypoint = paths.runtime_entrypoint
    detail = (
        f"the runner runtime is not installed at {entrypoint}; "
        "re-run scripts/install_runner.sh on the Mac"
    )
    envelope = json.dumps({"ok": False, "error": "daemon_unavailable", "detail": detail})
    return (
        "#!/bin/sh\n"
        "# Managed by `hermes-claude-runner install` — regenerate rather than edit.\n"
        "# Execs the managed runtime's console script directly so nothing but\n"
        "# the JSON envelope can ever reach stdout. The path runs through the\n"
        "# `current` symlink, so a rollback takes effect without rewriting this.\n"
        f'TARGET="{entrypoint}"\n'
        'if [ ! -x "$TARGET" ]; then\n'
        "    # Answer Hermes in its own protocol instead of failing silently,\n"
        "    # and exit non-zero so launchd throttles instead of hot-looping.\n"
        f"    printf '%s\\n' {shlex.quote(envelope)}\n"
        "    exit 3\n"
        "fi\n"
        'exec "$TARGET" "$@"\n'
    )


def plist_definition(paths: RunnerPaths) -> dict[str, Any]:
    return {
        "Label": paths.launch_agent_label,
        "ProgramArguments": [str(paths.wrapper_path), "daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        # A broken install would otherwise respawn as fast as launchd allows;
        # the wrapper reports the problem and this keeps the noise bounded.
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "WorkingDirectory": str(paths.data_dir),
        "StandardOutPath": str(paths.log_dir / "stdout.log"),
        "StandardErrorPath": str(paths.log_dir / "stderr.log"),
        "EnvironmentVariables": {
            "PATH": ":".join(path_entries(paths.home)),
            "HOME": str(paths.home),
            config.ENV_HOME: str(paths.data_dir),
            config.ENV_PROJECTS_ROOT: str(paths.projects_root),
            config.ENV_WORKTREES_ROOT: str(paths.worktrees_root),
            config.ENV_SOCKET: str(paths.socket_path),
            config.ENV_LOG_DIR: str(paths.log_dir),
            config.ENV_RUNTIME: str(paths.runtime_dir),
            config.ENV_CLAUDE_CLI: str(paths.claude_cli_path),
        },
    }


def plist_xml(paths: RunnerPaths) -> str:
    return plistlib.dumps(plist_definition(paths)).decode()


def _stamp(when: float | None = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when if when is not None else time.time()))


def backup_path(target: Path, when: float | None = None) -> Path:
    return target.with_name(f"{target.name}.bak-{_stamp(when)}")


@contextlib.contextmanager
def install_lock(paths: RunnerPaths) -> Iterator[None]:
    """Serialize provisioning, activation and rollback across processes.

    Advisory (``flock``) and exclusive. Two installers racing would otherwise
    hand out the same generation name and overwrite each other's swap, and a
    rollback landing mid-install would activate a generation that is still
    being built.
    """
    lock_path = paths.runtime_dir / RUNTIME_LOCK_NAME
    held: dict[str, int] | None = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _LOCK_STATE.held = held
    key = str(lock_path)

    if held.get(key):
        # Re-entered from a function the holder itself called.
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _staging_path(target: Path) -> Path:
    """A staging name no concurrent writer can also be using.

    A shared name is worse than no staging at all: the second writer unlinks
    the first one's half-finished entry out from under it.
    """
    return target.with_name(f".{target.name}.tmp.{os.getpid()}.{next(_STAGING_COUNTER)}")


def _write_file(target: Path, content: str, *, executable: bool, dry_run: bool) -> dict[str, Any]:
    """Write *content* to *target*, backing up any differing existing file."""
    exists = target.exists()
    if exists and target.read_text() == content:
        return {"path": str(target), "action": "unchanged", "backup": None}

    action = "replace" if exists else "create"
    if dry_run:
        return {"path": str(target), "action": f"would_{action}", "backup": None}

    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if exists:
        backup = backup_path(target)
        shutil.copy2(target, backup)

    tmp = _staging_path(target)
    tmp.write_text(content)
    if executable:
        tmp.chmod(0o755)
    tmp.replace(target)
    return {
        "path": str(target),
        "action": "replaced" if exists else "created",
        "backup": str(backup) if backup else None,
    }


# ── the managed runtime ────────────────────────────────────────────────────

def runtime_generations(paths: RunnerPaths) -> list[Path]:
    """Finished generations, oldest first.

    Only manifest-bearing directories count, so a generation whose
    provisioning died half-way can never be activated or rolled back to.
    """
    versions = paths.runtime_versions_dir
    if not versions.is_dir():
        return []
    return sorted(
        (child for child in versions.iterdir()
         if child.is_dir() and (child / RUNTIME_MANIFEST_NAME).is_file()),
        key=lambda child: child.name,
    )


def current_generation(paths: RunnerPaths) -> Path | None:
    """The generation the wrapper currently runs, if the link resolves."""
    link = paths.runtime_link
    if not link.is_symlink():
        return None
    target = link.parent / link.readlink()
    return target.resolve() if target.exists() else None


def allocate_generation(paths: RunnerPaths, *, when: float | None = None) -> Path:
    """Create and return an empty directory for the next generation.

    Unique by construction. The stamp only has one-second resolution, so a
    reinstall in the same second would otherwise be handed the directory the
    running daemon executes from — and a ``uv sync`` that rebuilds a venv it
    then fails on would take the live entrypoint with it.
    """
    versions = paths.runtime_versions_dir
    versions.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(when)
    # Zero-padded so generation names keep sorting chronologically.
    for suffix in ("", *(f"-{n:02d}" for n in range(2, 100))):
        candidate = versions / f"{stamp}{suffix}"
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(
        f"refusing to provision: {versions} already holds every generation name for {stamp}"
    )


def _validated_generation(paths: RunnerPaths, generation: Path | str) -> Path:
    """The generation, proven to be one this installation owns.

    The link is written relative to the runtime directory, so a generation
    from anywhere else would be linked by basename alone and leave ``current``
    dangling — an activation that reports success and breaks the service.
    """
    versions = paths.runtime_versions_dir.resolve()
    resolved = Path(generation).resolve()
    if resolved.parent != versions:
        raise ValueError(
            f"refusing to activate {generation}: a runtime generation must be a "
            f"direct child of {paths.runtime_versions_dir}"
        )
    return resolved


def _provision_command(repo_root: Path, python: str | None) -> list[str]:
    """The uv invocation that fills a generation from a source checkout.

    ``--no-editable`` is the load-bearing flag: an editable install would put
    the checkout's path back into the runtime's site-packages, which is the
    dependency this whole module exists to remove.
    """
    argv = [
        "uv", "sync",
        "--frozen",      # install what the checkout locked; never re-resolve here
        "--no-editable",
        "--no-dev",
        "--project", str(repo_root),
    ]
    if python:
        argv += ["--python", python]
    return argv


def _provision_env(generation: Path, env: Mapping[str, str] | None) -> dict[str, str]:
    child = dict(os.environ if env is None else env)
    # The installer itself runs under `uv run`, whose VIRTUAL_ENV names the
    # checkout's .venv. Left in place, uv would sync that one instead.
    child.pop("VIRTUAL_ENV", None)
    child["UV_PROJECT_ENVIRONMENT"] = str(generation)
    return child


def activate_generation(
    paths: RunnerPaths, generation: Path, *, dry_run: bool = False
) -> dict[str, Any]:
    """Point ``current`` at *generation* with a single atomic rename.

    Fails closed: the generation is validated before anything is written, so a
    refusal leaves whatever was running exactly where it was.
    """
    resolved = _validated_generation(paths, generation)
    entrypoint = resolved / "bin" / "hermes-claude-runner"
    if not (entrypoint.is_file() and os.access(entrypoint, os.X_OK)):
        raise RuntimeError(
            f"refusing to activate the runtime generation {resolved}: "
            f"{entrypoint} is missing or not executable"
        )

    with install_lock(paths):
        previous = current_generation(paths)
        if previous is not None and previous == resolved:
            return {"action": "unchanged", "generation": str(resolved),
                    "previous": str(previous)}
        if dry_run:
            return {"action": "would_activate", "generation": str(resolved),
                    "previous": str(previous) if previous else None}

        link = paths.runtime_link
        link.parent.mkdir(parents=True, exist_ok=True)
        staging = _staging_path(link)
        # Relative, and built from the validated name, so the runtime stays
        # movable as a unit and can never point outside itself.
        staging.symlink_to(Path(config.RUNTIME_GENERATIONS_DIR_NAME) / resolved.name)
        try:
            os.replace(staging, link)
        except OSError:
            staging.unlink(missing_ok=True)
            raise
        return {"action": "activated", "generation": str(resolved),
                "previous": str(previous) if previous else None}


def rollback_runtime(paths: RunnerPaths, *, dry_run: bool = False) -> dict[str, Any]:
    """Go back to the newest generation that is not the live one.

    Nothing is deleted: rollback is a symlink swap, so it is reversible by
    running it again against the generation it came from.
    """
    with install_lock(paths):
        generations = runtime_generations(paths)
        live = current_generation(paths)
        earlier = [gen for gen in generations if live is None or gen.resolve() != live]
        if not earlier:
            return {
                "action": "no_previous_generation",
                "generation": str(live) if live else None,
                "previous": None,
                "generations": [str(gen) for gen in generations],
            }
        report = activate_generation(paths, earlier[-1], dry_run=dry_run)
        report["generations"] = [str(gen) for gen in generations]
        return report


def provision_runtime(
    paths: RunnerPaths,
    repo_root: Path,
    *,
    dry_run: bool = False,
    when: float | None = None,
    python: str | None = None,
    run: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Install this package into a fresh generation and make it current.

    The order matters and is the safety property: sync, verify, write the
    manifest, then swap the symlink. A failure at any step raises with the
    live runtime still in place, because nothing before the swap touches it.
    """
    report: dict[str, Any] = {
        "path": str(paths.runtime_dir),
        "entrypoint": str(paths.runtime_entrypoint),
        "source": str(repo_root),
        "generation": None,
        "previous": None,
        "generations": [],
    }

    if not is_source_checkout(repo_root):
        # `install` also runs from the managed runtime, where there is no
        # source tree to build from. Leaving the runtime alone is correct.
        report["action"] = "skipped"
        report["reason"] = f"{repo_root} is not a source checkout (no pyproject.toml)"
        return report

    if dry_run:
        report["action"] = "would_provision"
        report["generations"] = [str(gen) for gen in runtime_generations(paths)]
        return report

    with install_lock(paths):
        return _provision_locked(paths, repo_root, report, when, python, run, env)


def _provision_locked(
    paths: RunnerPaths,
    repo_root: Path,
    report: dict[str, Any],
    when: float | None,
    python: str | None,
    run: Runner,
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    """The critical section of :func:`provision_runtime`, lock already held."""
    generation = allocate_generation(paths, when=when)
    live = current_generation(paths)
    if live is not None and generation.resolve() == live:
        # Unreachable while allocation hands out fresh directories, and
        # spelled out anyway: syncing into the running runtime is the one
        # thing that can break an install that was working a second ago.
        raise RuntimeError(
            f"refusing to provision into {generation}: it is the live runtime generation"
        )

    completed = run(
        _provision_command(repo_root, python),
        env=_provision_env(generation, env),
        capture_output=True,
        text=True,
        timeout=PROVISION_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"provisioning the managed runtime in {generation} failed "
            f"(uv exited {completed.returncode}): {(completed.stderr or '').strip()[-800:]}"
        )

    entrypoint = generation / "bin" / "hermes-claude-runner"
    if not (entrypoint.is_file() and os.access(entrypoint, os.X_OK)):
        raise RuntimeError(
            f"the managed runtime in {generation} has no executable "
            f"{entrypoint.name}; the previous runtime is untouched"
        )

    (generation / RUNTIME_MANIFEST_NAME).write_text(json.dumps({
        "label": paths.launch_agent_label,
        "runner_version": __version__,
        "source": str(repo_root),
        "installed_at": _stamp(when),
        "entrypoint": str(entrypoint),
    }, indent=2) + "\n")

    activation = activate_generation(paths, generation)
    report.update({
        "action": "created",
        "generation": str(generation),
        "previous": activation["previous"],
        "generations": [str(gen) for gen in runtime_generations(paths)],
    })
    return report


def install(
    paths: RunnerPaths,
    *,
    dry_run: bool = False,
    repo_root: Path | str | None = None,
    runtime_python: str | None = None,
    when: float | None = None,
    run: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Provision the runtime, then write the wrapper and plist. Idempotent.

    The runtime is provisioned first: if it fails, the wrapper and the
    LaunchAgent still point at the generation that was working a moment ago.
    A real install holds the install lock from the first allocation to the
    last file, so a second installer waits rather than interleaves.
    """
    root = Path(repo_root) if repo_root else default_repo_root()
    if not dry_run:
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        paths.data_dir.mkdir(parents=True, exist_ok=True)

    # A dry run reads; it must not create the runtime directory the lock
    # file would live in.
    guard: contextlib.AbstractContextManager[None] = (
        contextlib.nullcontext() if dry_run else install_lock(paths)
    )
    with guard:
        runtime = provision_runtime(
            paths, root, dry_run=dry_run, when=when, python=runtime_python, run=run, env=env,
        )
        wrapper = _write_file(paths.wrapper_path, wrapper_script(paths),
                              executable=True, dry_run=dry_run)
        plist = _write_file(paths.plist_path, plist_xml(paths), executable=False,
                            dry_run=dry_run)

    label = paths.launch_agent_label

    return {
        "dry_run": dry_run,
        "repo_root": str(root),
        "runtime": runtime,
        "wrapper": wrapper,
        "plist": plist,
        "log_dir": str(paths.log_dir),
        "label": paths.launch_agent_label,
        "next_steps": [
            f"launchctl bootout gui/$(id -u)/{label} 2>/dev/null || true",
            f"launchctl bootstrap gui/$(id -u) {paths.plist_path}",
            f"launchctl kickstart -k gui/$(id -u)/{label}",
            f"launchctl print gui/$(id -u)/{label} | head -20",
            f"{paths.wrapper_path} health",
        ],
        # Every generation is kept, so going back is a symlink swap away.
        "rollback": [
            f"ln -sfn {config.RUNTIME_GENERATIONS_DIR_NAME}/<generation> {paths.runtime_link}",
            f"launchctl kickstart -k gui/$(id -u)/{label}",
        ],
    }


def uninstall_instructions(paths: RunnerPaths) -> str:
    """Steps that stop the service while preserving every artefact."""
    return "\n".join([
        "# Stop and remove the LaunchAgent (state is preserved).",
        f"launchctl bootout gui/$(id -u)/{paths.launch_agent_label} 2>/dev/null || true",
        f"mv {paths.plist_path} {paths.plist_path}.removed",
        f"mv {paths.wrapper_path} {paths.wrapper_path}.removed",
        "",
        "# Deliberately preserved — delete by hand only if you really mean it:",
        f"#   database : {paths.db_path}",
        f"#   worktrees: {paths.worktrees_root}",
        f"#   logs     : {paths.log_dir}",
        f"#   data dir : {paths.data_dir}",
        f"#   runtime  : {paths.runtime_dir}",
    ])
