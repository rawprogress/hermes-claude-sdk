# Contributing

Thanks for looking. This is a small, opinionated project: it lets Hermes Agent drive Claude
Code on a Mac, and it tries very hard never to lie about what happened or destroy anything.

## Getting set up

```sh
git clone <your-fork> && cd hermes-claude-sdk
uv sync --all-groups
uv run pytest -q
```

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and git. You do **not** need a Mac,
Claude Code or an API key to run the test suite — no unit test contacts a model, and
git-backed tests build throwaway repositories under `tmp_path`.

To exercise the runner end to end you additionally need macOS, Claude Code signed in, and
(for the tests that cost tokens) `HERMES_CLAUDE_RUNNER_LIVE_SDK=1`.

## The workflow

1. **Write in English.** Everything a reader sees — prose, comments, output, examples —
   is English, and the test suite enforces it.
2. **Write the failing test first.** Behaviour changes are test-driven here, not
   test-decorated. Name the test after the behaviour.
3. **Make it pass** with the smallest change that is honest.
4. **Run the full gate** before you open anything — CI runs exactly these three on
   Python 3.12 and 3.14, so a green local run is a green pipeline:

   ```sh
   uv run pytest -q
   uv run ruff check .
   uv run mypy
   hermes plugins doctor hermes_plugin/hermes-claude-sdk --ci   # if you touched the plugin
   ```

5. **One reason to change per commit.** Write the message about *why*, not *what*.

## What will get a change rejected

Read [AGENTS.md](AGENTS.md) — it lists the invariants in full. The ones that come up most:

- Anything public-facing written in a language other than English — documentation,
  comments, CLI help, installer output, error details, examples.
  `tests/test_english_only.py` will fail.
- A hardcoded absolute path containing a username, or any author-specific identity string.
  `tests/test_english_only.py` and `tests/test_portability.py` will fail.
- A third-party import in `hermes_plugin/` — the Hermes venv stays clean.
- Anything that writes to stdout in `rpc` mode other than the single JSON envelope.
- `git reset` / `clean` / `stash` / `rm -rf` of user state, anywhere.
- A code path that can report success it did not verify.
- A test that writes outside `tmp_path`.

## Reporting a bug

Include the output of `uv run hermes-claude-runner doctor` (it contains no secrets), the
error envelope you got, and what you expected instead. If it involves a run, the `run_id`
and the relevant `claude_events` page help a lot.

**Do not open a public issue for a security problem** — see [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contribution is licensed under the MIT Licence, the
same as the rest of the project.
