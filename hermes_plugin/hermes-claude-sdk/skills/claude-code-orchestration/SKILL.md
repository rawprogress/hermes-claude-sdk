---
name: claude-code-orchestration
description: How to drive Claude Code on the Mac from Hermes with the claude_* tools. Read before starting, steering, inspecting, stopping or resuming a Claude run.
---

# Orchestrating Claude Code from Hermes

**Hermes orchestrates. Claude does all coding, tests, lint, typecheck, build and reviews.**

You decide *what* should happen and verify that it did. You do not edit files, run
test suites, run linters or type checkers, or produce build artefacts for the target
repository yourself — you hand that work to Claude and check the evidence it reports.

## The seven tools

| Tool | Use it for |
| --- | --- |
| `claude_start` | Begin a run: project, prompt, optional role and worktree flag. |
| `claude_send` | Queue a follow-up or answer for a live run. |
| `claude_status` | Durable status, Claude session id, worktree, result or error. |
| `claude_events` | Ordered events: assistant text, tool activity, results. |
| `claude_list` | Recent and active runs. |
| `claude_stop` | Controlled stop that preserves worktree, branch and session. |
| `claude_resume` | Respawn a finished run by its stored Claude session id. |

## A normal run

1. `claude_start(project, prompt)` — state the definition of done in the prompt
   ("the failing test passes, `pytest -q` is green, commit locally"). Keep the
   returned `run_id`.
2. Poll `claude_status(run_id)`. Statuses are
   `queued → preparing → working → completed | blocked | failed | stopped | unknown`.
3. Read progress with `claude_events(run_id, after=<last next_cursor>)`. Page forward
   with the cursor rather than re-reading from zero.
4. When the status is `completed`, verify the claim: the events should show the tests
   Claude ran and the commit it made. Do not report success on the strength of the
   status alone.

## Rules that matter

- **`unknown` is not failure and not success.** It means the worker process vanished
  without a final result. Investigate with `claude_events`; resume if appropriate.
- **`blocked` means an escalated tool is waiting for you.** The `blocked` event carries
  the question and its options verbatim — read it with `claude_events`, then answer the
  substance with `claude_send(run_id, "<your answer>")`. The answer is handed straight
  back to Claude. An unanswered question is released after about 15 minutes and Claude
  proceeds on its own judgement, so answer promptly.

  In practice you will not see this status yet. The escalation set defaults to
  `AskUserQuestion`, and Claude Agent SDK 0.2.144 does not offer it in an SDK
  session's init tool list, so the model cannot call it and the blocked path **does
  not currently occur**. The PreToolUse escalation machinery is in place and tested,
  ready for a future SDK that exposes the tool or for another tool configured into the
  escalation set. Until then, treat a run as never needing your input mid-turn: put
  everything Claude needs into the prompt.
- **Stopping preserves everything.** Worktree, branch, Claude session id and events
  survive, so `claude_resume` can pick the same conversation back up.
- **Never ask Claude to reset, clean or stash.** Runs work in their own git worktree
  on branch `hermes/<short-run-id>`; the human checkout is never touched.
- **One prompt, one deliverable.** Follow-ups belong in `claude_send` or
  `claude_resume`, not in a second `claude_start` against the same work.

## Writing a good prompt

State the repository context, the change, and the verification Claude must run —
for example: "In `demo`, `add()` returns the wrong value and `tests/test_add.py`
fails. Fix the implementation, keep the test as it is, run `pytest -q`, and commit
locally with a clear message." Claude has the repository's own `CLAUDE.md`, skills,
hooks and MCP servers available, so repeat only what is specific to this task.
