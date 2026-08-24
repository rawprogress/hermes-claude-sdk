# Security

## Reporting a vulnerability

Please report security issues privately — open a GitHub security advisory on the repository
rather than a public issue, and give the maintainers a reasonable window to respond before
disclosing.

Include what an attacker can do, the smallest reproduction you have, and the affected
version (`hermes-claude-runner version`).

## What this software does, stated plainly

It lets a remote agent start unattended Claude Code sessions on your Mac, in your
repositories, with permission prompts bypassed. **Install it only on a machine whose
projects you are willing to let an agent modify without being asked.** That is the design,
not a defect — but it is the thing to weigh before installing.

## The trust model

Runs use `permission_mode="bypassPermissions"` together with
`--dangerously-skip-permissions`, and load the `user`, `project` and `local` Claude setting
sources. Claude therefore behaves exactly like your normal Claude Code, including your
`CLAUDE.md`, skills, hooks, MCP servers and subagents. Anything your Claude Code can reach,
a run can reach.

Whoever can invoke the Hermes tools can start such a run. Treat access to the Hermes host,
and the SSH key that reaches the Mac, as equivalent to sitting at the Mac.

## Controls that are actually enforced

| Control | Enforcement |
| --- | --- |
| **No API keys** | The runner never reads, copies, injects or prints `ANTHROPIC_API_KEY`. Authentication is the Mac's existing OAuth session. |
| **Project containment** | A project must resolve — after symlink resolution — to a git repository under the configured projects root, *and* its git object store (`--git-common-dir`) must live there too, so a linked worktree cannot commit into an uncontained repository. Nesting inside the runner's own worktree area is refused. |
| **Your checkout is never touched** | With `create_worktree=true` (the default) work happens in `~/Projects/.hermes-claude-worktrees/<repo>/<run-id>` on a fresh `hermes/<short-run-id>` branch, from a recorded base SHA. |
| **Nothing is destroyed** | No code path resets, cleans, stashes or deletes a worktree, a database or a log. |
| **Fixed argv, never a shell** | Both the plugin's ssh invocation and every git call are argv lists. `ssh_host` and `remote_command` are matched against strict patterns — a value that could smuggle `-oProxyCommand` or a second argument is refused and the documented default is used instead. |
| **Credential-shaped payloads are refused** | `claude_start`, `claude_send` and `claude_resume` reject a prompt containing `sk-ant-…`, `sk-…`, `ghp_`/`github_pat_`, `AKIA…`, `xox…`, JWTs or PEM private-key blocks with `secret_in_payload`, *before* anything is persisted. Redacting instead would hand Claude a corrupted prompt. |
| **Model output is redacted** | Event payloads, stored `result`/`error` columns and prompt summaries pass through broader scrubbing (including `NAME=value` shapes) and length caps. Chain of thought is stored as a character count only. |
| **Local-only transport** | The daemon listens on a unix socket created under `umask 0o077`, so it is mode `0700` (`srwx------`) and reachable only by its owner. Nothing listens on a TCP port. |
| **Bounded escalation** | Under bypass, a PreToolUse hook intercepts tools in the escalation set, marks the run `blocked` and waits for an explicit answer. See the limitation below. |

## Known limitations

- **The `blocked` path does not currently trigger.** The default escalation set is
  `AskUserQuestion`, which Claude Agent SDK 0.2.144 does not expose to SDK sessions. The
  machinery is tested and ready; it activates when the SDK offers the tool or when you
  configure another tool into the set.
- **The functional prompt is stored verbatim.** `runs.prompt` and mailbox bodies are not
  redacted — that is why the refusal gate above exists. Pass secrets through the
  environment or a file on the Mac, never through a Hermes tool call.
- **Anyone who can write the plugin's settings chooses the ssh destination.** The value is
  pattern-checked, but a valid hostname is a valid hostname. Protect `~/.hermes/config.yaml`
  as you would an SSH config.
- **The Mac's OAuth session is used unattended.** Runs consume the account's quota and act
  with its identity.

## Passing secrets to a run

Don't put them in the prompt. Put them in the environment of the Mac, or in a file the
worktree can read, and refer to them by name in the prompt.
