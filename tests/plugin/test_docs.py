"""The documented blocked-path limitation must stay true and stay visible."""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import PLUGIN_DIR

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
SKILL = PLUGIN_DIR / "skills" / "claude-code-orchestration" / "SKILL.md"


def flatten(document: Path) -> str:
    """Markdown wraps lines; phrases must be matched across those wraps."""
    return " ".join(document.read_text().split()).lower().replace("**", "")


@pytest.mark.parametrize("document", [README, SKILL], ids=["README", "SKILL"])
def test_the_ask_user_question_gap_is_documented(document: Path) -> None:
    text = flatten(document)
    assert "askuserquestion" in text
    assert "0.2.144" in text, "the SDK version the claim was verified against"
    assert "init tool list" in text
    assert "does not currently occur" in text


@pytest.mark.parametrize("document", [README, SKILL], ids=["README", "SKILL"])
def test_the_escalation_machinery_is_described_as_prepared(document: Path) -> None:
    text = flatten(document)
    assert "pretooluse" in text
    assert "escalat" in text


def test_the_readme_does_not_promise_a_routine_blocked_status() -> None:
    text = README.read_text()
    assert "Claude asked a question; the event carries it verbatim" not in text, (
        "the recovery table must not imply blocked happens in normal operation"
    )


def test_the_tripwire_live_test_is_still_present() -> None:
    live = REPO_ROOT / "tests" / "runner" / "test_live_sdk.py"
    source = live.read_text()
    assert "test_ask_user_question_availability_is_recorded" in source
    assert "AskUserQuestion" in source
