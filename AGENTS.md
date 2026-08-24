# AGENTS.md — conventions for anyone changing this codebase

Written for coding agents, useful to humans. These are the rules this repository already
holds itself to; a change that breaks one of them is a regression even when the tests pass.

## The architecture is the contract

`Hermes plugin → ssh → JSON-RPC → runner daemon → worker → Claude Agent SDK`.

Do not add a layer to that chain, and do not route around it. In particular:

- **The plugin stays standard-library only.** The Hermes venv must not grow a dependency
  because of us. `tests/plugin/test_registration.py` enforces the import list.
- **The plugin holds no state.** The runner's SQLite database is the single source of truth.
- **`rpc` stdout is JSON and nothing else.** Every log line goes to stderr. This is the one
  contract Hermes cannot recover from if it breaks.
- **One worker process per run**, spawned detached, with its PID recorded and reconciled.

## Honesty rules

These exist because the alternative is a system that lies to an orchestrator.

- **Never invent a result.** A worker that vanished without a final result becomes
  `unknown`, never `completed`.
- **Never report a cursor past an event the caller did not receive.** Truncation reports
  `truncated: true` and keeps the cursor honest, even when that means an empty page.
- **Never silently drop or cap without saying so.** If a response is shortened, the envelope
  says which event was dropped and how to move past it.
- **Document limitations where they are relevant, not only in a changelog.** The
  `AskUserQuestion` gap is documented in the README *and* the bundled skill, and
  `tests/plugin/test_docs.py` fails if either stops saying so.

## Safety rules

- **Nothing is ever deleted, reset, cleaned or stashed.** No `git reset`, no `git clean`, no
  `git stash`, no `rm -rf` of user state. Anywhere. `tests/runner/test_scripts.py` greps the
  scripts for those literal commands — a grep is a tripwire, not a proof, so the rule still
  has to be held by whoever writes the code.
- **Fail closed.** An unknown action, an unknown run, an uncontained project and an
  unparseable request all produce an error envelope, never a best-effort guess.
- **Validate before persisting.** Credential-shaped payloads are refused before anything
  reaches the database.
- **Every argv is fixed.** No shell, ever. Settings that feed an argv are matched against a
  strict pattern and fall back to the documented default.

## Language rules

- **Everything public-facing is English. Only English.** Documentation, code comments,
  commit messages, installer and CLI output, error details, tool descriptions, schema
  text, examples and templates. Contributors and their agents read this repository in one
  language.
- This is enforced, not requested: `tests/test_english_only.py` scans every tracked file
  for German prose and also checks text that only exists at runtime — argparse help, the
  doctor's rendered report and the installer's own `--help`.
- The language scan exempts exactly one file, the scanner itself, which has to spell out
  the words it looks for. The privacy scan exempts only the generated lockfile. A longer
  exemption list means the rule no longer holds; a test pins both.

## Portability rules

- **No absolute path containing a username, anywhere** — not in code, defaults, docs, tests
  or scripts. Everything derives from `$HOME` or an injectable override.
- **No personal identity anywhere either.** The privacy gate looks for *shapes*, never
  for particular people: an absolute `/Users/<account>` path outside the documented
  placeholders, or an e-mail address outside a reserved or no-reply domain. A gate that
  stored the identifiers it defends against would have to ship them — and would print them
  in pytest node IDs on a passing run.
- **Every root is injectable** through a `HERMES_CLAUDE_RUNNER_*` variable, which is also
  what lets tests run against `tmp_path`.
- **Tests never write outside `tmp_path`.** The shared `paths` fixture redirects `home` for
  exactly this reason; `test_config.py` guards it.

## Working style

- **TDD for behaviour changes.** Write the failing test first, in the same commit or an
  earlier one. Tests describe behaviour ("an existing install keeps its label"), not
  implementation.
- **Small, coherent commits.** One reason to change per commit. The message says *why* the
  old state was wrong, not what the diff shows.
- **Comments explain the non-obvious.** Why a timeout has that floor, why `can_use_tool` is
  deliberately unset. Never narrate the code.
- **No unit test ever contacts a model.** The worker is driven by a scripted fake client.
  Tests that cost tokens live in `tests/runner/test_live_sdk.py` and are opt-in via
  `HERMES_CLAUDE_RUNNER_LIVE_SDK=1`.

## Before you say you are done

```sh
uv run pytest -q          # 0 failed, English-only gate included
uv run ruff check .       # All checks passed!
uv run mypy               # Success
uv run hermes-claude-runner doctor
hermes plugins doctor hermes_plugin/hermes-claude-sdk --ci   # 7 tool(s)
```

Never push, publish, create a remote, or delete state. See
[INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md) for the full boundary list.
