"""JSON schemas for the seven Claude orchestration tools.

Kept separate from the handlers so the model-facing contract is reviewable
on its own.
"""

from __future__ import annotations

RUN_ID = {
    "type": "string",
    "description": "Run id returned by claude_start or claude_list.",
}

CLAUDE_START_SCHEMA = {
    "name": "claude_start",
    "description": (
        "Start a Claude Code run on the Mac. Claude does the actual coding, tests, "
        "lint, typecheck, build and review work; you orchestrate. Returns run_id, "
        "status, worktree and base_sha immediately — poll claude_status/claude_events "
        "for progress."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": (
                    "Repository under the runner's projects root (~/Projects on the "
                    "Mac by default) — an absolute path or just the directory name."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "What Claude should do. Be specific and state the "
                    "definition of done."
                ),
            },
            "role": {
                "type": "string",
                "default": "implementer",
                "description": "Label for this run, e.g. implementer or reviewer.",
            },
            "create_worktree": {
                "type": "boolean",
                "default": True,
                "description": (
                    "True (default) isolates the run in a git worktree on branch "
                    "hermes/<short-run-id>. False works directly in the checkout."
                ),
            },
        },
        "required": ["project", "prompt"],
    },
}

CLAUDE_SEND_SCHEMA = {
    "name": "claude_send",
    "description": (
        "Queue a follow-up message or an answer for a live run. It is delivered to the "
        "same Claude conversation between turns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": RUN_ID,
            "message": {"type": "string", "description": "The follow-up or answer."},
        },
        "required": ["run_id", "message"],
    },
}

CLAUDE_STATUS_SCHEMA = {
    "name": "claude_status",
    "description": (
        "Durable status of one run: lifecycle status, Claude session id, worktree, "
        "base sha, current activity, result or error, and the event high-water mark."
    ),
    "parameters": {
        "type": "object",
        "properties": {"run_id": RUN_ID},
        "required": ["run_id"],
    },
}

CLAUDE_EVENTS_SCHEMA = {
    "name": "claude_events",
    "description": (
        "Ordered structured events for one run: assistant messages, tool activity and "
        "result summaries. Pass the returned next_cursor as 'after' to page forward."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": RUN_ID,
            "after": {
                "type": "integer",
                "default": 0,
                "description": "Return events with a sequence number greater than this.",
            },
            "limit": {
                "type": "integer",
                "default": 100,
                "description": "Maximum number of events to return (capped by the runner).",
            },
        },
        "required": ["run_id"],
    },
}

CLAUDE_LIST_SCHEMA = {
    "name": "claude_list",
    "description": "List recent and active Claude runs, newest first.",
    "parameters": {
        "type": "object",
        "properties": {
            "project": {
                "type": "string",
                "description": "Only runs for this repository (path or directory name).",
            },
            "status": {
                "type": "string",
                "enum": ["queued", "preparing", "working", "completed",
                         "blocked", "failed", "stopped", "unknown"],
                "description": "Only runs in this lifecycle status.",
            },
            "limit": {"type": "integer", "default": 50},
        },
        "required": [],
    },
}

CLAUDE_STOP_SCHEMA = {
    "name": "claude_stop",
    "description": (
        "Request a controlled stop. The worktree, the branch, the Claude session id and "
        "every event are preserved, so the run stays resumable."
    ),
    "parameters": {
        "type": "object",
        "properties": {"run_id": RUN_ID},
        "required": ["run_id"],
    },
}

CLAUDE_RESUME_SCHEMA = {
    "name": "claude_resume",
    "description": (
        "Respawn a stopped, failed, blocked, completed or unknown run using its stored "
        "Claude session id and a new prompt. Never resets, cleans or stashes anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": RUN_ID,
            "message": {"type": "string", "description": "What Claude should do next."},
        },
        "required": ["run_id", "message"],
    },
}

ALL_SCHEMAS = (
    CLAUDE_START_SCHEMA,
    CLAUDE_SEND_SCHEMA,
    CLAUDE_STATUS_SCHEMA,
    CLAUDE_EVENTS_SCHEMA,
    CLAUDE_LIST_SCHEMA,
    CLAUDE_STOP_SCHEMA,
    CLAUDE_RESUME_SCHEMA,
)
