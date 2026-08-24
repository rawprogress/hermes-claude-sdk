"""plist generation and idempotent installation. Nothing is loaded here."""

from __future__ import annotations

import dataclasses
import plistlib
import stat
from pathlib import Path

import pytest

from hermes_claude_runner import config, launchd
from hermes_claude_runner.config import RunnerPaths


@pytest.fixture()
def install_paths(paths: RunnerPaths, tmp_path: Path) -> RunnerPaths:
    """Paths whose wrapper/plist targets live inside tmp_path."""
    return dataclasses.replace(
        paths,
        log_dir=tmp_path / "Logs",
        wrapper_path=tmp_path / "bin" / "hermes-claude-runner",
        plist_path=tmp_path / "LaunchAgents" / f"{config.DEFAULT_LAUNCH_AGENT_LABEL}.plist",
    )


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


# ── wrapper ────────────────────────────────────────────────────────────────

def test_wrapper_execs_the_venv_console_script(tmp_path: Path) -> None:
    script = launchd.wrapper_script(tmp_path / "repo")
    console = tmp_path / "repo" / ".venv" / "bin" / "hermes-claude-runner"
    assert script.startswith("#!/bin/sh")
    assert f'TARGET="{console}"' in script
    assert 'exec "$TARGET" "$@"' in script
    assert "uv run" not in script, "uv sync noise must never reach rpc stdout"


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
    assert "preserved" in text.lower()
    assert "rm -rf" not in text


# ── respawn-loop containment ───────────────────────────────────────────────

def test_plist_throttles_respawns(install_paths: RunnerPaths) -> None:
    parsed = plistlib.loads(launchd.plist_xml(install_paths).encode())
    assert parsed["ThrottleInterval"] >= 10


def test_wrapper_reports_a_missing_install_instead_of_looping(tmp_path: Path) -> None:
    """A broken repo path must produce one clear JSON answer, not log noise."""
    import json
    import subprocess

    wrapper = tmp_path / "hermes-claude-runner"
    wrapper.write_text(launchd.wrapper_script(tmp_path / "not-installed"))
    wrapper.chmod(0o755)

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(wrapper), "rpc"], capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode != 0
    envelope = json.loads(completed.stdout)
    assert envelope["ok"] is False
    assert envelope["error"] == "daemon_unavailable"
    assert "install" in envelope["detail"]


def test_wrapper_execs_a_present_install(tmp_path: Path) -> None:
    import subprocess

    console = tmp_path / "repo" / ".venv" / "bin" / "hermes-claude-runner"
    console.parent.mkdir(parents=True)
    console.write_text('#!/bin/sh\necho "{\\"ok\\":true}"\n')
    console.chmod(0o755)

    wrapper = tmp_path / "wrapper"
    wrapper.write_text(launchd.wrapper_script(tmp_path / "repo"))
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
