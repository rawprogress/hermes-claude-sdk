# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.2.0] - 2026-08-24

First release prepared for publication. Everything below is relative to the internal
0.1.0, which was never published.

### Added

- `hermes-claude-runner doctor [--json] [--no-daemon-probe]` — a read-only preflight that
  creates nothing, opens an existing database read-only, and reports a machine-readable
  verdict with the fix for every failing check.
- `scripts/install_runner.sh` gained `--dry-run`, `--json` and `--skip-verify`, so an agent
  can preflight and verify an install without parsing progress text.
- `HERMES_CLAUDE_RUNNER_LABEL` selects the LaunchAgent label, so an install made under an
  older label stays manageable.
- Community and release documentation: `LICENSE` (MIT), `SECURITY.md`, `CONTRIBUTING.md`,
  `AGENTS.md`, `INSTALL_FOR_AGENTS.md`, `RELEASE_CHECKLIST.md` and this changelog.
- `tests/test_portability.py` scans every tracked file for credential-shaped strings and
  generated artefacts, and checks the documented tool examples against the real plugin
  schemas.
- `tests/test_english_only.py` enforces the English-only rule for everything public-facing:
  it scans every tracked file for German prose and checks text that only exists at runtime
  (argparse help, the doctor's rendered report, the installer's `--help`). Its privacy half
  looks for *shapes* — an absolute `/Users/<account>` path outside the documented
  placeholders, or an address outside a reserved or no-reply domain — so the gate stores no
  identity of its own.

### Changed

- **The supported Python floor dropped from 3.14 to 3.12.** Requiring the newest release
  shut out most machines for no benefit; the full suite, ruff and mypy pass on 3.12, 3.13
  and 3.14. `doctor` reads the floor from one place, and a test keeps that in step with
  `pyproject.toml` and the documentation.
- **Breaking for re-installs:** the default LaunchAgent label is now
  `com.hermes-claude-sdk.runner` instead of one operator's reverse-DNS namespace. An
  existing install keeps running untouched; set `HERMES_CLAUDE_RUNNER_LABEL` to the old
  label to manage or remove it.
- The plugin's default `remote_command` is now `~/.local/bin/hermes-claude-runner`. ssh
  hands the command to the Mac's login shell, which expands the tilde, so the published
  default works for any account. Absolute paths remain accepted and are still the better
  choice when pinning an install. `~other/` is refused.
- `ssh_failed` and `ssh_timeout` now name the settings that decide the connection.
- `scripts/install_plugin_on_surface.sh` takes `MAC_REPO` relative to the Mac's home and
  keeps the remote tilde unexpanded, so it no longer assumes one particular macOS account.
- The shared test fixture redirects `home` into `tmp_path`, so no test can write to the
  caller's real installation.
- README rewritten for a public audience: quickstart, verification, worked examples for all
  seven tools, troubleshooting, security, update and uninstall.

### Fixed

- The publication gate stored the identifiers it forbade, and printed them in pytest node
  IDs on a passing run. It recognises shapes now — an absolute `/Users/<account>` path
  outside a documented placeholder vocabulary, an address outside a reserved or no-reply
  domain — so the repository holds no identity to leak, and the package metadata is scanned
  like any other file instead of being exempt.
- `scripts/install_plugin_on_surface.sh` claimed to verify the seven registrations with
  `hermes tools`, which refuses to run through a pipe: the check could neither pass nor
  fail. It now asserts against the plugin doctor's own output.
- The plugin doctor tests bootstrapped a Hermes home into whoever ran the suite, appending
  to their live `~/.hermes/logs/agent.log`. Every subprocess now gets a disposable `HOME`
  and `HERMES_HOME`.
- `doctor` reports `installable`, so the read-only preflight has a criterion that is
  reachable before anything is installed; `ok` remains the criterion afterwards. The two
  documents that contradicted each other now agree.
- `uv` is a required check: the installer runs `uv sync` unconditionally, so reporting it
  as optional sent an agent two phases past the real problem.
- Documented socket mode corrected to `0700`, the two JSON examples that did not parse now
  parse, and the troubleshooting section lists every stable error code, `not_a_git_repository`
  included.

### Removed

- `BUILD_BRIEF.md`, the internal implementation brief. Its content is superseded by the
  README, `AGENTS.md` and `SECURITY.md`.

## [0.1.0] - 2026-08-24

Internal only; never published.

### Added

- Mac runner: SQLite (WAL) state, unix-socket daemon, one detached worker process per run,
  worker reconciliation, and the `rpc` JSON contract.
- Git worktree isolation with symlink-resolved project containment; nothing is ever deleted,
  reset, cleaned or stashed.
- Hermes plugin with the seven `claude_*` tools, a fixed-argv ssh transport and a bundled
  orchestration skill.
- PreToolUse escalation machinery for the `blocked` lifecycle state.
- Credential-shaped payload refusal and redaction of everything derived from the model.
- LaunchAgent installation that is idempotent and backs up whatever it replaces.
