"""Guards for a public release.

Two things a fork of this repository must be able to rely on: no file carries
a credential or a generated artefact, and the documentation describes the CLI
and the tool schemas that actually exist.

The English-only rule and the author-specific identifiers are the other half
of the publication gate; they live in :mod:`tests.test_english_only`.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_claude_runner import cli, doctor, redaction

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
AGENT_GUIDE = REPO_ROOT / "INSTALL_FOR_AGENTS.md"

# Fake tokens live here on purpose — they are what proves the refusal gate works.
SECRET_FIXTURE_DIRS = ("tests/",)

TEXT_SUFFIXES = {".py", ".md", ".sh", ".yaml", ".yml", ".toml", ".cfg", ".txt", ""}


def tracked_files() -> list[Path]:
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.split()
    return [REPO_ROOT / name for name in out]


def text_files() -> list[Path]:
    return [p for p in tracked_files() if p.suffix in TEXT_SUFFIXES and p.is_file()]


def relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ── no secrets ─────────────────────────────────────────────────────────────

# GitHub push protection and comparable scanners reject valid-looking provider
# tokens even when they are test fixtures. Keep the patterns here, but assemble
# every matching fixture from harmless fragments at runtime so no tracked blob
# contains a push-protection-capable literal.
PUSH_PROTECTION_PATTERNS = {
    "anthropic": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "aws": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "jwt": re.compile(
        r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    ),
    "slack": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
}


def test_no_tracked_blob_contains_a_provider_token_literal() -> None:
    offenders = {}
    for path in text_files():
        matches = [
            kind for kind, pattern in PUSH_PROTECTION_PATTERNS.items()
            if pattern.search(read(path))
        ]
        if matches:
            offenders[relative(path)] = matches
    assert offenders == {}, (
        "assemble synthetic tokens from fragments at runtime; literal fixtures "
        f"trigger repository push protection: {offenders}"
    )


def test_no_shipped_file_carries_credential_shaped_material() -> None:
    """The runner's own detector, pointed at the repository.

    Test fixtures are exempt: the fake tokens there are what prove the
    refusal gate works.
    """
    offenders = {}
    for path in text_files():
        name = relative(path)
        if name.startswith(SECRET_FIXTURE_DIRS):
            continue
        kinds = redaction.find_secrets(read(path))
        if kinds:
            offenders[name] = kinds
    assert offenders == {}, offenders


def test_no_generated_cache_or_backup_is_tracked() -> None:
    forbidden = (".pyc", ".db", ".sock", ".log")
    offenders = [
        relative(p) for p in tracked_files()
        if p.suffix in forbidden or "__pycache__" in p.parts
        or ".bak-" in p.name or ".removed-" in p.name
    ]
    assert offenders == [], offenders


# ── the community files a public repository needs ──────────────────────────

@pytest.mark.parametrize("name", [
    "LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md",
    "AGENTS.md", "CHANGELOG.md", "INSTALL_FOR_AGENTS.md", "RELEASE_CHECKLIST.md",
])
def test_the_release_documentation_is_present_and_tracked(name: str) -> None:
    path = REPO_ROOT / name
    assert path.is_file(), f"{name} is missing"
    assert path in tracked_files(), f"{name} exists but is not tracked"
    assert read(path).strip(), f"{name} is empty"


def test_the_declared_licence_is_the_one_that_ships() -> None:
    assert "MIT License" in read(REPO_ROOT / "LICENSE")
    assert 'license = "MIT"' in read(REPO_ROOT / "pyproject.toml")
    assert "license: MIT" in read(
        REPO_ROOT / "hermes_plugin" / "hermes-claude-sdk" / "plugin.yaml"
    )


# ── documented examples match the real contracts ───────────────────────────

def plugin_schemas() -> dict:
    sys.path.insert(0, str(REPO_ROOT / "hermes_plugin" / "hermes-claude-sdk"))
    try:
        import schemas  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return {s["name"]: s for s in schemas.ALL_SCHEMAS}


def documented_tool_examples() -> dict[str, dict]:
    """The first JSON block under each ``### `claude_x``` heading is its arguments."""
    text = read(README)
    examples: dict[str, dict] = {}
    sections = re.split(r"^### `(claude_\w+)`", text, flags=re.MULTILINE)
    for name, body in zip(sections[1::2], sections[2::2], strict=True):
        for block in re.findall(r"```json\n(.*?)\n```", body, flags=re.DOTALL):
            payload = json.loads(block)
            if "ok" not in payload:          # a result envelope, not an argument list
                examples[name] = payload
                break
    return examples


def test_every_tool_has_a_documented_example() -> None:
    assert set(documented_tool_examples()) == set(plugin_schemas()), (
        "the README must show all seven tools and only the seven that exist"
    )


@pytest.mark.parametrize("name", sorted(plugin_schemas()))
def test_documented_arguments_exist_in_the_real_schema(name: str) -> None:
    schema = plugin_schemas()[name]["parameters"]
    example = documented_tool_examples()[name]

    unknown = set(example) - set(schema["properties"])
    assert unknown == set(), f"{name} example passes arguments that do not exist: {unknown}"

    missing = set(schema["required"]) - set(example)
    assert missing == set(), f"{name} example omits required arguments: {missing}"


@pytest.mark.parametrize("name", sorted(plugin_schemas()))
def test_documented_argument_types_match_the_real_schema(name: str) -> None:
    kinds = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}
    properties = plugin_schemas()[name]["parameters"]["properties"]
    for key, value in documented_tool_examples()[name].items():
        expected = kinds[properties[key]["type"]]
        assert isinstance(value, expected), f"{name}.{key} should be {expected.__name__}"


def documented_cli_invocations() -> set[tuple[str, ...]]:
    pattern = re.compile(r"hermes-claude-runner ([a-z]+)((?: --[a-z-]+)*)")
    found = set()
    for document in (README, AGENT_GUIDE, REPO_ROOT / "AGENTS.md",
                     REPO_ROOT / "CONTRIBUTING.md", REPO_ROOT / "RELEASE_CHECKLIST.md"):
        for command, flags in pattern.findall(read(document)):
            found.add((command, *flags.split()))
    return found


def test_every_documented_cli_invocation_really_parses() -> None:
    parser = cli.build_parser()
    assert documented_cli_invocations(), "no CLI examples found — the regex has drifted"
    for invocation in sorted(documented_cli_invocations()):
        try:
            parser.parse_args(list(invocation))
        except SystemExit as exc:  # argparse exits on an unknown command or flag
            raise AssertionError(f"documented but unusable: {' '.join(invocation)}") from exc


@pytest.mark.parametrize("flag", ["--dry-run", "--json", "--skip-verify"])
def test_every_documented_installer_flag_is_implemented(flag: str) -> None:
    assert flag in read(README) or flag in read(AGENT_GUIDE)
    script = read(REPO_ROOT / "scripts" / "install_runner.sh")
    assert f"{flag})" in script, f"{flag} is documented but the installer ignores it"


# ── portable defaults, end to end ──────────────────────────────────────────

def test_a_fresh_account_gets_a_complete_working_installation(tmp_path: Path) -> None:
    """Everything a stranger's install needs, derived from their home alone."""
    import plistlib

    from hermes_claude_runner import config, launchd

    home = tmp_path / "a-different-person"
    home.mkdir()
    paths = config.paths_from_env({}, home=home)

    report = launchd.install(paths, repo_root=tmp_path / "checkout", dry_run=True)
    assert report["label"] == config.DEFAULT_LAUNCH_AGENT_LABEL

    plist = plistlib.loads(launchd.plist_xml(paths).encode())
    assert plist["Label"] == config.DEFAULT_LAUNCH_AGENT_LABEL
    assert str(home) in launchd.plist_xml(paths)
    assert plist["EnvironmentVariables"][config.ENV_PROJECTS_ROOT] == str(home / "Projects")
    assert str(paths.wrapper_path).startswith(str(home))


def test_the_doctor_knows_the_real_python_floor() -> None:
    """One declared minimum. A doctor that invents its own would lie to an agent."""
    declared = re.search(r'requires-python = ">=([\d.]+)"', read(REPO_ROOT / "pyproject.toml"))
    assert declared, "pyproject no longer declares requires-python"
    floor = tuple(int(part) for part in declared.group(1).split("."))
    assert doctor.MINIMUM_PYTHON == floor

    for name in ("README.md", "CONTRIBUTING.md"):
        assert f"{declared.group(1)}+" in read(REPO_ROOT / name), (
            f"{name} documents a different Python floor than pyproject declares"
        )


# ── documented JSON is JSON ────────────────────────────────────────────────

def json_blocks(document: Path) -> list[tuple[int, str]]:
    """Every ```json fence in *document*, with the line it starts on."""
    blocks = []
    for match in re.finditer(r"```json\n(.*?)\n```", read(document), flags=re.DOTALL):
        line = read(document)[: match.start()].count("\n") + 1
        blocks.append((line, match.group(1)))
    return blocks


@pytest.mark.parametrize("name", [
    "README.md", "INSTALL_FOR_AGENTS.md", "AGENTS.md", "SECURITY.md",
    "CONTRIBUTING.md", "CHANGELOG.md", "RELEASE_CHECKLIST.md",
])
def test_every_documented_json_block_parses(name: str) -> None:
    """A reader who pastes an example must get JSON, not a syntax error.

    Fence anything that is deliberately elided as ``text``; a ```json fence is
    a promise that the content is valid.
    """
    for line, block in json_blocks(REPO_ROOT / name):
        try:
            json.loads(block)
        except ValueError as exc:
            raise AssertionError(
                f"{name}:{line} is fenced as json but does not parse: {exc}"
            ) from exc


def test_the_json_gate_sees_the_blocks_it_claims_to_check() -> None:
    """A parser that silently matches nothing would pass every document."""
    assert len(json_blocks(README)) >= 14, "the fence regex has drifted"
    assert len(json_blocks(AGENT_GUIDE)) >= 1


# ── the version is one number ──────────────────────────────────────────────

def test_every_declared_version_agrees() -> None:
    """Four places declare it; a release with three of them bumped is a bug."""
    import hermes_claude_runner
    from hermes_claude_runner import rpc

    declared = re.search(r'^version = "([^"]+)"', read(REPO_ROOT / "pyproject.toml"),
                         flags=re.MULTILINE)
    assert declared, "pyproject declares no version"
    version = declared.group(1)

    manifest = re.search(r"^version: (.+)$",
                         read(REPO_ROOT / "hermes_plugin" / "hermes-claude-sdk" / "plugin.yaml"),
                         flags=re.MULTILINE)
    assert manifest and manifest.group(1).strip() == version
    assert hermes_claude_runner.__version__ == version
    assert rpc.__version__ == version


def test_the_changelog_documents_the_version_being_released() -> None:
    """An entry nobody dated is an entry nobody released."""
    changelog = read(REPO_ROOT / "CHANGELOG.md")
    version = re.search(r'^version = "([^"]+)"', read(REPO_ROOT / "pyproject.toml"),
                        flags=re.MULTILINE).group(1)
    heading = re.search(rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$",
                        changelog, flags=re.MULTILINE)
    assert heading, f"CHANGELOG has no dated ## [{version}] - YYYY-MM-DD heading"


# ── the troubleshooting table covers what the code can raise ───────────────

def test_every_stable_error_code_is_documented() -> None:
    """A code a user can see and cannot look up is a dead end."""
    from hermes_claude_runner import errors

    documented = read(README)
    for code in sorted(errors.ERROR_CODES):
        assert code in documented, f"{code} can reach a user but the README never mentions it"


# ── continuous integration ─────────────────────────────────────────────────

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def workflow() -> dict:
    import yaml

    return yaml.safe_load(read(WORKFLOW))


def test_a_ci_workflow_exists_and_is_valid_yaml() -> None:
    assert WORKFLOW.is_file(), "the release checklist asks for CI; it must exist"
    assert isinstance(workflow(), dict)


def test_ci_covers_the_declared_python_floor_and_the_newest_version() -> None:
    """A floor nobody tests is a floor nobody supports."""
    declared = re.search(r'requires-python = ">=([\d.]+)"',
                         read(REPO_ROOT / "pyproject.toml")).group(1)
    versions = {
        str(v) for v in workflow()["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    }
    assert declared in versions, f"CI never runs the declared floor {declared}"
    assert {"3.12", "3.14"} <= versions


def test_ci_runs_every_gate_the_contributor_guide_promises() -> None:
    steps = workflow()["jobs"]["test"]["steps"]
    script = " ".join(step.get("run", "") for step in steps)
    assert "pytest" in script
    assert "ruff check" in script
    assert "mypy" in script


def test_ci_runs_on_macos_because_the_runner_is_a_launch_agent() -> None:
    """The platform check is required, so a Linux runner would fail honestly."""
    assert "macos" in str(workflow()["jobs"]["test"]["runs-on"]).lower()


def test_ci_never_pushes_or_publishes() -> None:
    """CI verifies; releasing stays a human decision."""
    text = read(WORKFLOW)
    for forbidden in ("git push", "gh release", "twine", "pypi", "uv publish"):
        assert forbidden not in text.lower(), f"the workflow does {forbidden}"


def test_ci_is_listed_as_done_in_the_release_checklist() -> None:
    checklist = read(REPO_ROOT / "RELEASE_CHECKLIST.md")
    assert "this project has none yet" not in checklist, (
        "the checklist still says CI does not exist"
    )


def test_the_checklist_tells_the_publisher_how_to_leave_the_history_behind() -> None:
    """The history is unpublishable; the recipe that replaces it must be exact."""
    checklist = read(REPO_ROOT / "RELEASE_CHECKLIST.md")
    assert "git checkout --orphan" in checklist, "no recipe for a clean root commit"
    assert "users.noreply.github.com" in checklist, "no anonymous address form"
    assert "Co-Authored-By" in checklist, "the trailer decision must be written down"
    assert "user.email" in checklist and "before" in checklist.lower(), (
        "the identity has to be set before the commit is created"
    )
