"""plist generation and idempotent installation. Nothing is loaded here."""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hermes_claude_runner import config, launchd
from hermes_claude_runner.config import RunnerPaths

UV = shutil.which("uv")
requires_uv = pytest.mark.skipif(UV is None, reason="uv is not installed")


@pytest.fixture()
def install_paths(paths: RunnerPaths, tmp_path: Path) -> RunnerPaths:
    """Paths whose wrapper/plist targets live inside tmp_path."""
    return dataclasses.replace(
        paths,
        log_dir=tmp_path / "Logs",
        wrapper_path=tmp_path / "bin" / "hermes-claude-runner",
        plist_path=tmp_path / "LaunchAgents" / f"{config.DEFAULT_LAUNCH_AGENT_LABEL}.plist",
    )


def fake_uv(*, exit_code: int = 0, build: bool = True) -> tuple[Callable[..., Any], list[Any]]:
    """A stand-in for ``uv sync`` that records its argv and fakes a venv.

    ``build=False`` reproduces the nastier failure: uv reports success but the
    console script is not there, so the generation must never be activated.
    """
    calls: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = dict(kwargs.get("env") or {})
        calls.append({"argv": list(argv), "env": env})
        if exit_code == 0 and build:
            target = Path(env["UV_PROJECT_ENVIRONMENT"]) / "bin" / "hermes-claude-runner"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('#!/bin/sh\nprintf \'{"ok":true}\\n\'\n')
            target.chmod(0o755)
        return subprocess.CompletedProcess(argv, exit_code, "", "uv said no\n")

    return run, calls


#: Run in a child process: reports whether the install lock is held by anyone
#: else. Non-blocking on purpose — a test that waits on a lock cannot fail.
LOCK_PROBE = (
    "import fcntl, sys\n"
    "handle = open(sys.argv[1], 'a+')\n"
    "try:\n"
    "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "except OSError:\n"
    "    print('locked')\n"
    "else:\n"
    "    print('free')\n"
)


def probe_lock(paths: RunnerPaths) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", LOCK_PROBE, str(paths.runtime_dir / launchd.RUNTIME_LOCK_NAME)],
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def wiping_uv() -> tuple[Callable[..., Any], list[Any]]:
    """A failing ``uv sync`` that first empties the environment it targets.

    That is what a real one does with a virtualenv it decides to rebuild, and
    it is why provisioning must never be pointed at the live generation.
    """
    calls: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = dict(kwargs.get("env") or {})
        calls.append({"argv": list(argv), "env": env})
        target = Path(env["UV_PROJECT_ENVIRONMENT"])
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 1, "", "uv rebuilt the venv and failed\n")

    return run, calls


def make_checkout(path: Path) -> Path:
    """The smallest thing ``install`` accepts as a source checkout."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text('[project]\nname = "hermes-claude-runner"\n')
    return path


# ── plist ──────────────────────────────────────────────────────────────────

def test_plist_matches_the_brief(install_paths: RunnerPaths) -> None:
    parsed = plistlib.loads(launchd.plist_xml(install_paths).encode())
    assert parsed["Label"] == config.DEFAULT_LAUNCH_AGENT_LABEL
    assert parsed["ProgramArguments"] == [str(install_paths.wrapper_path), "daemon"]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["StandardOutPath"] == str(install_paths.log_dir / "stdout.log")
    assert parsed["StandardErrorPath"] == str(install_paths.log_dir / "stderr.log")


def test_plist_uses_only_absolute_paths(install_paths: RunnerPaths) -> None:
    parsed = plistlib.loads(launchd.plist_xml(install_paths).encode())
    for value in parsed["ProgramArguments"]:
        assert Path(value).is_absolute() or not value.startswith(".")
    assert Path(parsed["StandardOutPath"]).is_absolute()
    assert Path(parsed["WorkingDirectory"]).is_absolute()
    for entry in parsed["EnvironmentVariables"]["PATH"].split(":"):
        assert Path(entry).is_absolute()


def test_plist_path_covers_the_tools_the_daemon_needs(install_paths: RunnerPaths) -> None:
    # launchd hands the job a minimal PATH; git, claude and node must be findable.
    path = plistlib.loads(launchd.plist_xml(install_paths).encode())
    entries = path["EnvironmentVariables"]["PATH"].split(":")
    assert str(install_paths.home / ".local/bin") in entries
    assert "/usr/bin" in entries and "/bin" in entries
    assert "/opt/homebrew/bin" in entries


def test_plist_never_carries_a_credential(install_paths: RunnerPaths) -> None:
    parsed = plistlib.loads(launchd.plist_xml(install_paths).encode())
    env = parsed["EnvironmentVariables"]
    assert not any("KEY" in key or "TOKEN" in key or "SECRET" in key for key in env)


def test_plist_forwards_the_runner_roots(install_paths: RunnerPaths) -> None:
    env = plistlib.loads(launchd.plist_xml(install_paths).encode())["EnvironmentVariables"]
    assert env[config.ENV_HOME] == str(install_paths.data_dir)
    assert env[config.ENV_PROJECTS_ROOT] == str(install_paths.projects_root)
    # Every root the config can override is spelled out: launchd hands the job
    # a minimal environment, so an unforwarded root would silently fall back to
    # a different account's default.
    assert env[config.ENV_RUNTIME] == str(install_paths.runtime_dir)


# ── the managed runtime lives outside every checkout ───────────────────────

def test_the_runtime_is_a_managed_path_under_the_data_dir(install_paths: RunnerPaths) -> None:
    """Nothing about the runtime may be derived from a source tree."""
    assert install_paths.runtime_dir == install_paths.data_dir / config.RUNTIME_DIR_NAME
    assert install_paths.runtime_link == install_paths.runtime_dir / "current"
    assert install_paths.runtime_entrypoint == (
        install_paths.runtime_link / "bin" / "hermes-claude-runner"
    )
    assert launchd.default_repo_root() not in install_paths.runtime_dir.parents


def test_the_runtime_root_is_overridable(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    paths = config.paths_from_env({config.ENV_RUNTIME: str(elsewhere)}, home=tmp_path / "home")
    assert paths.runtime_dir == elsewhere
    assert paths.runtime_entrypoint == elsewhere / "current" / "bin" / "hermes-claude-runner"


# ── wrapper ────────────────────────────────────────────────────────────────

def test_wrapper_execs_the_installed_runtime_entrypoint(install_paths: RunnerPaths) -> None:
    script = launchd.wrapper_script(install_paths)
    assert script.startswith("#!/bin/sh")
    assert f'TARGET="{install_paths.runtime_entrypoint}"' in script
    assert 'exec "$TARGET" "$@"' in script
    assert "uv run" not in script, "uv sync noise must never reach rpc stdout"


def test_wrapper_names_no_checkout_and_no_project_venv(install_paths: RunnerPaths) -> None:
    """The whole point: deleting the checkout must not disarm the service."""
    script = launchd.wrapper_script(install_paths)
    assert "/.venv/" not in script
    assert str(launchd.default_repo_root()) not in script


# ── install ────────────────────────────────────────────────────────────────

def test_install_writes_wrapper_plist_and_logs(install_paths: RunnerPaths, tmp_path) -> None:
    report = launchd.install(install_paths, repo_root=tmp_path / "repo")

    assert install_paths.wrapper_path.exists()
    assert install_paths.plist_path.exists()
    assert install_paths.log_dir.is_dir()
    assert report["wrapper"]["action"] == "created"
    assert report["plist"]["action"] == "created"
    mode = install_paths.wrapper_path.stat().st_mode
    assert mode & stat.S_IXUSR


def test_install_is_idempotent(install_paths: RunnerPaths, tmp_path) -> None:
    launchd.install(install_paths, repo_root=tmp_path / "repo")
    second = launchd.install(install_paths, repo_root=tmp_path / "repo")

    assert second["wrapper"]["action"] == "unchanged"
    assert second["plist"]["action"] == "unchanged"
    assert list(install_paths.plist_path.parent.glob("*.bak-*")) == []


def test_install_backs_up_before_replacing(install_paths: RunnerPaths, tmp_path) -> None:
    install_paths.plist_path.parent.mkdir(parents=True, exist_ok=True)
    install_paths.plist_path.write_text("<plist>old</plist>")
    install_paths.wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    install_paths.wrapper_path.write_text("#!/bin/sh\necho old\n")

    report = launchd.install(install_paths, repo_root=tmp_path / "repo")

    assert report["plist"]["action"] == "replaced"
    backup = Path(report["plist"]["backup"])
    assert backup.read_text() == "<plist>old</plist>"
    assert ".bak-" in backup.name and backup.name.endswith("Z")
    assert Path(report["wrapper"]["backup"]).read_text() == "#!/bin/sh\necho old\n"


def test_install_dry_run_changes_nothing(install_paths: RunnerPaths, tmp_path) -> None:
    report = launchd.install(install_paths, repo_root=tmp_path / "repo", dry_run=True)
    assert not install_paths.wrapper_path.exists()
    assert not install_paths.plist_path.exists()
    assert report["dry_run"] is True
    assert report["plist"]["action"] == "would_create"


def test_install_report_lists_the_commands_still_to_run(
    install_paths: RunnerPaths, tmp_path
) -> None:
    report = launchd.install(install_paths, repo_root=tmp_path / "repo", dry_run=True)
    joined = " ".join(report["next_steps"])
    assert "launchctl" in joined
    assert str(install_paths.plist_path) in joined


def test_backup_names_are_timestamped_and_sortable(tmp_path: Path) -> None:
    target = tmp_path / "thing.plist"
    first = launchd.backup_path(target, when=1_700_000_000.0)
    second = launchd.backup_path(target, when=1_700_000_060.0)
    assert first != second
    assert sorted([second.name, first.name])[0] == first.name


def test_uninstall_instructions_preserve_state(install_paths: RunnerPaths) -> None:
    text = launchd.uninstall_instructions(install_paths)
    assert "launchctl bootout" in text or "launchctl unload" in text
    assert str(install_paths.plist_path) in text
    assert str(install_paths.data_dir) in text
    assert str(install_paths.runtime_dir) in text
    assert "preserved" in text.lower()
    assert "rm -rf" not in text


# ── provisioning the managed runtime ───────────────────────────────────────

def test_install_provisions_a_generation_from_the_checkout(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, calls = fake_uv()

    report = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)

    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == "uv" and argv[1] == "sync"
    assert "--no-editable" in argv, "an editable install would point back at the checkout"
    assert "--frozen" in argv, "installing must not rewrite the checkout's lockfile"
    assert "--project" in argv and str(checkout) in argv
    generation = Path(calls[0]["env"]["UV_PROJECT_ENVIRONMENT"])
    assert generation.parent == install_paths.runtime_versions_dir
    assert report["runtime"]["action"] == "created"
    assert report["runtime"]["generation"] == str(generation)


def test_provisioning_does_not_inherit_the_callers_virtualenv(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """The installer runs under `uv run`, whose VIRTUAL_ENV is the checkout's."""
    checkout = make_checkout(tmp_path / "checkout")
    run, calls = fake_uv()

    launchd.install(install_paths, repo_root=checkout, run=run,
                    env={"VIRTUAL_ENV": str(checkout / ".venv"), "PATH": "/usr/bin"})

    assert "VIRTUAL_ENV" not in calls[0]["env"]
    assert calls[0]["env"]["PATH"] == "/usr/bin"


def test_install_points_current_at_the_new_generation(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()

    launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)

    link = install_paths.runtime_link
    assert link.is_symlink()
    assert os.access(install_paths.runtime_entrypoint, os.X_OK)
    assert not link.readlink().is_absolute(), "a relative target keeps the runtime movable"


def test_install_never_writes_a_checkout_path_into_the_service(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()

    launchd.install(install_paths, repo_root=checkout, run=run)

    for artefact in (install_paths.wrapper_path, install_paths.plist_path):
        text = artefact.read_text()
        assert str(checkout) not in text, f"{artefact.name} pins the service to the checkout"
        assert "/.venv/" not in text


def test_install_keeps_every_earlier_generation_for_rollback(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()

    first = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)
    second = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_060.0)

    old = Path(first["runtime"]["generation"])
    new = Path(second["runtime"]["generation"])
    assert old != new
    assert old.is_dir(), "the superseded runtime is the backup; it must survive"
    assert second["runtime"]["previous"] == str(old)
    assert install_paths.runtime_link.resolve() == new.resolve()
    assert [str(old), str(new)] == second["runtime"]["generations"]


def test_rollback_repoints_current_at_the_previous_generation(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    first = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)
    launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_060.0)

    report = launchd.rollback_runtime(install_paths)

    assert report["action"] == "activated"
    assert report["generation"] == first["runtime"]["generation"]
    assert install_paths.runtime_link.resolve() == Path(first["runtime"]["generation"]).resolve()


def test_rollback_dry_run_moves_nothing(install_paths: RunnerPaths, tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)
    second = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_060.0)

    report = launchd.rollback_runtime(install_paths, dry_run=True)

    assert report["action"] == "would_activate"
    assert install_paths.runtime_link.resolve() == Path(second["runtime"]["generation"]).resolve()


def test_rollback_without_an_earlier_generation_refuses(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    installed = launchd.install(install_paths, repo_root=checkout, run=run)

    report = launchd.rollback_runtime(install_paths)

    assert report["action"] == "no_previous_generation"
    assert install_paths.runtime_link.resolve() == Path(
        installed["runtime"]["generation"]
    ).resolve()


@pytest.mark.parametrize("failure", [{"exit_code": 1}, {"build": False}])
def test_a_failed_provision_leaves_the_running_install_untouched(
    install_paths: RunnerPaths, tmp_path: Path, failure: dict[str, Any]
) -> None:
    """An upgrade that cannot build must not take the working runtime with it."""
    checkout = make_checkout(tmp_path / "checkout")
    good, _ = fake_uv()
    first = launchd.install(install_paths, repo_root=checkout, run=good, when=1_700_000_000.0)
    wrapper_before = install_paths.wrapper_path.read_text()

    broken, _ = fake_uv(**failure)
    with pytest.raises(RuntimeError, match="runtime"):
        launchd.install(install_paths, repo_root=checkout, run=broken, when=1_700_000_060.0)

    assert install_paths.runtime_link.resolve() == Path(
        first["runtime"]["generation"]
    ).resolve()
    assert os.access(install_paths.runtime_entrypoint, os.X_OK)
    assert install_paths.wrapper_path.read_text() == wrapper_before


def test_a_failed_provision_leaves_no_activatable_half_built_generation(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    broken, _ = fake_uv(build=False)

    with pytest.raises(RuntimeError):
        launchd.install(install_paths, repo_root=checkout, run=broken, when=1_700_000_000.0)

    assert launchd.runtime_generations(install_paths) == []


def test_install_skips_provisioning_when_the_source_is_not_a_checkout(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """`install` also runs *from* the managed runtime, where no source exists."""
    run, calls = fake_uv()

    report = launchd.install(install_paths, repo_root=tmp_path / "gone", run=run)

    assert calls == []
    assert report["runtime"]["action"] == "skipped"
    assert "checkout" in report["runtime"]["reason"]
    assert install_paths.wrapper_path.exists(), "the service is still installable"


def test_install_dry_run_provisions_nothing(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, calls = fake_uv()

    report = launchd.install(install_paths, repo_root=checkout, run=run, dry_run=True)

    assert calls == []
    assert not install_paths.runtime_dir.exists()
    assert report["runtime"]["action"] == "would_provision"


def test_the_generation_carries_a_manifest_naming_its_source(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()

    report = launchd.install(install_paths, repo_root=checkout, run=run)

    manifest = Path(report["runtime"]["generation"]) / launchd.RUNTIME_MANIFEST_NAME
    recorded = json.loads(manifest.read_text())
    assert recorded["source"] == str(checkout)
    assert recorded["label"] == install_paths.launch_agent_label
    assert recorded["installed_at"].endswith("Z")


# ── one generation per install, whatever the clock says ────────────────────

def test_a_same_second_reinstall_allocates_a_distinct_generation(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """Two installs within one second are two installs, not one."""
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()

    first = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)
    second = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)

    old = Path(first["runtime"]["generation"])
    new = Path(second["runtime"]["generation"])
    assert old != new
    assert old.is_dir(), "the superseded runtime is the backup; it must survive"
    assert second["runtime"]["previous"] == str(old)
    assert second["runtime"]["generations"] == [str(old), str(new)], "order must stay sortable"
    assert install_paths.runtime_link.resolve() == new.resolve()


def test_a_same_second_reinstall_can_still_be_rolled_back(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    first = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)
    launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)

    report = launchd.rollback_runtime(install_paths)

    assert report["action"] == "activated"
    assert report["generation"] == first["runtime"]["generation"]


def test_provisioning_never_syncs_into_the_live_generation(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """The failure the second hand used to cause: a rebuild of the live venv."""
    checkout = make_checkout(tmp_path / "checkout")
    good, _ = fake_uv()
    first = launchd.install(install_paths, repo_root=checkout, run=good, when=1_700_000_000.0)
    live = Path(first["runtime"]["generation"])

    wiping, calls = wiping_uv()
    with pytest.raises(RuntimeError, match="runtime"):
        launchd.install(install_paths, repo_root=checkout, run=wiping, when=1_700_000_000.0)

    assert Path(calls[0]["env"]["UV_PROJECT_ENVIRONMENT"]) != live
    assert os.access(install_paths.runtime_entrypoint, os.X_OK), (
        "a failed reinstall took the running runtime with it"
    )
    assert install_paths.runtime_link.resolve() == live.resolve()
    assert launchd.runtime_generations(install_paths) == [live]


def test_provisioning_refuses_a_generation_that_is_already_live(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """Belt and braces: the allocator's guarantee, asserted directly."""
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    first = launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)
    live = Path(first["runtime"]["generation"])

    allocated = launchd.allocate_generation(install_paths, when=1_700_000_000.0)

    assert allocated != live
    assert not any(allocated.iterdir()), "a generation is provisioned into empty, or not at all"


# ── two installers at once ─────────────────────────────────────────────────

def test_the_install_lock_is_exclusive_across_processes(install_paths: RunnerPaths) -> None:
    with launchd.install_lock(install_paths):
        assert probe_lock(install_paths) == "locked"
    assert probe_lock(install_paths) == "free"


def test_provisioning_and_activation_hold_the_install_lock(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """Observed from inside the sync, which is between allocate and activate."""
    checkout = make_checkout(tmp_path / "checkout")
    base, _ = fake_uv()
    observed: list[str] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.append(probe_lock(install_paths))
        return base(argv, **kwargs)

    launchd.install(install_paths, repo_root=checkout, run=run)

    assert observed == ["locked"]
    assert probe_lock(install_paths) == "free", "the lock must not outlive the install"


def test_rollback_holds_the_install_lock(install_paths: RunnerPaths, tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)
    launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_060.0)

    launchd.rollback_runtime(install_paths)

    assert probe_lock(install_paths) == "free"


def test_activation_never_touches_another_installers_staging_entry(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """A shared staging name lets one installer unlink another's half-swap."""
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_000.0)

    link = install_paths.runtime_link
    foreign = link.with_name(f".{link.name}.tmp")
    foreign.write_text("another installer is mid-swap")

    launchd.install(install_paths, repo_root=checkout, run=run, when=1_700_000_060.0)

    assert foreign.read_text() == "another installer is mid-swap"


def test_a_finished_install_leaves_no_staging_entry_of_its_own(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    launchd.install(install_paths, repo_root=checkout, run=run)

    leftovers = list(install_paths.runtime_dir.glob(".current.tmp*"))
    assert leftovers == [], leftovers
    assert list(install_paths.wrapper_path.parent.glob(".*.tmp*")) == []


# ── only a generation this install owns may be activated ───────────────────

def usable_generation(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    entrypoint = path / "bin" / "hermes-claude-runner"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text('#!/bin/sh\nprintf \'{"ok":true}\\n\'\n')
    entrypoint.chmod(0o755)
    return path


def test_activating_a_generation_outside_the_versions_root_is_refused(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """It would link versions/<basename> and leave `current` dangling."""
    outside = usable_generation(tmp_path / "somewhere-else")

    with pytest.raises(ValueError, match="direct child"):
        launchd.activate_generation(install_paths, outside)

    assert not install_paths.runtime_link.is_symlink()
    assert not install_paths.runtime_link.exists()


def test_activating_a_nested_generation_is_refused(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    nested = usable_generation(install_paths.runtime_versions_dir / "group" / "20260101T000000Z")

    with pytest.raises(ValueError, match="direct child"):
        launchd.activate_generation(install_paths, nested)

    assert not install_paths.runtime_link.is_symlink()


def test_a_refused_activation_leaves_the_live_generation_running(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    run, _ = fake_uv()
    installed = launchd.install(install_paths, repo_root=checkout, run=run)
    live = Path(installed["runtime"]["generation"])

    with pytest.raises(ValueError):
        launchd.activate_generation(install_paths, usable_generation(tmp_path / "elsewhere"))

    assert install_paths.runtime_link.resolve() == live.resolve()
    assert os.access(install_paths.runtime_entrypoint, os.X_OK)


def test_activation_links_the_generation_it_validated(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    generation = usable_generation(install_paths.runtime_versions_dir / "20260101T000000Z")

    launchd.activate_generation(install_paths, generation)

    assert install_paths.runtime_link.readlink() == Path("versions") / "20260101T000000Z"
    assert install_paths.runtime_entrypoint.resolve().is_file()


# ── the installed entrypoint outlives the checkout ─────────────────────────

def stand_in_checkout(path: Path) -> Path:
    """A tiny real project that installs a ``hermes-claude-runner`` script.

    It stands in for this repository so the test proves the mechanism without
    resolving this package's dependencies over the network.
    """
    package = path / "src" / "hermes_claude_runner_standin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "def main() -> int:\n"
        "    print('{\"ok\": true, \"result\": {\"status\": \"ok\"}}')\n"
        "    return 0\n"
    )
    floor = f"{sys.version_info.major}.{sys.version_info.minor}"
    (path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "hermes-claude-runner-standin"\n'
        'version = "0.0.0"\n'
        f'requires-python = ">={floor}"\n'
        "[project.scripts]\n"
        'hermes-claude-runner = "hermes_claude_runner_standin:main"\n'
        "[build-system]\n"
        'requires = ["uv_build>=0.11.24,<0.12.0"]\n'
        'build-backend = "uv_build"\n'
    )
    # Provisioning runs `uv sync --frozen`, so the source must arrive locked —
    # exactly as this repository does, where the installer locks before it
    # provisions.
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["uv", "lock", "--project", str(path)],
        capture_output=True, text=True, check=True, timeout=300,
    )
    return path


@requires_uv
def test_the_installed_entrypoint_survives_the_checkout_being_renamed_and_deleted(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """The lane's whole claim, executed: install, lose the source, still run."""
    checkout = stand_in_checkout(tmp_path / "checkout")

    launchd.install(install_paths, repo_root=checkout, runtime_python=sys.executable)

    moved = tmp_path / "moved-away"
    checkout.rename(moved)
    shutil.rmtree(moved)
    assert not checkout.exists()

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(install_paths.wrapper_path), "health"],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["ok"] is True


@requires_uv
def test_the_launch_agent_target_still_resolves_without_the_checkout(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    checkout = stand_in_checkout(tmp_path / "checkout")
    launchd.install(install_paths, repo_root=checkout, runtime_python=sys.executable)
    shutil.rmtree(checkout)

    program = plistlib.loads(
        install_paths.plist_path.read_bytes()
    )["ProgramArguments"][0]
    assert os.access(program, os.X_OK), "launchd would fail to spawn the daemon"
    assert install_paths.runtime_entrypoint.resolve().is_file()


# ── respawn-loop containment ───────────────────────────────────────────────

def test_plist_throttles_respawns(install_paths: RunnerPaths) -> None:
    parsed = plistlib.loads(launchd.plist_xml(install_paths).encode())
    assert parsed["ThrottleInterval"] >= 10


def test_wrapper_reports_a_missing_install_instead_of_looping(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """A missing runtime must produce one clear JSON answer, not log noise."""
    wrapper = tmp_path / "hermes-claude-runner"
    wrapper.write_text(launchd.wrapper_script(install_paths))
    wrapper.chmod(0o755)

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(wrapper), "rpc"], capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode != 0
    envelope = json.loads(completed.stdout)
    assert envelope["ok"] is False
    assert envelope["error"] == "daemon_unavailable"
    assert "install" in envelope["detail"]


def test_wrapper_execs_a_present_install(install_paths: RunnerPaths, tmp_path: Path) -> None:
    entrypoint = install_paths.runtime_entrypoint
    generation = install_paths.runtime_versions_dir / "20260101T000000Z"
    (generation / "bin").mkdir(parents=True)
    (generation / "bin" / "hermes-claude-runner").write_text(
        '#!/bin/sh\necho "{\\"ok\\":true}"\n'
    )
    (generation / "bin" / "hermes-claude-runner").chmod(0o755)
    install_paths.runtime_link.symlink_to(Path("versions") / generation.name)
    assert entrypoint.is_file()

    wrapper = tmp_path / "wrapper"
    wrapper.write_text(launchd.wrapper_script(install_paths))
    wrapper.chmod(0o755)

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(wrapper), "rpc"], capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == '{"ok":true}'


# ── the label follows the install, not the module ──────────────────────────

def test_plist_and_instructions_use_the_installs_own_label(
    install_paths: RunnerPaths, tmp_path: Path
) -> None:
    """An install made under an older label must stay manageable."""
    import plistlib as pl

    old_label = "com.example.legacy-runner"  # invented: any overridden label must work
    legacy = dataclasses.replace(
        install_paths,
        launch_agent_label=old_label,
        plist_path=tmp_path / "LaunchAgents" / f"{old_label}.plist",
    )

    assert pl.loads(launchd.plist_xml(legacy).encode())["Label"] == old_label

    report = launchd.install(legacy, repo_root=tmp_path / "repo", dry_run=True)
    assert all(old_label in step for step in report["next_steps"]
               if "launchctl" in step and "gui/" in step)
    assert old_label in launchd.uninstall_instructions(legacy)
