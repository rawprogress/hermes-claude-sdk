"""The publication gate: English only, and nobody's identity in it.

Everything a reader of this repository can see has to be in English — the
documentation, the code comments, the installer output, the CLI help, the
plugin's tool descriptions, the examples.

The privacy half looks for *shapes*, never for particular people. A gate that
stores the identifiers it is defending against has to ship them, print them in
pytest node IDs and leak them into public CI logs; this one knows what a home
directory and an e-mail address look like, and nothing more. Placeholder
accounts and reserved domains are the only things allowed through.

The check runs over dynamically composed text too — argparse help and the
doctor's rendered report are built at runtime and would otherwise escape a
scan of the files alone.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_claude_runner import cli, config, doctor

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The only file allowed to contain German words: this one, which has to spell
#: them out to look for them. Kept to a single entry on purpose — see
#: ``test_the_language_exemption_stays_a_single_file``.
GERMAN_SCAN_EXEMPT = {"tests/test_english_only.py"}

#: Generated, and not prose anyone reads. Nothing else is exempt: the package
#: metadata is scanned like every other file.
IDENTITY_SCAN_EXEMPT = {"uv.lock"}

#: Account names a path may use in documentation, examples and fixtures. Any
#: other name in a ``/Users/<name>`` path is somebody's real home directory.
PLACEHOLDER_ACCOUNTS = frozenset({"me", "you", "user", "someone", "x", "test"})

#: RFC 2606 reserved domains plus GitHub's anonymous address form. An address
#: at any other domain belongs to a real person.
PLACEHOLDER_EMAIL_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "users.noreply.github.com",
})

# Assembled from fragments so that this file is not itself a match for the
# shape it searches for. Nothing personal has to be stored to look for it.
_HOME_RE = re.compile("/Users" + r"/([A-Za-z0-9._-]+)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")

TEXT_SUFFIXES = {".py", ".md", ".sh", ".yaml", ".yml", ".toml", ".cfg", ".txt", ""}

# German function words and common technical-prose words with no English
# collision and no plausible use as an identifier. Deliberately conservative:
# "die", "war", "man", "hat", "also", "mit" and "in" are all English words too,
# and "MIT" is the licence, so none of them can be used as evidence.
GERMAN_WORDS = (
    # articles, pronouns, conjunctions
    "und", "oder", "nicht", "aber", "auch", "noch", "schon", "wenn", "dann",
    "weil", "dass", "damit", "deshalb", "deswegen", "jedoch", "sondern",
    "der", "das", "dem", "dieser", "diese", "dieses", "diesem", "welche",
    "eine", "einen", "einem", "eines", "kein", "keine", "keinen",
    "wir", "ich", "sie", "uns", "euch", "ihnen", "ihre", "ihrem", "ihren",
    "sein", "seine", "seinen",
    # verbs and modals
    "ist", "sind", "waren", "wird", "werden", "wurde", "wurden", "worden",
    "kann", "können", "konnte", "muss", "müssen", "musste", "soll", "sollte",
    "sollen", "darf", "dürfen", "hätte", "wäre", "gibt", "geben",
    # prepositions and adverbs
    "nach", "ohne", "durch", "gegen", "zwischen", "während", "über", "für",
    "unter", "bei", "aus", "auf", "zum", "zur", "beim", "sehr", "hier",
    "dort", "jetzt", "heute", "wieder", "immer", "etwas", "nichts", "mehr",
    "jeder", "jede", "jedes", "alle", "allen", "allem",
    # words that turn up in German technical writing
    "datei", "dateien", "verzeichnis", "benutzer", "einstellungen", "fehler",
    "sicherheit", "anleitung", "übersicht", "voraussetzungen", "hinweis",
    "achtung", "beispiel", "beispiele", "siehe", "folgende", "folgenden",
    "verwenden", "verwendet", "installieren", "ausführen", "prüfen",
    "erstellen", "löschen", "ändern", "starten", "beenden", "abbrechen",
)

_GERMAN_RE = re.compile(rf"\b(?:{'|'.join(GERMAN_WORDS)})\b", re.IGNORECASE)


def tracked_files() -> list[str]:
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout.split()
    return sorted(out)


def scannable(exempt: set[str]) -> list[str]:
    return [
        name for name in tracked_files()
        if name not in exempt
        and Path(name).suffix in TEXT_SUFFIXES
        and (REPO_ROOT / name).is_file()
    ]


def read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")


def german_in(text: str) -> list[str]:
    """The German words *text* contains, deduplicated and in order."""
    return list(dict.fromkeys(match.lower() for match in _GERMAN_RE.findall(text)))


# ── English only ───────────────────────────────────────────────────────────

def test_no_tracked_file_contains_german_prose() -> None:
    offenders = {}
    for name in scannable(GERMAN_SCAN_EXEMPT):
        found = german_in(read(name))
        if found:
            offenders[name] = found
    assert offenders == {}, (
        f"public-facing text must be English only; German found in: {offenders}"
    )


@pytest.mark.parametrize("name", [
    "README.md", "INSTALL_FOR_AGENTS.md", "AGENTS.md", "SECURITY.md",
    "CONTRIBUTING.md", "CHANGELOG.md", "RELEASE_CHECKLIST.md", "LICENSE",
])
def test_each_published_document_is_english(name: str) -> None:
    """Named individually so a failure says which document regressed."""
    assert german_in(read(name)) == []


@pytest.mark.parametrize("name", [
    "scripts/install_runner.sh",
    "scripts/install_plugin_on_surface.sh",
    "scripts/uninstall.sh",
])
def test_installer_output_is_english(name: str) -> None:
    assert german_in(read(name)) == []


def test_the_plugins_model_facing_contract_is_english() -> None:
    """Tool names and descriptions are read by a model and by users."""
    for name in ("plugin.yaml", "schemas.py", "tools.py", "__init__.py"):
        text = read(f"hermes_plugin/hermes-claude-sdk/{name}")
        assert german_in(text) == [], name
    skill = "hermes_plugin/hermes-claude-sdk/skills/claude-code-orchestration/SKILL.md"
    assert german_in(read(skill)) == []


# ── English only, in text that only exists at runtime ──────────────────────

def test_the_cli_help_is_english() -> None:
    parser = cli.build_parser()
    assert german_in(parser.format_help()) == []
    for action in parser._subparsers._group_actions[0].choices.values():  # noqa: SLF001
        assert german_in(action.format_help()) == [], action.prog


#: Every machine state the doctor has prose for. Rendering one state would
#: leave the sentences the other four print unscanned.
DOCTOR_STATES = {
    "signed-in": {},
    "logged-out": {"auth_stdout": '{"loggedIn": false}'},
    "too-old-for-the-auth-probe": {
        "auth_stdout": "", "auth_stderr": "error: unknown command auth", "auth_exit": 1,
    },
    "unreadable-auth-answer": {"auth_stdout": "not json"},
    "unusable-binary": {"version_stdout": "", "version_stderr": "boom", "version_exit": 1},
}


@pytest.mark.parametrize("state", list(DOCTOR_STATES), ids=list(DOCTOR_STATES))
def test_the_doctors_rendered_report_is_english(tmp_path: Path, state: str) -> None:
    """Hermetic on purpose: this used to fall through to the real Claude Code.

    With no binary at the disposable home the doctor consulted PATH, so a
    publication gate ran the caller's own CLI and its auth probe — and the
    states that binary was not in went unscanned.
    """
    from tests.runner.test_doctor import fake_claude

    home = tmp_path / "home"
    cli = fake_claude(home / ".local" / "bin" / "claude", **DOCTOR_STATES[state])
    paths = config.paths_from_env({}, home=home)
    assert paths.claude_cli_path == cli, "the fake is not where the doctor looks"

    report = doctor.diagnose(paths, probe_daemon=False)

    rendered = doctor.render(report)
    assert german_in(rendered) == []
    for entry in report["checks"]:
        assert german_in(f"{entry['detail']} {entry['fix']}") == [], entry["name"]


def test_the_publication_gate_never_runs_the_real_claude(
    tmp_path: Path, monkeypatch
) -> None:
    """Proof, not intent: the binary this gate spawns is the disposable one."""
    from tests.runner.test_doctor import fake_claude

    spawned: list[str] = []
    spawn = doctor._spawn  # noqa: SLF001

    def record(argv):  # type: ignore[no-untyped-def]
        spawned.append(str(argv[0]))
        return spawn(argv)

    monkeypatch.setattr(doctor, "_spawn", record)
    home = tmp_path / "home"
    fake_claude(home / ".local" / "bin" / "claude")
    doctor.diagnose(config.paths_from_env({}, home=home), probe_daemon=False)

    assert spawned, "the gate no longer exercises the probes at all"
    real = shutil.which("claude")
    for binary in spawned:
        assert binary != real, f"the publication gate ran the real CLI at {binary}"
        assert Path.home() not in Path(binary).parents, binary
        assert str(tmp_path) in binary, binary


def test_the_installers_own_usage_text_is_english() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(REPO_ROOT / "scripts" / "install_runner.sh"), "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert german_in(completed.stdout) == []
    assert completed.stdout.strip(), "the installer must explain itself"


# ── nobody's home directory, nobody's address ──────────────────────────────

def real_account_paths(text: str) -> list[str]:
    """Home directories in *text* that are not documented placeholders."""
    return [
        account for account in _HOME_RE.findall(text)
        if account.lower() not in PLACEHOLDER_ACCOUNTS
    ]


def personal_emails(text: str) -> list[str]:
    """Addresses in *text* that are not at a reserved or no-reply domain."""
    return [
        f"{local}@{domain}" for local, domain in _EMAIL_RE.findall(text)
        if domain.lower() not in PLACEHOLDER_EMAIL_DOMAINS
        and not local.lower().startswith("noreply")
    ]


def test_no_tracked_file_shows_a_real_accounts_home_directory() -> None:
    """Examples must use a placeholder, so nobody publishes their own path."""
    offenders = {}
    for name in scannable(IDENTITY_SCAN_EXEMPT):
        found = real_account_paths(read(name))
        if found:
            offenders[name] = sorted(set(found))
    assert offenders == {}, (
        f"a real account's home directory must not ship: {offenders}. "
        f"Use one of {sorted(PLACEHOLDER_ACCOUNTS)}."
    )


def test_no_tracked_file_carries_a_personal_email_address() -> None:
    """Package metadata included: a release must not publish anyone's inbox."""
    offenders = {}
    for name in scannable(IDENTITY_SCAN_EXEMPT):
        found = personal_emails(read(name))
        if found:
            offenders[name] = sorted(set(found))
    assert offenders == {}, (
        f"a personal address must not ship: {offenders}. Use a reserved domain "
        f"({sorted(PLACEHOLDER_EMAIL_DOMAINS)}) or a noreply address."
    )


def test_the_published_identity_is_a_project_not_a_person() -> None:
    """The maintainers publish under a project identity, with no address."""
    metadata = read("pyproject.toml")
    assert 'authors = [{ name = "Katso & rawprogress" }]' in metadata
    assert "email" not in metadata.split("dependencies")[0], (
        "package metadata must carry no address at all"
    )
    assert "author: Katso & rawprogress" in read(
        "hermes_plugin/hermes-claude-sdk/plugin.yaml"
    )
    assert "Katso, rawprogress, and contributors" in read("LICENSE")


# ── the gate itself has to stay trustworthy ────────────────────────────────

def test_the_exemptions_stay_minimal() -> None:
    """An exemption list that grows is a rule that no longer holds."""
    assert GERMAN_SCAN_EXEMPT == {"tests/test_english_only.py"}
    assert IDENTITY_SCAN_EXEMPT == {"uv.lock"}, (
        "the privacy scan looks for shapes, so nothing needs exempting but the lockfile"
    )
    for name in GERMAN_SCAN_EXEMPT | IDENTITY_SCAN_EXEMPT:
        assert (REPO_ROOT / name).exists(), f"{name} is exempted but does not exist"


def test_this_repository_stores_no_identity_to_defend_against() -> None:
    """The fixture that held real identifiers is gone and must not come back.

    It shipped the values it existed to forbid, and printed them in pytest
    node IDs on a passing run.
    """
    # Spelled in fragments so this assertion is not itself a match.
    module = "legacy_" + "identifiers"
    assert not (REPO_ROOT / "tests" / f"{module}.py").exists()
    for name in scannable(set()):
        assert module not in read(name), (
            f"{name} still refers to the deleted identity fixture"
        )


def test_the_detector_actually_detects() -> None:
    """A gate nobody has seen fail is a gate nobody can trust."""
    assert german_in("Diese Datei ist auf Deutsch und muss abgelehnt werden.")
    assert german_in("Bitte prüfen Sie die Einstellungen.")
    assert german_in("Der Installer wird ausgeführt.")


def test_the_detector_passes_this_projects_english() -> None:
    """No false positive on the vocabulary this repository actually uses."""
    sample = (
        "The daemon owns runs and spawns one worker per run. A worker that dies "
        "is reported as unknown, never as completed. Install it with uv, then "
        "check the LaunchAgent, the socket and the database. See SECURITY.md."
    )
    assert german_in(sample) == []


def test_a_lone_non_ascii_character_is_not_read_as_german() -> None:
    """The UTF-8 round-trip fixtures use umlaut characters, not German words."""
    assert german_in('{"text": "' + "ü" * 200 + '"}') == []


def test_the_gate_covers_every_published_document() -> None:
    """Nothing public may be added without the language gate noticing."""
    published = {
        name for name in tracked_files()
        if Path(name).suffix == ".md" and "/" not in name
    }
    assert published <= set(scannable(GERMAN_SCAN_EXEMPT)), (
        "a top-level document escaped the scan"
    )
    assert {"README.md", "INSTALL_FOR_AGENTS.md", "SECURITY.md"} <= published


def test_the_privacy_detectors_actually_detect() -> None:
    """Invented values, assembled at runtime, so nothing real is stored here."""
    invented_account = "/Users" + "/" + "jane" + "doe"
    invented_address = "jane" + "." + "doe" + "@" + "gmail" + ".com"

    assert real_account_paths(f"run it from {invented_account}/Projects")
    assert personal_emails(f"contact {invented_address} for help")

    # ...and the placeholders they must not fire on.
    assert real_account_paths("/Users" + "/me/Projects/demo") == []
    assert personal_emails("test" + "@" + "example.com") == []
    assert personal_emails("noreply" + "@" + "anthropic.com") == []
    assert personal_emails("1234+handle" + "@" + "users.noreply.github.com") == []


def test_no_test_is_parametrized_over_a_privacy_needle() -> None:
    """Node IDs are printed by ``--collect-only`` and land in public CI logs.

    The previous gate parametrized over the author's home directory, name and
    address, so a fully passing run published all three.
    """
    for name in scannable(set()):
        if not name.startswith("tests/"):
            continue
        source = read(name)
        for block in re.findall(r"parametrize\((.*?)\)\s*\ndef ", source, re.DOTALL):
            assert real_account_paths(block) == [], name
            assert personal_emails(block) == [], name
