"""Best-effort secret scrubbing for anything that reaches the events table.

Events travel back to Hermes and sit on disk, so credentials that appear in
a shell command or a tool result must not be persisted verbatim.
"""

from __future__ import annotations

import re
from typing import Any

_MAX_DEPTH = 12

# High-confidence credential shapes. Only these drive the reject gate: they
# match token material, never prose that merely talks about credentials.
_HIGH_CONFIDENCE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
)

# Looser heuristics. Good enough to scrub model output, too eager to reject an
# operator's prompt on — "fix the 'wrong password: try again' message" is fine.
_HEURISTIC: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 [redacted:auth]"),
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (pattern, f"[redacted:{kind}]") for kind, pattern in _HIGH_CONFIDENCE
) + _HEURISTIC

# ``NAME=value`` / ``name: value`` where NAME looks like a credential.
_SECRET_NAME = (
    r"[A-Za-z0-9_.\-]*"
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|ACCESS_KEY|CREDENTIAL|PRIVATE_KEY)"
    r"[A-Za-z0-9_.\-]*"
)
_ASSIGNMENT = re.compile(
    rf"(?i)\b({_SECRET_NAME})(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;)}}\]]+)"
)


def redact_text(value: str) -> str:
    """Replace credential-shaped substrings in *value*."""
    if not isinstance(value, str) or not value:
        return value
    out = value
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    out = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted:secret]", out)
    return out


def find_secrets(value: str) -> list[str]:
    """Return the kinds of credential-shaped material found in *value*.

    Only high-confidence token shapes count. This drives the reject gate, so a
    false positive would refuse legitimate work; the looser assignment
    heuristics stay confined to :func:`redact_text`.
    """
    if not isinstance(value, str) or not value:
        return []
    found: list[str] = []
    for kind, pattern in _HIGH_CONFIDENCE:
        if kind not in found and pattern.search(value):
            found.append(kind)
    return found


def redact_value(value: Any, _depth: int = 0) -> Any:
    """Recursively redact strings inside dicts, lists and tuples."""
    if _depth >= _MAX_DEPTH:
        return "[redacted:too_deep]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact_value(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, _depth + 1) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(_safe_repr(value))


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:  # noqa: BLE001 - a broken __repr__ must not lose the event
        return f"<unrepresentable {type(value).__name__}>"
