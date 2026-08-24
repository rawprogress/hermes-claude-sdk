"""Turn SDK messages into bounded, redacted, durable event records.

Chain of thought is deliberately reduced to a character count: persisting raw
reasoning would retain sensitive internal model text, but knowing that the model
thought is useful.
"""

from __future__ import annotations

from typing import Any

from .redaction import redact_text, redact_value

MAX_TEXT_CHARS = 8000

EVENT_KINDS = frozenset({
    "run_created",
    "worktree_ready",
    "prompt",
    "follow_up",
    "system",
    "assistant_text",
    "thinking",
    "tool_use",
    "tool_result",
    "user_message",
    "result",
    "blocked",
    "unblocked",
    "stop_requested",
    "stopped",
    "error",
    "worker_exit",
    "reconciled",
    "sdk_message",
})

# System ``init`` payloads carry the whole CLI configuration; only these keys
# are useful to Hermes and none of them are credential-bearing.
_SYSTEM_KEYS = ("session_id", "model", "cwd", "uuid")


def truncate(text: str, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    """Return *text* bounded to *limit* characters plus a truncation flag."""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"... [+{len(text) - limit} chars]", True


def _clean(text: Any, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
    if not isinstance(text, str):
        text = _stringify(text)
    return truncate(redact_text(text), limit)


def _stringify(value: Any) -> str:
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - never lose an event to a broken __str__
        return f"<unrepresentable {type(value).__name__}>"


def _event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "payload": payload}


def _blocks_to_events(blocks: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks or []:
        name = type(block).__name__
        if name == "TextBlock":
            text, truncated = _clean(getattr(block, "text", ""))
            if text.strip():
                out.append(_event("assistant_text", {"text": text, "truncated": truncated}))
        elif name == "ThinkingBlock":
            # Length only — never the reasoning itself.
            out.append(_event("thinking", {"chars": len(getattr(block, "thinking", "") or "")}))
        elif name in ("ToolUseBlock", "ServerToolUseBlock"):
            raw_input = getattr(block, "input", {}) or {}
            payload_input, truncated = _clean(_stringify(redact_value(raw_input)))
            out.append(_event("tool_use", {
                "id": _stringify(getattr(block, "id", "")),
                "name": _stringify(getattr(block, "name", "")),
                "input": payload_input,
                "truncated": truncated,
            }))
        elif name in ("ToolResultBlock", "ServerToolResultBlock"):
            content = getattr(block, "content", None)
            text, truncated = _clean(
                content if isinstance(content, str) else _stringify(redact_value(content))
            )
            out.append(_event("tool_result", {
                "tool_use_id": _stringify(getattr(block, "tool_use_id", "")),
                "is_error": getattr(block, "is_error", None),
                "content": text,
                "truncated": truncated,
            }))
        else:
            out.append(_event("sdk_message", {"type": name}))
    return out


def events_from_message(message: Any) -> list[dict[str, Any]]:
    """Convert one SDK message into zero or more event records."""
    kind = type(message).__name__

    if kind == "AssistantMessage":
        return _blocks_to_events(getattr(message, "content", []))

    if kind == "UserMessage":
        content = getattr(message, "content", None)
        if isinstance(content, str):
            text, truncated = _clean(content)
            if not text.strip():
                return []
            return [_event("user_message", {"text": text, "truncated": truncated})]
        return _blocks_to_events(content)

    if kind == "SystemMessage":
        data = getattr(message, "data", {}) or {}
        payload: dict[str, Any] = {"subtype": _stringify(getattr(message, "subtype", ""))}
        if isinstance(data, dict):
            for key in _SYSTEM_KEYS:
                if data.get(key) is not None:
                    payload[key] = redact_text(_stringify(data[key]))
            if data.get("permissionMode") is not None:
                payload["permission_mode"] = _stringify(data["permissionMode"])
            tools = data.get("tools")
            if isinstance(tools, list):
                payload["tool_count"] = len(tools)
        return [_event("system", payload)]

    if kind == "ResultMessage":
        text, truncated = _clean(getattr(message, "result", "") or "")
        return [_event("result", {
            "subtype": _stringify(getattr(message, "subtype", "")),
            "is_error": bool(getattr(message, "is_error", False)),
            "num_turns": getattr(message, "num_turns", None),
            "duration_ms": getattr(message, "duration_ms", None),
            "session_id": _stringify(getattr(message, "session_id", "") or ""),
            "total_cost_usd": getattr(message, "total_cost_usd", None),
            "stop_reason": getattr(message, "stop_reason", None),
            "result": text,
            "truncated": truncated,
        })]

    if kind in ("StreamEvent", "RateLimitEvent", "ConversationResetMessage"):
        return [_event("sdk_message", {"type": kind})]

    return [_event("sdk_message", {"type": kind})]


def session_id_from_message(message: Any) -> str | None:
    """Extract a Claude session id when the message carries one."""
    data = getattr(message, "data", None)
    if isinstance(data, dict) and isinstance(data.get("session_id"), str):
        return data["session_id"] or None
    session_id = getattr(message, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None
