"""Validate the plugin through the real Hermes plugin loader.

``hermes plugins doctor`` runs Hermes' own discovery, import and registration
path — the same check acceptance testing runs on the Hermes host.

It bootstraps a Hermes home as a side effect: a tree of directories, a
``SOUL.md`` and log files. Left alone it would write into whoever ran the
suite, appending to their live ``~/.hermes/logs/agent.log``. Every subprocess
here therefore gets a disposable ``HOME`` *and* ``HERMES_HOME`` under
``tmp_path``. Nothing is installed into the caller's account.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import HERMES, PLUGIN_DIR, requires_hermes

EXPECTED_TOOLS = [
    "claude_start", "claude_send", "claude_status",
    "claude_events", "claude_list", "claude_stop", "claude_resume",
]

FRESH_PROCESS_SCRIPT = f'''
import importlib.util, json, sys, types

root = {str(PLUGIN_DIR)!r}
namespace = "hermes_plugins"
package = types.ModuleType(namespace)
package.__path__ = []
sys.modules[namespace] = package

name = namespace + ".hermes_claude_sdk"
spec = importlib.util.spec_from_file_location(
    name, root + "/__init__.py", submodule_search_locations=[root]
)
module = importlib.util.module_from_spec(spec)
module.__package__ = name
module.__path__ = [root]
sys.modules[name] = module
spec.loader.exec_module(module)


class Ctx:
    def __init__(self):
        self.names = []

    def register_tool(self, **kwargs):
        self.names.append(kwargs["name"])

    def register_skill(self, **kwargs):
        pass

    def get_config(self, key, default=None):
        return default


ctx = Ctx()
module.register(ctx)
print(json.dumps(ctx.names))
'''


@pytest.fixture(scope="module")
def hermes_home(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """A disposable account for the real Hermes binary to bootstrap into."""
    home = tmp_path_factory.mktemp("hermes-home")
    return {**os.environ, "HOME": str(home), "HERMES_HOME": str(home / ".hermes")}


@pytest.fixture(scope="module")
def doctor(hermes_home: dict[str, str]) -> subprocess.CompletedProcess[str]:
    assert HERMES is not None
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [HERMES, "plugins", "doctor", str(PLUGIN_DIR), "--ci"],
        capture_output=True, text=True, timeout=600, env=hermes_home,
    )


@pytest.mark.integration
@requires_hermes
def test_doctor_passes(doctor: subprocess.CompletedProcess[str]) -> None:
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "OK:" in doctor.stdout


@pytest.mark.integration
@requires_hermes
def test_doctor_reports_no_warnings(doctor: subprocess.CompletedProcess[str]) -> None:
    assert "warning" not in doctor.stdout.lower(), doctor.stdout


@pytest.mark.integration
@requires_hermes
def test_doctor_sees_all_seven_registrations(
    doctor: subprocess.CompletedProcess[str]
) -> None:
    assert "7 tool(s)" in doctor.stdout


@pytest.mark.integration
@requires_hermes
def test_doctor_reads_the_manifest(doctor: subprocess.CompletedProcess[str]) -> None:
    assert "hermes-claude-sdk" in doctor.stdout
    assert "standalone" in doctor.stdout


def test_all_seven_tools_register_in_a_fresh_process() -> None:
    """Registration must work outside pytest's already-warm interpreter."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", FRESH_PROCESS_SCRIPT],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == EXPECTED_TOOLS


@pytest.mark.integration
@requires_hermes
def test_the_doctor_run_wrote_nothing_into_the_callers_account(
    doctor: subprocess.CompletedProcess[str], hermes_home: dict[str, str]
) -> None:
    """The Hermes home it bootstrapped must be the disposable one."""
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    bootstrapped = Path(hermes_home["HOME"]) / ".hermes"
    assert bootstrapped.is_dir(), "the binary bootstraps a home; it must be this one"
    assert not str(bootstrapped).startswith(str(Path.home()) + "/"), (
        "the disposable home must not live inside the caller's real home"
    )
