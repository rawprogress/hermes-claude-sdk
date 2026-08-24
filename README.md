# hermes-claude-sdk

Let **Hermes Agent** on one machine drive **Claude Code** on your Mac.

Hermes gets seven tools — `claude_start`, `claude_send`, `claude_status`, `claude_events`,
`claude_list`, `claude_stop`, `claude_resume`. Hermes decides *what* should happen; Claude
writes the code, runs the tests, the linter, the type checker and the build, and commits.
Nobody has to sit at the Mac.

There is no API key. Claude authenticates with the Claude Code login that is already on
the Mac.

> **Installing this with a coding agent?** Point it at
> **[INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md)** — a deterministic read-only preflight,
> exact commands, machine-readable success criteria and hard stop points. See
> [Agent Quickstart](#agent-quickstart) below.

---

## How it works

```
Hermes host (Linux/macOS)                    Mac (macOS, Apple Silicon or Intel)
┌──────────────────────────────┐             ┌──────────────────────────────────────┐
│ plugin hermes-claude-sdk     │             │ LaunchAgent                          │
│  claude_start  claude_send   │  ssh -o     │   hermes-claude-runner daemon        │
│  claude_status claude_events │ BatchMode   │   unix socket  ~/…/daemon.sock       │
│  claude_list   claude_stop   │ ──JSON──▶   │   SQLite (WAL) ~/…/data.db           │
│  claude_resume               │             │   one worker process per run         │
│  (stdlib only, holds no      │             │     └─ ClaudeSDKClient → claude CLI  │
│   state)                     │             │          in a git worktree           │
└──────────────────────────────┘             └──────────────────────────────────────┘
```

Four moving parts, in the order a request passes through them:

1. **The plugin** holds no state. It runs exactly one command shape —
   `ssh -o BatchMode=yes <host> <runner> rpc` — and passes the request as JSON on stdin.
   Never a shell, never a third-party dependency in the Hermes venv.
2. **`hermes-claude-runner rpc`** is a thin pipe to the daemon's unix socket. It writes one
   JSON envelope to stdout and nothing else; all logging goes to stderr.
3. **The daemon** owns runs. Commands return quickly: it creates the git worktree, spawns a
   detached worker and answers. It reconciles lost workers at startup and every 30 s.
4. **One worker per run** drives a long-lived `ClaudeSDKClient`, picks up follow-up messages
   between turns, and writes structured, redacted events into SQLite.

Every reply is `{"ok": true, "result": {…}}` or
`{"ok": false, "error": "<stable_code>", "detail": "<human readable>"}`. Always.

Run lifecycle: `queued → preparing → working → completed | blocked | failed | stopped | unknown`.

### Where things live

| Path | What it is |
| --- | --- |
| `src/hermes_claude_runner/` | The Mac runner: config, db/store, rpc, worktree, daemon, worker, doctor |
| `hermes_plugin/hermes-claude-sdk/` | The Hermes plugin and its bundled skill |
| `scripts/` | Idempotent install and uninstall helpers |
| `tests/` | The suite; no unit test ever contacts a model |

---

## Requirements

| Where | What |
| --- | --- |
| Mac | macOS, Python 3.12+, [uv](https://docs.astral.sh/uv/), git, [Claude Code](https://claude.com/claude-code) **already signed in**. Node.js only if your Claude Code install needs it — `doctor` reports it without blocking |
| Hermes host | Hermes Agent with plugin support, `ssh` and `scp` |
| Between them | Passwordless SSH from the Hermes host to the Mac (`ssh <host> true` must succeed) |

The runner is a macOS LaunchAgent, so the Mac side is macOS-only by design.

---

## Human quickstart

**1 — On the Mac.** Clone, check the machine, install:

```sh
git clone https://github.com/rawprogress/hermes-claude-sdk.git ~/Projects/hermes-claude-sdk
cd ~/Projects/hermes-claude-sdk
uv run hermes-claude-runner doctor      # read-only: ends with READY TO INSTALL
./scripts/install_runner.sh             # verifies, installs, starts the LaunchAgent
```

`install_runner.sh` is idempotent: it backs up anything it replaces with a timestamped
`.bak-…` copy and never deletes state. `--dry-run` shows what it would do, `--skip-verify`
trusts an already-green suite, `--json` prints one machine-readable object.

**2 — On the Hermes host.** Copy the plugin over and enable it:

```sh
scp <your-mac-ssh-host>:Projects/hermes-claude-sdk/scripts/install_plugin_on_surface.sh /tmp/
MAC_HOST=<your-mac-ssh-host> sh /tmp/install_plugin_on_surface.sh
```

The first command fetches the installer from the checkout you just made on the Mac. The
second runs it on the Hermes host; it copies the plugin from that same checkout, validates
all seven registrations and checks the runner over SSH.

**3 — Tell the plugin where your Mac is.** In `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled: [hermes-claude-sdk]
  entries:
    hermes-claude-sdk:
      settings:
        ssh_host: my-mac                              # your ssh destination
        remote_command: ~/.local/bin/hermes-claude-runner
        timeout_seconds: 240                          # 240 is also the enforced floor
```

`ssh_host` is the only setting most people need to change. `remote_command` is
home-relative by default, so it resolves for whichever account runs the runner.

---

## Verify it works

On the Mac:

```sh
uv run pytest -q                                   # unit + integration tests
uv run ruff check .                                # lint
uv run mypy                                        # typecheck
uv run hermes-claude-runner doctor                 # ends with READY
~/.local/bin/hermes-claude-runner health           # {"ok":true,…}
```

On the Hermes host:

```sh
hermes plugins doctor ~/.hermes/plugins/hermes-claude-sdk --ci   # 7 tool(s)
ssh -o BatchMode=yes <host> '~/.local/bin/hermes-claude-runner' health
```

Then ask Hermes to start a real run and read the events back. A run is only proven when the
events show the tests Claude ran and the commit it made — never on the strength of a status
alone.

---

## Using the seven tools

Each tool returns a JSON string. Arguments below are exactly the tool schemas.

### `claude_start` — begin a run

```json
{"project": "demo", "prompt": "tests/test_add.py fails because add() is wrong. Fix the implementation, keep the test as it is, run pytest -q, and commit locally.", "role": "implementer", "create_worktree": true}
```

`project` is a path or a bare directory name under the Mac's projects root (`~/Projects` by
default). `role` defaults to `"implementer"`, `create_worktree` to `true`.

```json
{"ok": true, "result": {"run_id": "r3f9c1a2b7d4e6081235", "status": "queued",
 "worktree": "/Users/me/Projects/.hermes-claude-worktrees/demo/r3f9c1a2b7d4e6081235",
 "branch": "hermes/3f9c1a2b", "base_sha": "9662e58…", "mode": "worktree",
 "project": "/Users/me/Projects/demo", "role": "implementer"}}
```

### `claude_status` — where is it?

```json
{"run_id": "r3f9c1a2b7d4e6081235"}
```

```json
{"ok": true, "result": {"run_id": "r3f9c1a2b7d4e6081235", "status": "working",
 "claude_session_id": "0b7e…", "worktree": "…", "branch": "hermes/3f9c1a2b",
 "base_sha": "9662e58…", "activity": {"kind": "tool_use", "seq": 12, "at": "…"},
 "result": null, "error": null, "event_high_water": 12, "pending_messages": 0}}
```

### `claude_events` — what happened

```json
{"run_id": "r3f9c1a2b7d4e6081235", "after": 0, "limit": 100}
```

Page forward by passing the returned `next_cursor` as the next `after` — don't re-read from
zero.

```json
{"ok": true, "result": {"run_id": "r3f9c1a2b7d4e6081235",
 "events": [{"seq": 1, "kind": "run_created", "created_at": "2026-08-24T09:14:02.481Z",
             "payload": {"project": "/Users/me/Projects/demo", "role": "implementer"}}],
 "next_cursor": 12, "high_water": 12, "truncated": false}}
```

### `claude_send` — answer or steer a live run

```json
{"run_id": "r3f9c1a2b7d4e6081235", "message": "Also add a test for the negative case."}
```

Delivered to the same Claude conversation between turns.

```json
{"ok": true, "result": {"queued": true, "run_id": "r3f9c1a2b7d4e6081235",
 "status": "working", "pending_messages": 1}}
```

Once the run has finished this returns `run_not_live` — use `claude_resume` instead.

### `claude_list` — what is going on

```json
{"project": "demo", "status": "working", "limit": 50}
```

All three are optional.

```json
{"ok": true, "result": {"runs": [{"run_id": "…", "project": "…", "status": "working",
 "branch": "hermes/3f9c1a2b", "claude_session_id": "0b7e…", "created_at": "…"}],
 "count": 1, "limit": 50}}
```

### `claude_stop` — stop without losing anything

```json
{"run_id": "r3f9c1a2b7d4e6081235"}
```

```json
{"ok": true, "result": {"run_id": "r3f9c1a2b7d4e6081235", "status": "working",
 "stop_requested": true, "signalled": true, "already_final": false, "worktree": "…"}}
```

The worktree, the branch, the Claude session id and every event survive.

### `claude_resume` — pick the same conversation back up

```json
{"run_id": "r3f9c1a2b7d4e6081235", "message": "Now add the same handling for empty input."}
```

Works on a `stopped`, `failed`, `blocked`, `completed` or `unknown` run. Never resets,
cleans or stashes anything.

```json
{"ok": true, "result": {"run_id": "r3f9c1a2b7d4e6081235", "status": "queued",
 "claude_session_id": "0b7e…", "worktree": "…", "branch": "hermes/3f9c1a2b",
 "base_sha": "9662e58…", "resumed": true}}
```

---

## Troubleshooting

| Symptom | What it means | What to do |
| --- | --- | --- |
| `ssh_failed` / `ssh_timeout` | The Hermes host cannot reach the Mac | `ssh <host> true`; check the plugin's `ssh_host` / `remote_command` settings |
| `daemon_unavailable` | The LaunchAgent is not listening | `launchctl kickstart -k gui/$(id -u)/com.hermes-claude-sdk.runner`, then `hermes-claude-runner health` |
| `invalid_project` | The path is outside the projects root, or escapes it via a symlink | Use a repository under `~/Projects` on the Mac |
| `not_a_git_repository` | The path is contained, but there is no git checkout there | `git init` it, or point at the repository root |
| `secret_in_payload` | The prompt contained credential-shaped text | Pass secrets through the environment or a file on the Mac, never through Hermes |
| `response_too_large` | The answer exceeded the transport cap | Ask for fewer events (`limit`) |
| Status `unknown` | The worker vanished with no final result — never a success | Read `claude_events`, then `claude_resume` |
| Status `failed` | The SDK reported an error, or the worker crashed | `claude_events` shows the `error` event; `claude_resume` retries in the same session |
| Status `blocked` | An escalated tool is waiting for an answer | Read the `blocked` event, then `claude_send(run_id, "<your answer>")`. Does not occur with the default escalation set — see [Security](#security) |
| `worktree_failed` | The target directory exists and is not a worktree | Inspect it by hand; the runner will not reset anything |

### Every error code

The runner's codes are part of the wire contract — Hermes branches on them, so they are
never renamed silently.

| Code | Meaning |
| --- | --- |
| `invalid_request` | stdin was not a single JSON object |
| `invalid_action` | unknown or missing `action` |
| `invalid_params` | a parameter failed type or range validation |
| `unknown_run` | no run with that id |
| `invalid_project` | missing, outside the projects root, or escaping it via a symlink |
| `not_a_git_repository` | inside the projects root, but not a git checkout |
| `worktree_failed` | `git worktree add` or `rev-parse` failed |
| `daemon_unavailable` | the LaunchAgent daemon is not accepting connections |
| `run_not_resumable` | the run is still live, or has no session to resume |
| `no_claude_session` | resume requested before a Claude session id was captured |
| `worker_spawn_failed` | the daemon could not launch the worker process |
| `secret_in_payload` | the prompt or message carried credential-shaped material |
| `run_not_live` | `claude_send` needs a live worker; this run has finished |
| `internal_error` | unexpected failure; the detail is redacted |

The plugin adds its own, for failures that never reach the Mac: `ssh_timeout`,
`ssh_failed`, `ssh_unavailable`, `bad_response`, `response_too_large`, `plugin_error`.

Start every diagnosis with `uv run hermes-claude-runner doctor` — it names the missing
piece and the command that fixes it.

**Logs.** `~/Library/Logs/HermesClaudeRunner/{stdout,stderr}.log` for the daemon,
`worker-<run-id>.log` for each run.
**State.** `~/Library/Application Support/HermesClaudeRunner/data.db`.

A worker that dies is reported as `working` until the next reconciliation sweep (at most
30 s), then becomes `unknown`. `unknown` is never reported as success.

---

## Security

Read [SECURITY.md](SECURITY.md) before installing. The short version:

- **No API keys.** The runner never reads, copies, injects or prints `ANTHROPIC_API_KEY`.
  Claude uses the Mac's existing Claude Code login.
- **Containment.** A project must resolve — through symlinks — to a git repository under the
  configured projects root (`~/Projects` by default), and its git object store must live
  there too. Anything else fails closed as `invalid_project`.
- **Your checkout is never touched** when `create_worktree=true`. Work happens in
  `~/Projects/.hermes-claude-worktrees/<repo>/<run-id>` on branch `hermes/<short-run-id>`.
- **Nothing is ever deleted, reset, cleaned or stashed** by this project.
- **Runs are unattended, so they bypass permission prompts.** `permission_mode` is
  `bypassPermissions` plus `--dangerously-skip-permissions`, and `user`/`project`/`local`
  settings are loaded, so Claude behaves like your normal Claude Code — `CLAUDE.md`, skills,
  hooks, MCP servers and subagents included. **Install this only on a machine whose projects
  you are willing to let an agent modify unattended.**
- **Escalation is prepared, but nothing escalates today.** Because `can_use_tool` is
  auto-approved under bypass, the runner installs a **PreToolUse** hook instead, verified
  live to fire with `bypassPermissions`. A tool in the escalation set puts the run into
  `blocked` and waits up to 15 minutes for a `claude_send` answer.

  **Limitation:** the default escalation set is `AskUserQuestion`, and Claude Agent SDK
  0.2.144 does not expose `AskUserQuestion` in an SDK session's init tool list — the model
  cannot call it, so **the `blocked` status does not currently occur in normal operation**.
  The machinery activates the day the SDK offers the tool, or as soon as you configure
  another tool into the escalation set.
  `tests/runner/test_live_sdk.py::test_ask_user_question_availability_is_recorded` fails
  when that changes.
- **Credential-shaped payloads are refused, not rewritten.** A prompt or message containing
  token material (`sk-ant-…`, `ghp_…`, `AKIA…`, JWTs, PEM private keys) is rejected with
  `secret_in_payload` before anything is written to the database.
- The daemon socket is mode `0700` (`srwx------`) and reachable only by its owner.

---

## Update

```sh
cd ~/Projects/hermes-claude-sdk
git pull
./scripts/install_runner.sh                 # re-verifies, rewrites, restarts, backs up
```

Then refresh the plugin on the Hermes host:

```sh
MAC_HOST=<your-mac-ssh-host> ./scripts/install_plugin_on_surface.sh
```

Both are idempotent. Existing runs, worktrees and the database survive an update.

---

## Uninstall

```sh
./scripts/uninstall.sh
```

Stops the LaunchAgent and moves it and the wrapper aside with a timestamp. The database,
the worktrees and the logs are preserved on purpose — remove them by hand only if you
really mean to. On the Hermes host, remove `~/.hermes/plugins/hermes-claude-sdk` and drop
the entry from `~/.hermes/config.yaml`.

If you installed before the LaunchAgent label was renamed, set the old label first:

```sh
HERMES_CLAUDE_RUNNER_LABEL=com.example.old-label ./scripts/uninstall.sh
```

---

## Agent Quickstart

For a coding agent installing this on someone's behalf. The full contract, including
machine-readable success criteria and rollback, is in
**[INSTALL_FOR_AGENTS.md](INSTALL_FOR_AGENTS.md)**. Read it before running anything.

```sh
# 1. Preflight — read-only, creates nothing, safe on any machine.
uv run hermes-claude-runner doctor --json          # branch on .installable

# 2. Dry run — shows every file that would change, changes none.
./scripts/install_runner.sh --dry-run --json

# 3. Install — idempotent, backs up whatever it replaces.
./scripts/install_runner.sh --json

# 4. Confirm.
uv run hermes-claude-runner doctor --json          # .ok == true
```

Before installing, branch on `.installable`: on a first install `.ok` is `false` because
the service is precisely what is missing. `.ok == true` is the criterion afterwards.

**Stop and hand back to a human for:** signing into Claude Code (OAuth in a browser),
approving SSH key setup or host-key acceptance, and editing `~/.hermes/config.yaml`. An
agent must never push, publish, create a remote, or delete a worktree, a database or a log.

---

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) for the workflow, [AGENTS.md](AGENTS.md) for the
conventions this codebase holds itself to, [CHANGELOG.md](CHANGELOG.md) for what changed.

## Credits

Built by **Katso** and **rawprogress** with **Claude Code** — which is also the thing it
drives, so the project was largely written by the agent it orchestrates.

MIT licensed — see [LICENSE](LICENSE).
