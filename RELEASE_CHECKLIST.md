# Release checklist

Work top to bottom. Do not skip a step because the previous release skipped it.

## 1. The code is sound

- [ ] `uv sync --all-groups`
- [ ] `uv run pytest -q` — 0 failed
- [ ] `uv run ruff check .` — All checks passed
- [ ] `uv run mypy` — Success
- [ ] The suite passes on the declared floor *and* the newest release:
      `uv run -p 3.12 pytest -q` and `uv run -p 3.14 pytest -q`
- [ ] `hermes plugins doctor hermes_plugin/hermes-claude-sdk --ci` — 7 tool(s), exit 0
- [ ] `HERMES_CLAUDE_RUNNER_LIVE_SDK=1 uv run pytest tests/runner/test_live_sdk.py -q`
      — these cost tokens; run them when the SDK version changed
- [ ] `uv run hermes-claude-runner doctor` on a real Mac — READY

## 2. English only, and nothing personal, ships

- [ ] `uv run pytest tests/test_english_only.py -q` — German prose and author-specific
      identity strings, in files and in runtime-composed output
- [ ] `uv run pytest tests/test_portability.py -q` — credentials, caches, doc/CLI agreement
- [ ] Skim any document changed since the last release: is every sentence English?
- [ ] `git grep -n "$(whoami)"` over tracked files — expect nothing
- [ ] The published identity is a project, not a person: `pyproject.toml` `authors`,
      `plugin.yaml` `author` and the `LICENSE` holder all agree and carry no address
- [ ] **Do not push this history.** It contains the deleted internal brief, author-specific
      paths in several commits, and every commit is authored under a personal address.
      Publish from a fresh squashed root commit — the recipe is section 6.

## 3. Documentation matches reality

- [ ] README quickstart works on a machine that has never seen this project
- [ ] Every documented flag exists (`--dry-run`, `--json`, `--skip-verify`, `--no-daemon-probe`)
- [ ] `CHANGELOG.md` has a dated entry for this version, with breaking changes called out
- [ ] The `AskUserQuestion` limitation still states the SDK version it was verified against
- [ ] `SECURITY.md` still describes the trust model accurately

## 4. Version and metadata

- [ ] Bump `version` in `pyproject.toml`
- [ ] Bump `version` in `hermes_plugin/hermes-claude-sdk/plugin.yaml` if the plugin changed
- [ ] `hermes_claude_runner.__version__` and `rpc.__version__` agree with `pyproject.toml`
- [ ] `LICENSE` present, `license = "MIT"` in `pyproject.toml`, `license: MIT` in `plugin.yaml`

## 5. A clean install, verified

On a machine that does not already have it:

- [ ] Clone, `uv run hermes-claude-runner doctor --json` → `installable: true`. On a
      truly fresh account `projects_root` fails too, and then `installable` is `false`
      until `~/Projects` exists — that is correct, not a bug
- [ ] `./scripts/install_runner.sh --dry-run --json` → `would_create`
- [ ] `./scripts/install_runner.sh --json` → `ok: true`, `health.ok: true`
- [ ] Run it a second time → identical report, no new backup (idempotency)
- [ ] `./scripts/uninstall.sh` → service gone, database and worktrees still present

## 6. Publication (human only)

An agent must not perform any of these. The identity below was decided by the maintainers;
apply it exactly.

**Build the clean root commit.** Set the identity *before* committing — a commit inherits
whatever is configured at the moment it is created, and rewriting it afterwards means doing
this again.

```sh
git checkout --orphan public            # a root commit with no parent, no history
git add -A
git config user.name  "rawprogress"
git config user.email "<your-id>+rawprogress@users.noreply.github.com"
```

Take `<your-id>` from GitHub → Settings → Emails → "Keep my email address private"; it
carries an account-specific number, so copy the address shown there rather than composing
one. Then:

```sh
git commit -m "feat: Hermes Agent drives Claude Code on your Mac"
git log -1 --format='%an <%ae>%n%n%B'   # verify before anything leaves the machine
```

- [ ] The root commit is authored as `rawprogress` at the GitHub no-reply address
- [ ] **No `Co-Authored-By:` trailers** on it. They belong to the development history, not
      to the published root commit; the README credits Katso, rawprogress and Claude Code
- [ ] `git log --oneline` shows exactly one commit
- [ ] `git log -p | grep -c "$(whoami)"` → `0`
- [ ] Re-run sections 1 and 2 against the orphan branch before adding a remote
- [ ] Create the remote repository
- [ ] Push only `public:main`. Never use `git push --all`, `git push --mirror`, or
      `gh repo create --source=. --push` from the development repository: its old refs still
      contain the private history this clean root commit intentionally leaves behind
- [ ] Confirm the CI workflow ran green on the default branch
      (`.github/workflows/ci.yml`: pytest, ruff and mypy on Python 3.12 and 3.14).
      It cannot have run before a remote exists, so its first run is here
- [ ] Tag `v<version>` and push the tag
- [ ] Write release notes from the changelog entry
