# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `transport: local` reaches a runner on the same machine without ssh. The plugin runs the
  validated runner command plus `rpc` directly: a fixed argv of exactly two elements, never
  a shell, with `local_command` held to the same "`~/…` or absolute, no metacharacters"
  shape as `remote_command` by one shared pattern. Because no login shell is involved the
  plugin expands the tilde itself and refuses anything that does not resolve to an absolute
  path, rather than letting a relative command resolve against Hermes' working directory.
  The default is unchanged — an install that never heard of `transport` keeps its ssh argv
  byte for byte, and an unrecognised value falls back to ssh.
- Local failures carry their own codes, `local_timeout`, `local_unavailable` and
  `local_failed`, because an `ssh_timeout` naming `ssh_host` would send the reader to a
  setting that had no part in the failure. The envelope shape and the 240 s floor are
  shared: local mode removes the ssh hop, not the runner's inline worktree and socket wait.

### Changed

- **The installed service runs from a managed runtime, not from the checkout.** The
  LaunchAgent used to exec `<checkout>/.venv/bin/hermes-claude-runner`, so moving or
  deleting the source tree silently disarmed the daemon. `install` now provisions one
  virtualenv per install generation under `HERMES_CLAUDE_RUNNER_RUNTIME` (default
  `<data dir>/runtime`), holding a non-editable copy of the package, plus a `current`
  symlink the wrapper follows. `uv sync --frozen` installs what the checkout locked, so
  provisioning cannot re-resolve or rewrite the lockfile. The checkout can be renamed or
  deleted afterwards without stopping the service.
- Install ordering is the safety property: allocate a generation, sync, verify its console
  script, write the manifest, swap the symlink, and only then rewrite the wrapper and
  plist. A provision that fails at any step raises with the previous generation still live
  and still current. An exclusive lock covers allocation, sync, activation and rollback, so
  two installers serialize instead of handing out the same generation. Every generation is
  kept, which makes going back a symlink swap; running workers keep the runtime they
  started from. The installer also checks that the wrapper it installed names nothing in
  the checkout, and records the answer in its verdict rather than exiting early, so
  `--json` still prints exactly one object.
- `doctor` now proves Claude Code is usable rather than merely present. `claude_cli`
  resolves the binary, requires it to be an executable file, and makes it report its
  version. Two new required checks join it: `claude_auth`, which asks
  `claude auth status --json` whether a session exists, and `agent_sdk`, which compares
  the importable `claude-agent-sdk` against the version `pyproject.toml` pins. Each
  carries a machine-readable `state` from a declared vocabulary, and an exact fix.
- A machine with no signed-in session reports `installable: false`. Installing cannot sign
  anybody in, so offering it as the fix would walk an installing agent past the one step
  that needs a human. `INSTALL_FOR_AGENTS.md` maps both new checks and their states.
- The preflight always emits one machine-readable report. Output that is not UTF-8, JSON
  nested past the recursion limit, and a binary that cannot be spawned each produce a
  verdict rather than a traceback.
- Probe output is captured with a bound in memory and still read to completion, so a CLI
  that prints megabytes is neither buffered in full nor mistaken for one that hung. Both
  probes remain bounded by a timeout.

- The secret scrubber is linear. Both its assignment scan and the doctor's
  address scan opened with an unbounded run of name characters, which made the
  regex engine walk that run and back at every position: 64 KiB of `a.` cost
  200 seconds and 6.8 seconds respectively, on text a CLI controls. Both now
  anchor on the one token that has to be there — the credential word, the `@` —
  and walk outwards. Nothing about what counts as a secret changed.

### Fixed

- **The installer no longer loses the race between `bootout` and `bootstrap`.** Upgrading a
  healthy service booted the old one out and bootstrapped the new plist in the next breath,
  but `bootout` returns when the teardown is *requested*, not when it is done: while the
  old job still held the label, launchd refused the bootstrap with exit 5,
  `Input/output error`, and the install died with a valid plist, an executable entrypoint
  and no service. The bootstrap is now its own readiness probe — retried on any non-zero
  exit, immediately and then after 0.2 s, 0.5 s, 1 s, 2 s, 4 s, 4 s and 4 s: eight attempts
  and at most 15.7 s of waiting, with nothing added to an install that has nothing to wait
  for. The bound comes from the machine it was found on, whose slowest teardown released
  the label only in time for the sixth attempt at 7.7 s; the ladder deliberately runs past
  that so a slightly slower machine still has attempts in reserve, and stops doubling at
  4 s because a longer gap postpones the verdict without making a stuck teardown more
  likely to finish. A bootstrap that never succeeds stays fatal: the exit code and the
  attempt count are reported on stderr, `kickstart` is skipped rather than piling a derived
  failure on the real one, `--json` still prints exactly one object, and the installer exits
  non-zero.
- **A stop now stops the whole worker process group, not just its pid.** Only the worker
  was signalled, so the Claude CLI it spawned kept running against the run's worktree. The
  group is signalled only when the worker provably leads both it and its session
  (`sid == pgid == pid`, which is what every spawned worker gets), never when it is the
  daemon's own group; anything else falls back to the single-pid signal. The existing
  identity proof still runs last, immediately before the signal, so a recycled or unrelated
  pid is never touched.
- **A requested stop is no longer finalized as `failed`.** Terminating the group tears the
  CLI down under the SDK, so the turn raises mid-flight; the worker treated that as a
  crash. It now asks whether a stop is on record: with one the run ends `stopped`, with the
  usual preserved worktree and the SDK error kept in the event trail, and without one the
  same exception still fails the run. An unreadable stop flag counts as no stop, so a broken
  store can never rewrite a genuine failure into a stop.
- **An existing worktree is proven to belong to the run before it is reused.** Reuse only
  checked that the target was some repository root, so a standalone repository initialised
  at that path, or a linked worktree of a different repository, was silently adopted and the
  run's commits landed in a repository nobody asked for; a worktree that had wandered onto
  another branch was adopted too. Reuse now requires two proofs from git — the same shared
  object store as the project, and the run's own branch checked out — and a target that
  fails either is refused as `worktree_failed` exactly as it stands. Nothing is deleted,
  reset, cleaned or stashed. A probe git cannot answer stays `worktree_failed`, so
  `not_a_git_repository` keeps speaking only for the project argument.

### Security

- **The runner's state on disk is private.** The database carries prompts, results and
  session identifiers but was created with whatever the caller's umask allowed — `0755`
  state directories and a `0644` database, `-wal` and `-shm` under a common umask of 022.
  `db.connect` now creates every state directory it makes at `0700` and the database file
  at `0600` before SQLite opens it, which is what makes SQLite create the sidecars privately
  too. Directories and files that already exist lose their group and world bits, so an
  installation predating this is tightened without touching its data; a path owned by
  another account is skipped rather than turned into an error.
- No part of the auth payload reaches a report, on any path. Only `loggedIn` and the
  non-identifying mode fields are read: the account's address and organisation stay inside
  the CLI, and a non-zero exit is classified rather than quoted back.
- Everything the CLI does supply is redacted before it is truncated, and bounded per field
  as well as per line, so no output the doctor did not expect can grow a report or carry a
  credential into one.

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
