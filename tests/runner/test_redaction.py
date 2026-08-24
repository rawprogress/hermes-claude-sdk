import pytest

from hermes_claude_runner import redaction


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
