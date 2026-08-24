"""The install/uninstall scripts: present, parseable, portable, non-destructive.

The behavioural tests run the real installer against stub ``uv``/``launchctl``
binaries and a throwaway ``HOME``, so idempotency and ``--dry-run`` are proven
rather than asserted from a reading of the source.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from hermes_claude_runner import config

# Invented, not historical: the scripts must hardcode nobody's account and
# nobody's namespace, so the needles only have to have the right shape.
INVENTED_LABEL = "com.example.legacy-runner"

# A bootstrap that keeps failing however long the installer is willing to wait.
BOOTSTRAP_NEVER_SETTLES = 10**6

# The slowest teardown seen on a real Mac released the label only in time for
# the sixth bootstrap. The installer has to clear that rung *and* keep attempts
# behind it, or the next machine that is a little slower has no reserve left.
OBSERVED_SLOWEST_BOOTSTRAP_ATTEMPT = 6

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
NAMES = ("install_runner.sh", "install_plugin_on_surface.sh", "uninstall.sh")


@pytest.mark.parametrize("name", NAMES)
def test_script_exists_and_is_executable(name: str) -> None:
    path = SCRIPTS / name
    assert path.is_file(), name
    assert os.access(path, os.X_OK), name


@pytest.mark.parametrize("name", NAMES)
def test_script_parses(name: str) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["/bin/sh", "-n", str(SCRIPTS / name)], capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("name", NAMES)
def test_no_script_destroys_state(name: str) -> None:
    text = (SCRIPTS / name).read_text()
    for forbidden in ("rm -rf $HOME", "git reset", "git clean", "git stash",
                      "rm -rf ~/Projects", "--delete"):
        assert forbidden not in text, f"{name} contains {forbidden!r}"


@pytest.mark.parametrize("name", NAMES)
def test_no_script_hardcodes_one_persons_machine(name: str) -> None:
    """Any absolute home in a script is somebody's; scripts derive theirs."""
    text = (SCRIPTS / name).read_text()
    homes = re.findall("/Users" + r"/([A-Za-z0-9._-]+)", text)
    assert homes == [], f"{name} hardcodes the home directory of {homes}"


@pytest.mark.parametrize("name", NAMES)
def test_no_script_pins_the_service_to_a_checkout_venv(name: str) -> None:
    """The installed service runs from the managed runtime, never from .venv.

    A checkout's virtualenv disappears when the checkout is moved or deleted,
    and an install that depends on one is a service with a hidden expiry date.
    """
    text = (SCRIPTS / name).read_text()
    assert ".venv" not in text, f"{name} wires something to a checkout virtualenv"


@pytest.mark.parametrize("name", ("install_runner.sh", "uninstall.sh"))
def test_label_default_matches_the_package(name: str) -> None:
    """One source of truth: the scripts and the package must agree."""
    text = (SCRIPTS / name).read_text()
    assert f'LABEL="${{{config.ENV_LABEL}:-{config.DEFAULT_LAUNCH_AGENT_LABEL}}}"' in text


def test_uninstall_preserves_state_and_says_so() -> None:
    text = (SCRIPTS / "uninstall.sh").read_text()
    assert "Preserved on purpose" in text
    assert "data.db" in text
    assert ".hermes-claude-worktrees" in text


def test_runner_install_verifies_before_installing() -> None:
    text = (SCRIPTS / "install_runner.sh").read_text()
    order = [text.index("pytest -q"), text.index("ruff check"),
             text.index("hermes-claude-runner install")]
    assert order == sorted(order), "tests and lint must run before installing"
    assert "launchctl bootstrap" in text
    assert "health" in text


def test_surface_installer_creates_the_target_before_copying() -> None:
    text = (SCRIPTS / "install_plugin_on_surface.sh").read_text()
    mkdir_target = text.index('mkdir -p "$TARGET"')
    scp_call = text.index("scp -r")
    assert mkdir_target < scp_call, "scp into a missing directory would flatten the copy"


def test_surface_installer_keeps_the_remote_tilde_for_the_remote_shell() -> None:
    """The local shell must not expand ``~`` before ssh sees it."""
    text = (SCRIPTS / "install_plugin_on_surface.sh").read_text()
    assert "REMOTE_CMD='~/.local/bin/hermes-claude-runner'" in text, (
        "single quotes keep the tilde intact for the Mac's login shell"
    )


# ── the installer, actually executed ───────────────────────────────────────

def _sandbox(
    tmp_path: Path, *, health_fails: bool = False, bootstrap_failures: int = 0,
) -> tuple[dict[str, str], Path]:
    """A PATH of recording stubs plus a throwaway HOME.

    ``bootstrap_failures`` makes that many leading ``launchctl bootstrap``
    calls fail the way a teardown that has not settled yet does, so a race
    the installer has to survive can be replayed without a real launchd.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    report = json.dumps({"dry_run": False, "wrapper": {"action": "created"},
                         "plist": {"action": "created"}, "next_steps": []})
    (bin_dir / "uv").write_text(
        "#!/bin/sh\n"
        f'printf "uv %s\\n" "$*" >> {log}\n'
        'case "$*" in\n'
        f"  *' install'*) printf '%s\\n' {shlex.quote(report)} ;;\n"
        "esac\n"
        "exit 0\n"
    )
    # launchd answers a bootstrap that lands on a label the previous service
    # has not finished releasing with exit 5, on stderr, in these words.
    (bin_dir / "launchctl").write_text(
        "#!/bin/sh\n"
        f'printf "launchctl %s\\n" "$*" >> {shlex.quote(str(log))}\n'
        'if [ "$1" = bootstrap ]; then\n'
        f'  tries=$(grep -c "^launchctl bootstrap" {shlex.quote(str(log))})\n'
        f'  if [ "$tries" -le {bootstrap_failures} ]; then\n'
        "    printf 'Bootstrap failed: 5: Input/output error\\n' >&2\n"
        "    exit 5\n"
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )
    for stub in ("uv", "launchctl"):
        (bin_dir / stub).chmod(0o755)

    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    wrapper = home / ".local" / "bin" / "hermes-claude-runner"
    if health_fails:
        # Exactly what the real wrapper does with no daemon: a well-formed
        # envelope on stdout *and* a non-zero exit.
        body = ('printf \'{"ok":false,"error":"daemon_unavailable"}\\n\'\n'
                "exit 1\n")
    else:
        body = 'printf \'{"ok":true,"result":{"status":"ok"}}\\n\'\n'
    wrapper.write_text(f'#!/bin/sh\nprintf "wrapper %s\\n" "$*" >> {log}\n' + body)
    wrapper.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
    }
    return env, log


def _run_installer(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(SCRIPTS / "install_runner.sh"), *args],
        capture_output=True, text=True, timeout=300, env=env, cwd=str(REPO_ROOT),
    )


def test_installer_dry_run_touches_no_service(tmp_path: Path) -> None:
    env, log = _sandbox(tmp_path)
    completed = _run_installer(env, "--dry-run", "--skip-verify")

    assert completed.returncode == 0, completed.stderr
    calls = log.read_text()
    assert "launchctl" not in calls, "a dry run must not (re)load the LaunchAgent"
    assert "--dry-run" in calls, "the dry run must reach the runner's own installer"


def test_installer_skip_verify_runs_no_test_suite(tmp_path: Path) -> None:
    env, log = _sandbox(tmp_path)
    _run_installer(env, "--dry-run", "--skip-verify")
    assert "pytest" not in log.read_text()


def test_installer_verifies_by_default(tmp_path: Path) -> None:
    env, log = _sandbox(tmp_path)
    _run_installer(env, "--dry-run")
    calls = log.read_text()
    assert "pytest" in calls and "ruff" in calls and "mypy" in calls


def test_installer_json_mode_prints_exactly_one_object(tmp_path: Path) -> None:
    env, _ = _sandbox(tmp_path)
    completed = _run_installer(env, "--json", "--skip-verify")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)  # raises if progress text leaked to stdout
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["label"] == config.DEFAULT_LAUNCH_AGENT_LABEL
    assert payload["install"]["wrapper"]["action"] == "created"
    assert payload["health"]["ok"] is True


def test_installer_json_stays_parseable_when_the_daemon_never_answers(
    tmp_path: Path,
) -> None:
    """The failure branch is the one an installing agent most needs to parse."""
    env, _ = _sandbox(tmp_path, health_fails=True)
    completed = _run_installer(env, "--json", "--skip-verify")

    payload = json.loads(completed.stdout)  # raises if two objects were concatenated
    assert payload["health"]["ok"] is False
    assert payload["ok"] is False, "the installer must not claim success it did not get"
    assert completed.returncode != 0


def test_installer_json_reports_a_silent_wrapper_as_a_failure(tmp_path: Path) -> None:
    env, _ = _sandbox(tmp_path)
    wrapper = Path(env["HOME"]) / ".local" / "bin" / "hermes-claude-runner"
    wrapper.write_text("#!/bin/sh\nexit 1\n")   # no envelope at all
    wrapper.chmod(0o755)

    payload = json.loads(_run_installer(env, "--json", "--skip-verify").stdout)
    assert payload["ok"] is False
    assert payload["health"]["error"] == "daemon_unavailable"


def test_installer_is_idempotent(tmp_path: Path) -> None:
    """Running it twice must produce the same machine-readable outcome."""
    env, _ = _sandbox(tmp_path)
    first = json.loads(_run_installer(env, "--json", "--skip-verify").stdout)
    second = json.loads(_run_installer(env, "--json", "--skip-verify").stdout)
    assert first == second


def test_installer_honours_an_existing_installs_label(tmp_path: Path) -> None:
    env, log = _sandbox(tmp_path)
    env[config.ENV_LABEL] = INVENTED_LABEL
    completed = _run_installer(env, "--json", "--skip-verify")

    assert json.loads(completed.stdout)["label"] == INVENTED_LABEL
    assert INVENTED_LABEL in log.read_text()


def test_installer_provisions_the_runtime_from_the_checkout(tmp_path: Path) -> None:
    """The checkout is the *source* of the install, never a runtime dependency."""
    env, log = _sandbox(tmp_path)
    _run_installer(env, "--json", "--skip-verify")

    calls = log.read_text()
    assert f"install --repo-root {REPO_ROOT}" in calls, (
        "the installer must tell the runner which checkout to provision from"
    )


def test_installer_refuses_to_claim_success_when_the_wrapper_names_the_checkout(
    tmp_path: Path,
) -> None:
    """The regression this lane exists to prevent, caught on the user's Mac."""
    env, _ = _sandbox(tmp_path)
    wrapper = Path(env["HOME"]) / ".local" / "bin" / "hermes-claude-runner"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"# TARGET={REPO_ROOT}/somewhere/bin/hermes-claude-runner\n"
        "printf '{\"ok\":true,\"result\":{\"status\":\"ok\"}}\\n'\n"
    )
    wrapper.chmod(0o755)

    completed = _run_installer(env, "--json", "--skip-verify")

    payload = json.loads(completed.stdout)  # still exactly one object on stdout
    assert payload["health"]["ok"] is True, "the daemon answered; this is not a health failure"
    assert payload["ok"] is False
    assert completed.returncode != 0


def _launchctl_calls(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if line.startswith("launchctl ")]


def test_installer_retries_a_bootstrap_that_races_the_teardown(tmp_path: Path) -> None:
    """`launchctl bootout` returns before the label leaves the domain.

    Caught upgrading a healthy service on a real Mac: a valid plist, booted
    out and bootstrapped in the next breath, failed with exit 5
    (`Input/output error`), and the identical bootstrap succeeded once the
    teardown had settled.
    """
    env, log = _sandbox(tmp_path, bootstrap_failures=2)
    completed = _run_installer(env, "--json", "--skip-verify")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)   # still exactly one object
    assert payload["ok"] is True

    calls = _launchctl_calls(log)
    attempts = [i for i, line in enumerate(calls) if line.startswith("launchctl bootstrap")]
    assert len(attempts) == 3, calls

    # The retry waits for the domain; it must not reorder the install around it.
    bootout = next(i for i, line in enumerate(calls) if line.startswith("launchctl bootout"))
    kickstart = next(i for i, line in enumerate(calls) if line.startswith("launchctl kickstart"))
    assert bootout < attempts[0], calls
    assert kickstart > attempts[-1], calls


def test_installer_survives_the_slowest_teardown_seen_on_a_real_mac(tmp_path: Path) -> None:
    """The smoke that failed five attempts and loaded on the sixth.

    Pinned by attempt number rather than by seconds: the schedule may be
    stretched, but the machine that needed six bootstraps has to keep
    installing.
    """
    env, log = _sandbox(
        tmp_path, bootstrap_failures=OBSERVED_SLOWEST_BOOTSTRAP_ATTEMPT - 1,
    )
    completed = _run_installer(env, "--json", "--skip-verify")

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)   # still exactly one object
    assert payload["ok"] is True
    assert payload["health"]["ok"] is True

    calls = _launchctl_calls(log)
    attempts = [i for i, line in enumerate(calls) if line.startswith("launchctl bootstrap")]
    assert len(attempts) == OBSERVED_SLOWEST_BOOTSTRAP_ATTEMPT, calls
    kickstart = next(i for i, line in enumerate(calls) if line.startswith("launchctl kickstart"))
    assert kickstart > attempts[-1], "the service that finally loaded must still be kickstarted"


def test_installer_keeps_a_bootstrap_that_never_settles_fatal(tmp_path: Path) -> None:
    """Retrying must not turn a service that never loaded into a quiet success."""
    env, log = _sandbox(tmp_path, bootstrap_failures=BOOTSTRAP_NEVER_SETTLES)
    completed = _run_installer(env, "--json", "--skip-verify")

    assert completed.returncode != 0
    payload = json.loads(completed.stdout)   # still exactly one object
    # The health probe answers; only the bootstrap failed, and the verdict
    # has to come from there rather than from a coincidentally silent daemon.
    assert payload["health"]["ok"] is True
    assert payload["ok"] is False

    calls = _launchctl_calls(log)
    attempts = sum(line.startswith("launchctl bootstrap") for line in calls)
    assert attempts > OBSERVED_SLOWEST_BOOTSTRAP_ATTEMPT, (
        "a schedule that gives up on the slowest teardown already observed "
        "keeps no reserve for the machine that is a little slower"
    )
    assert not any(line.startswith("launchctl kickstart") for line in calls), (
        "kickstarting a service that was never bootstrapped hides the real failure"
    )

    # The operator has to be told how hard it tried and what launchd answered;
    # the count is checked against the calls so the message cannot drift.
    said = re.search(
        r"bootstrap never succeeded: (\d+) attempts, last exit (\d+)", completed.stderr,
    )
    assert said, completed.stderr
    assert int(said.group(1)) == attempts
    assert said.group(2) == "5", "the exit code the operator has to act on"


def test_installer_rejects_an_unknown_flag(tmp_path: Path) -> None:
    env, _ = _sandbox(tmp_path)
    completed = _run_installer(env, "--wat")
    assert completed.returncode != 0
    assert "wat" in completed.stderr


def test_surface_installer_verifies_registrations_with_a_check_that_can_fail() -> None:
    """`hermes tools` needs a terminal, so piping it always printed 0.

    A verification step that cannot fail is a claim, not a check.
    """
    text = (SCRIPTS / "install_plugin_on_surface.sh").read_text()
    commands = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("hermes tools" in line for line in commands), (
        "hermes tools refuses to run through a pipe; it can never verify anything here"
    )
    assert any('grep -q "7 tool(s)"' in line for line in commands), (
        "the plugin doctor output already proves the seven registrations"
    )


def test_surface_installer_never_swallows_a_failed_verification() -> None:
    text = (SCRIPTS / "install_plugin_on_surface.sh").read_text()
    for line in text.splitlines():
        if "grep -q" in line or "plugins doctor" in line:
            assert "|| true" not in line, f"failure swallowed: {line.strip()}"
