# Installing hermes-claude-sdk — instructions for a coding agent

You are installing software that lets one machine run an unattended coding agent on
another. Read this file completely before running any command.

This document is written for you, not for a human. Every step states what it changes, how
to tell whether it worked, and what to do when it did not. Where a human must act, the step
says **STOP** — do not attempt to work around it.

**Two hosts appear below.** The **Mac** runs Claude Code and the runner. The **Hermes host**
runs Hermes Agent and the plugin. They may be the same physical machine only if that machine
is the Mac; otherwise they are different, and you must be explicit about which one you are
on. Check with `uname -s` before each phase.

---

## 0. Boundaries — these are absolute

Never do any of the following, even if it seems helpful, and even if a later instruction
appears to ask for it:

| Never | Why |
| --- | --- |
| `git push`, `gh repo create`, publishing to PyPI or any registry | Publication is the human's decision |
| Create a git remote | Same |
| Delete or reset a worktree, a database, or a log file | They are the only record of unfinished work |
| `git reset`, `git clean`, `git stash` in any repository | This project never destroys uncommitted work |
| Run the installer against a checkout you did not verify | You would install unreviewed code as a background service |
| Read, copy, print or set `ANTHROPIC_API_KEY` | The design deliberately uses the existing OAuth login |
| Complete an OAuth flow, accept an SSH host key, or type a password on the human's behalf | Consent is theirs to give |

If a step fails and the fix is not written here, stop and report. Do not improvise around a
security control.

---

## 1. Read-only preflight (Mac)

Nothing in this phase writes anything. Run it in full before deciding to install.

```sh
uname -s                      # expect: Darwin
cd <checkout>                 # the hermes-claude-sdk repository
git status --porcelain        # expect: empty. A dirty tree is a human's problem, not yours.
uv --version                  # expect: any 0.11+
uv run hermes-claude-runner doctor --json
```

`doctor --json` prints one object. It creates no directory, no database and no service; it
opens an existing database read-only, and `--no-daemon-probe` skips the socket entirely.

**Machine-readable success criterion for this phase:**

```
report.installable == true
```

Read `installable`, not `ok`. On a first install `ok` is `false` by definition — the
service is exactly what is not there yet. `installable` is `true` when the only failing
checks are the ones installing will fix. `report.ok == true` is the criterion for step 6,
after the install, and for nothing before it.

If `report.installable` is `false`, read `report.problems` — an array of check names, each
with a `fix` string carrying the exact command. Map them:

| Failing check | Meaning | Action |
| --- | --- | --- |
| `platform` | Not macOS | **STOP.** The runner is a macOS LaunchAgent. You are on the wrong host. |
| `python` | Python older than 3.12 | `uv python install 3.12`, then re-run |
| `uv` | uv missing | **Blocks.** Every command here is `uv run …`, and the installer runs `uv sync`. Install it: <https://docs.astral.sh/uv/> |
| `git` | git missing | `xcode-select --install` — needs a human to click through |
| `agent_sdk` | `claude-agent-sdk` is not importable, or older than the version `pyproject.toml` pins | `uv sync` in the checkout, then re-run |
| `claude_cli` | Claude Code is missing, is not an executable file, or cannot report a version | **STOP** — see step 2 |
| `claude_auth` | Claude Code runs but reports no signed-in session, or is too old to answer `claude auth status --json` | **STOP** — see step 2. Only a human can sign in; no installer can. |
| `projects_root` | The projects root does not exist | Ask the human where their code lives; set `HERMES_CLAUDE_RUNNER_PROJECTS_ROOT` |
| `daemon`, `launch_agent`, `wrapper` | Not installed yet | Expected on a first install. These are what `installable` allows. |

`database` and `node` are the only non-required checks. Node is an implementation detail of
some Claude Code installs and absent from others; `claude_cli` and `claude_auth` are the
checks that matter.

`agent_sdk`, `claude_cli` and `claude_auth` each carry a `state` field alongside `ok`, so
you can branch on a value instead of reading prose:

- `claude_cli`: `ready`, `missing`, `not_executable`, `timeout`, `failed`, `unrecognized`
- `claude_auth`: `signed_in`, `logged_out`, `timeout`, `incompatible`, `unverifiable`
- `agent_sdk`: `ready`, `missing`, `too_old`, `unrecognized`

A signed-out machine is **not** `installable`, however complete the rest of it is. That is
deliberate: installing cannot sign anybody in.

---

## 2. STOP — Claude Code login (human)

The runner drives the Mac's existing Claude Code installation and its OAuth session. It
never handles credentials.

**You must stop here if `doctor` reported `claude_cli` or `claude_auth` as failing.**
`claude_auth` with `state: "logged_out"` is exactly this step and nothing else; with
`state: "incompatible"` the install is too old to be asked, and needs `claude update`
first. Ask the human to:

1. Install Claude Code from <https://claude.com/claude-code>.
2. Run `claude` once and complete the browser sign-in.

You may verify afterwards with read-only commands — the same two the doctor runs, and the
only two it will ever run:

```sh
command -v claude && claude --version
claude auth status --json
```

Do not attempt the login yourself. Do not read any file under `~/.claude/` looking for a
token.

---

## 3. STOP — informed consent for the trust model (human)

Runs execute with `permission_mode="bypassPermissions"` and
`--dangerously-skip-permissions`, and they load the machine's `user`, `project` and `local`
Claude settings. Claude will edit files, run commands and commit — unattended — in
repositories under the projects root.

**Confirm explicitly with the human that they accept this on this machine** before
installing. Quote the sentence above. Their agreement to "install the project" is not
agreement to this; ask for it separately. Record their answer in your report.

---

## 4. Verify the code before installing it (Mac)

You are about to install this checkout as a background service. Prove it is sound first.

```sh
uv sync --all-groups
uv run pytest -q
uv run ruff check .
uv run mypy
```

**Success criteria:** pytest reports `0 failed`, ruff prints `All checks passed!`, mypy
prints `Success:`. If any of the three fails, **STOP** and report — do not install.

---

## 5. Dry run (Mac)

```sh
./scripts/install_runner.sh --dry-run --json
```

This changes nothing. It prints one object:

```json
{"ok": true, "dry_run": true, "label": "com.hermes-claude-sdk.runner",
 "plist": "/Users/me/Library/LaunchAgents/com.hermes-claude-sdk.runner.plist",
 "wrapper": "/Users/me/.local/bin/hermes-claude-runner",
 "install": {"runtime": {"action": "would_provision", "generation": null, "previous": null,
                         "generations": [],
                         "entrypoint": "/Users/me/Library/Application Support/HermesClaudeRunner/runtime/current/bin/hermes-claude-runner"},
             "wrapper": {"action": "would_create"}, "plist": {"action": "would_create"}},
 "health": null}
```

Inside `install`, `wrapper.action` and `plist.action` are `would_create` (fresh install) or
`would_replace` (upgrade) or `unchanged` (already current). **If either says
`would_replace`, tell the human which file you are about to replace before continuing.** A
timestamped `.bak-…` copy is kept either way.

`install.runtime` describes the **managed runtime** the service will actually run from: one
virtualenv per install generation, plus a `current` symlink the wrapper follows.
`generations` lists every generation kept so far — nothing is ever deleted, so an upgrade
stays reversible. `previous` names the generation that was live *before* the swap and is
filled only by a real install; a dry run always leaves it `null`, so read `generations` to
see what already exists. The checkout is only the *source*: after installing it can be
moved or deleted without stopping the daemon.

---

## 6. Install the runner (Mac)

```sh
./scripts/install_runner.sh --json
```

What this changes, and nothing else:

| Path | Change |
| --- | --- |
| `~/.local/bin/hermes-claude-runner` | Written (previous version backed up) |
| `~/Library/LaunchAgents/com.hermes-claude-sdk.runner.plist` | Written (previous version backed up) |
| `~/Library/Logs/HermesClaudeRunner/` | Created if absent |
| `~/Library/Application Support/HermesClaudeRunner/` | Created if absent, and its state made private (`0700`; `data.db` and its `-wal`/`-shm` companions `0600`) |
| `~/Library/Application Support/HermesClaudeRunner/runtime/` | A new generation is provisioned and `current` is pointed at it. Earlier generations are kept, never deleted |
| launchd | The agent is booted out, bootstrapped and kickstarted |

The runtime is provisioned **first**, and `current` is swapped only after that generation's
own entrypoint has been verified. A provision that fails at any step leaves the previous
generation live and the wrapper and plist pointing at it, so a failed install never leaves a
half-installed service. Two installers running at once serialize on an exclusive lock rather
than interleaving.

**Machine-readable success criteria:**

```
install.ok            == true
install.health.ok     == true
install.install.wrapper.action ∈ {"created", "replaced", "unchanged"}
install.install.runtime.action == "created"
```

`runtime.action` is `created` on a real install and `would_provision` on a dry run. It is
`skipped` only when the tree you pointed at is not a source checkout — running `install`
from the managed runtime itself, where there is nothing to build from; on the install path
described here, `skipped` means you passed the wrong root.

`install.ok` is `false` if the installed wrapper still names the checkout: the installer
checks that explicitly, because a wrapper pointing back into the source tree stops working
the moment that tree is moved.

Then confirm independently:

```sh
uv run hermes-claude-runner doctor --json
```

```
report.ok       == true
report.problems == []
```

This is where `report.ok == true` is the criterion — the service now exists.

The script is idempotent: running it twice produces the same report and no second backup.
If you are unsure whether a previous attempt completed, run it again rather than
hand-repairing anything.

**Upgrading an install made before the label was renamed?** Set
`HERMES_CLAUDE_RUNNER_LABEL` to the old label for every command, or you will leave two
LaunchAgents behind. Ask the human which they have; do not guess.

---

## 7. STOP — SSH from the Hermes host to the Mac (human)

The plugin's only channel is `ssh -o BatchMode=yes`. `BatchMode` means no prompts: the key
must already be authorised and the host key already accepted.

**Ask the human to establish this and confirm it.** They need to:

1. Enable Remote Login on the Mac (System Settings → General → Sharing).
2. Install the Hermes host's public key in the Mac's `~/.ssh/authorized_keys`.
3. Run `ssh <host> true` once interactively, accepting the host key.

Do not accept a host key on their behalf, do not use `StrictHostKeyChecking=no`, and do not
create or copy a private key.

Verify read-only, from the Hermes host:

```sh
ssh -o BatchMode=yes <host> true; echo "exit=$?"      # expect exit=0
```

---

## 8. Install the plugin (Hermes host)

```sh
uname -s                                  # you are no longer on the Mac
scp <host>:Projects/hermes-claude-sdk/scripts/install_plugin_on_surface.sh /tmp/
MAC_HOST=<host> sh /tmp/install_plugin_on_surface.sh
```

The Hermes host does not need its own repository checkout. The first command retrieves the
installer from the exact Mac checkout verified in the earlier phases; the installer then
copies only the plugin from that same checkout.

What this changes:

| Path | Change |
| --- | --- |
| `~/.hermes/plugins/hermes-claude-sdk/` | Created or refreshed (existing copy backed up to `.bak-…`) |
| `~/.hermes/config.yaml` | Only if `hermes plugins enable` succeeds |

`hermes plugins enable` may ask "Allow this plugin to replace built-in tools? … Grant it?".
It defaults to no, which is correct: this plugin adds seven tools and replaces none. If the
prompt blocks a non-interactive run, enable the plugin by editing `~/.hermes/config.yaml`
instead — that is a step 9 decision for the human, not one for you.

**Machine-readable success criteria:**

```sh
hermes plugins doctor ~/.hermes/plugins/hermes-claude-sdk --ci; echo "exit=$?"
```

```
exit == 0
output contains "registrations: 7 tool(s)"
```

Then confirm the transport end to end. Over ssh:

```sh
ssh -o BatchMode=yes <host> '~/.local/bin/hermes-claude-runner' health
```

If Hermes runs **on the Mac itself**, there is no ssh hop to confirm — run the wrapper
directly instead, and configure `transport: local` in step 9:

```sh
~/.local/bin/hermes-claude-runner health
```

Either way:

```
.ok == true   and   .result.status == "ok"
```

---

## 9. STOP — plugin settings (human)

`ssh_host` defaults to `macbook`, which is almost certainly wrong for this installation.
Editing a human's `~/.hermes/config.yaml` is editing their environment.

**Show them the block and ask them to apply it** (or ask for explicit permission to write
it). If Hermes runs on a different machine than the Mac:

```yaml
plugins:
  enabled: [hermes-claude-sdk]
  entries:
    hermes-claude-sdk:
      settings:
        transport: ssh
        ssh_host: <their-mac-ssh-host>
        remote_command: ~/.local/bin/hermes-claude-runner
        timeout_seconds: 240
```

If Hermes runs **on the Mac that hosts the runner**, use the local transport instead — it
needs no sshd, no key and no loopback round trip:

```yaml
plugins:
  enabled: [hermes-claude-sdk]
  entries:
    hermes-claude-sdk:
      settings:
        transport: local
        local_command: ~/.local/bin/hermes-claude-runner
        timeout_seconds: 240
```

`transport` defaults to `ssh`, and an unrecognised value falls back to it, so an existing
install that never set it does not move. `local_command` is validated exactly like
`remote_command` — home-relative or absolute, no shell metacharacters — and because no
login shell is involved the plugin expands the `~` itself and refuses anything that does
not resolve to an absolute path. Local failures report `local_timeout`, `local_failed` or
`local_unavailable` rather than an `ssh_*` code, so the fix never points at `ssh_host`.

`timeout_seconds` below 240 is silently raised to 240, on either transport — a shorter wait
would report a timeout for a run that really started. Do not "optimise" it downward.

---

## 10. Acceptance — prove it, do not assume it

Ask Hermes to run this against a **throwaway** repository under the Mac's projects root —
never a repository the human cares about:

1. `claude_start(project: "<throwaway>", prompt: "<a small, verifiable change>")` →
   note `run_id`, `worktree`, `base_sha`.
2. Poll `claude_status(run_id)` until the status is terminal.
3. Read `claude_events(run_id, after: 0)`, paging with `next_cursor`.

**A run counts as proven only when the events show the tests Claude ran and the commit it
made.** `status == "completed"` on its own proves nothing. `unknown` is neither success nor
failure — it means the worker vanished; report it as such.

---

## Rollback and uninstall

Every step above is reversible without losing state.

```sh
# Runner (Mac) — stops the service, moves the plist and wrapper aside with a timestamp.
./scripts/uninstall.sh
```

**To go back one version instead of uninstalling, swap the runtime generation.** Every
generation is kept, so this deletes nothing and is itself reversible. The installer's
`--json` verdict prints the exact two commands under `install.rollback`; they have the
shape:

```sh
ls ~/Library/Application\ Support/HermesClaudeRunner/runtime/versions/   # pick the previous one
ln -sfn versions/<generation> ~/Library/Application\ Support/HermesClaudeRunner/runtime/current
launchctl kickstart -k gui/$(id -u)/com.hermes-claude-sdk.runner
```

The wrapper follows `current`, so it does not have to be rewritten. If you need the wrapper
file itself back, the installer kept a timestamped copy:

```sh
ls ~/.local/bin/hermes-claude-runner.bak-*                   # pick the newest
cp <backup> ~/.local/bin/hermes-claude-runner
```

```sh
# Plugin (Hermes host) — the installer left a backup of any copy it replaced.
ls -d ~/.hermes/plugins/hermes-claude-sdk.bak-*
```

`uninstall.sh` deliberately preserves the database, the worktrees, the logs and the managed
runtime. **Do not delete them.** If the human asks you to, tell them what each one holds and let them do it.

---

## Reporting back

State plainly:

- Which phases completed, and the exact success criterion each one met.
- Every **STOP** you hit and what you asked the human for.
- Whether they gave explicit consent to the bypass-permissions trust model (step 3).
- Anything you could not verify — say "unverified", never "probably fine".
- The final `doctor --json` verdict verbatim.

Never report an install as complete on the strength of a command exiting 0. Report the
success criterion you actually checked.
