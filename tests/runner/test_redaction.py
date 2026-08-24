import time

import pytest

from hermes_claude_runner import redaction

#: Big enough to be the whole of a captured stream, and made only of the
#: characters a credential name may contain — the shape that used to make the
#: assignment scan quadratic.
DENSE = "a." * 32_768

#: What a scrub of a full capture has to fit in. The measured cost before the
#: rewrite was 111s for this input; anything near the old behaviour blows
#: through this by two orders of magnitude.
SCRUB_BUDGET_SECONDS = 5.0


def scrub(text: str) -> tuple[str, float]:
    started = time.monotonic()
    out = redaction.redact_text(text)
    return out, time.monotonic() - started


def test_anthropic_key_is_removed() -> None:
    text = 'export ANTHROPIC_API_KEY=' + 'sk' + '-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF1234'
    out = redaction.redact_text(text)
    assert "sk-ant" not in out
    assert "AAAABBBB" not in out
    assert "[redacted" in out


def test_common_provider_tokens_are_removed() -> None:
    samples = [
        'gh' + 'p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345',
        "github_pat_11ABCDE0Y0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789",
        'AK' + 'IAIOSFODNN7EXAMPLE',
        'xo' + 'xb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx',
        'ey' + 'JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r',
    ]
    for secret in samples:
        out = redaction.redact_text(f"value: {secret} end")
        assert secret not in out, secret
        assert "[redacted" in out


def test_secret_shaped_assignments_are_removed() -> None:
    for line in [
        "MY_SERVICE_TOKEN=hunter2hunter2",
        "database_password: sup3rs3cret",
        'AWS_SECRET_ACCESS_KEY = "abc/def+ghi"',
        "Authorization: Bearer abcdef123456789",
    ]:
        out = redaction.redact_text(line)
        assert "[redacted" in out, line
        assert "hunter2" not in out and "sup3rs3cret" not in out and "abc/def" not in out


# ── the scrubber has to finish ─────────────────────────────────────────────

def test_dense_punctuation_does_not_stall_the_scrubber() -> None:
    """A stream of dots and letters used to cost minutes, not milliseconds.

    The doctor hands this function whatever a CLI printed, so a shape that
    makes it quadratic is a preflight that never returns — the child timeout
    does not help, because the cost is all after the process has exited.
    """
    out, elapsed = scrub(DENSE)

    assert elapsed < SCRUB_BUDGET_SECONDS, f"scrubbing took {elapsed:.1f}s"
    assert out == DENSE, "nothing here is a credential"


def test_a_name_run_with_no_assignment_does_not_stall_the_scrubber() -> None:
    """Every keyword in one run expands to the same place; scan it once."""
    _out, elapsed = scrub("TOKEN" * 13_000)

    assert elapsed < SCRUB_BUDGET_SECONDS, f"scrubbing took {elapsed:.1f}s"


def test_a_dense_run_that_does_end_in_a_secret_is_still_scrubbed() -> None:
    """Speed must not come from giving up on the input."""
    out, elapsed = scrub(DENSE + "MY_TOKEN=hunter2hunter2")

    assert elapsed < SCRUB_BUDGET_SECONDS, f"scrubbing took {elapsed:.1f}s"
    assert "hunter2" not in out
    assert "[redacted:secret]" in out


def test_the_cost_grows_with_the_input_not_with_its_square() -> None:
    """Four times the input, not sixteen times the work."""
    _small, small_seconds = scrub("a." * 8_192)
    _large, large_seconds = scrub("a." * 32_768)

    # Generous: quadratic would be 16x, and was measured at ~40x here.
    assert large_seconds < max(small_seconds * 8, 0.5), (
        f"{small_seconds:.3f}s for 16 KiB, {large_seconds:.3f}s for 64 KiB"
    )


# ── and it has to keep finding what it found before ────────────────────────

@pytest.mark.parametrize(("line", "secret"), [
    ("MY_SERVICE_TOKEN=hunter2hunter2", "hunter2hunter2"),
    ("database_password: sup3rs3cret", "sup3rs3cret"),
    ('AWS_SECRET_ACCESS_KEY = "abc/def+ghi"', "abc/def+ghi"),
    ("api_key=abcdef123456", "abcdef123456"),
    ("APIKEY: abcdef123456", "abcdef123456"),
    ("x.y.PRIVATE_KEY_PATH=/tmp/id_rsa", "/tmp/id_rsa"),
    ("credential: abcdef123456", "abcdef123456"),
    ("passwd=letmein, user=me", "letmein"),
    ("SOME-TOKEN-NAME = value123", "value123"),
    ("9TOKEN=value123", "value123"),
    ("-TOKEN=value123", "value123"),
    ("run --token=abcdef123456", "abcdef123456"),
])
def test_every_assignment_shape_still_loses_its_value(line: str, secret: str) -> None:
    out = redaction.redact_text(line)

    assert secret not in out, line
    assert "[redacted:secret]" in out, line


@pytest.mark.parametrize(("line", "expected"), [
    ("MY_SERVICE_TOKEN=hunter2", "MY_SERVICE_TOKEN=[redacted:secret]"),
    ("database_password: sup3rs3cret", "database_password: [redacted:secret]"),
    ('AWS_SECRET_ACCESS_KEY = "abc"', 'AWS_SECRET_ACCESS_KEY = [redacted:secret]'),
    ("a TOKEN=1 and a SECRET=2", "a TOKEN=[redacted:secret] and a SECRET=[redacted:secret]"),
])
def test_the_name_survives_and_only_the_value_goes(line: str, expected: str) -> None:
    """The reader has to be able to see which setting was scrubbed."""
    assert redaction.redact_text(line) == expected


@pytest.mark.parametrize("line", [
    "the token is documented in README.md",
    "password reset flow, see docs/auth.md",
    "Fixed add() in src/demo/core.py; 3 tests pass at commit a1b2c3d.",
])
def test_prose_about_credentials_is_not_mistaken_for_one(line: str) -> None:
    """An over-eager scrubber makes logs unreadable and hides real findings."""
    assert redaction.redact_text(line) == line


def test_a_known_over_eager_match_is_pinned_not_quietly_changed() -> None:
    """`tokens: 1234` has always read as an assignment, and still does.

    Pinned rather than narrowed: refusing a suffix after the keyword would
    also lose `TOKEN_PATH=` and `API_KEY_ID=`, which are real. A rewrite for
    speed is the wrong moment to change what counts as a secret.
    """
    assert redaction.redact_text("tokens: 1234 in, 5678 out") == (
        "tokens: [redacted:secret] in, 5678 out"
    )


def test_private_key_blocks_are_removed() -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc123\n-----END OPENSSH PRIVATE KEY-----"
    out = redaction.redact_text(pem)
    assert "abc123" not in out
    assert "[redacted:private_key]" in out


def test_ordinary_text_survives_untouched() -> None:
    text = "Fixed add() in src/demo/core.py; 3 tests pass at commit a1b2c3d."
    assert redaction.redact_text(text) == text


def test_non_string_input_is_returned_as_is() -> None:
    assert redaction.redact_text(None) is None  # type: ignore[arg-type]


def test_redact_value_walks_nested_structures() -> None:
    payload = {
        "cmd": "deploy",
        "env": {"API_TOKEN": 'gh' + 'p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'},
        "args": ["--key", 'sk' + '-ant-api03-ZZZZYYYYXXXXWWWWVVVV9999'],
        "count": 3,
        "ok": True,
    }
    out = redaction.redact_value(payload)
    assert out["count"] == 3 and out["ok"] is True
    assert "ghp_" not in str(out)
    assert "sk-ant" not in str(out)


def test_redact_value_bounds_recursion_depth() -> None:
    deep: dict = {}
    node = deep
    for _ in range(200):
        node["next"] = {}
        node = node["next"]
    out = redaction.redact_value(deep)
    assert isinstance(out, dict)


# ── the reject gate uses high-confidence patterns only ─────────────────────

TOKENS = [
    'sk' + '-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF1234',
    'gh' + 'p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345',
    "github_pat_11ABCDE0Y0aBcDeFgHiJkL_mNoPqRsTuVwXyZ0123456789",
    'AK' + 'IAIOSFODNN7EXAMPLE',
    'xo' + 'xb-123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx',
    'ey' + 'JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r',
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
]


@pytest.mark.parametrize("token", TOKENS)
def test_find_secrets_flags_token_shaped_material(token: str) -> None:
    assert redaction.find_secrets(f"please use {token} for the call")


PROSE = [
    "fix the 'wrong password: try again' message in the login form",
    "rename API_KEY_HEADER to AUTH_HEADER across the codebase",
    "the secret sauce is caching; document it",
    "add a test for token expiry handling",
    "set DATABASE_PASSWORD from the environment, never inline",
]


@pytest.mark.parametrize("text", PROSE)
def test_find_secrets_leaves_ordinary_engineering_prose_alone(text: str) -> None:
    assert redaction.find_secrets(text) == []


def test_find_secrets_names_what_it_found() -> None:
    kinds = redaction.find_secrets('key ' + 'gh' + 'p_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345' + ' here')
    assert kinds == ["github_token"]


def test_find_secrets_reports_each_kind_once() -> None:
    text = " ".join(TOKENS[:2] + TOKENS[:2])
    assert sorted(redaction.find_secrets(text)) == ["anthropic_key", "github_token"]


def test_find_secrets_ignores_non_strings() -> None:
    assert redaction.find_secrets(None) == []  # type: ignore[arg-type]
    assert redaction.find_secrets(12) == []  # type: ignore[arg-type]


def test_redaction_still_scrubs_the_loose_heuristic() -> None:
    # The gate is strict; redaction of model output stays broad.
    assert "[redacted" in redaction.redact_text("MY_SERVICE_TOKEN=hunter2hunter2")
