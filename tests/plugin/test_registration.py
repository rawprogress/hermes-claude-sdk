"""All seven tools, their schemas, and the bundled skill."""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from .conftest import PLUGIN_DIR, FakeCtx

EXPECTED_TOOLS = [
    "claude_start", "claude_send", "claude_status",
    "claude_events", "claude_list", "claude_stop", "claude_resume",
]


@pytest.fixture()
def ctx(plugin: Any) -> FakeCtx:
    fake = FakeCtx()
    plugin.register(fake)
    return fake


def test_all_seven_tools_are_registered(ctx: FakeCtx) -> None:
    assert [t["name"] for t in ctx.tools] == EXPECTED_TOOLS


def test_tools_share_one_toolset(ctx: FakeCtx) -> None:
    assert {t["toolset"] for t in ctx.tools} == {"claude_sdk"}


def test_every_handler_is_callable_and_takes_kwargs(ctx: FakeCtx) -> None:
    import inspect

    for tool in ctx.tools:
        signature = inspect.signature(tool["handler"])
        params = list(signature.parameters.values())
        assert params[0].name == "args"
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), tool["name"]


def test_schemas_are_well_formed(ctx: FakeCtx) -> None:
    for tool in ctx.tools:
        schema = tool["schema"]
        assert schema["name"] == tool["name"]
        assert schema["description"].strip()
        params = schema["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert json.dumps(schema)  # must be JSON serializable for the model


def test_schema_required_fields_match_the_brief(ctx: FakeCtx) -> None:
    required = {t["name"]: t["schema"]["parameters"].get("required", []) for t in ctx.tools}
    assert required["claude_start"] == ["project", "prompt"]
    assert required["claude_send"] == ["run_id", "message"]
    assert required["claude_status"] == ["run_id"]
    assert required["claude_events"] == ["run_id"]
    assert required["claude_list"] == []
    assert required["claude_stop"] == ["run_id"]
    assert required["claude_resume"] == ["run_id", "message"]


def test_start_schema_documents_its_defaults(ctx: FakeCtx) -> None:
    properties = dict(ctx.tools[0]["schema"]["parameters"]["properties"])
    assert properties["role"]["default"] == "implementer"
    assert properties["create_worktree"]["default"] is True
    assert properties["create_worktree"]["type"] == "boolean"


def test_events_schema_exposes_the_cursor(ctx: FakeCtx) -> None:
    events = next(t for t in ctx.tools if t["name"] == "claude_events")
    properties = events["schema"]["parameters"]["properties"]
    assert properties["after"]["type"] == "integer"
    assert properties["limit"]["type"] == "integer"


def test_bundled_skill_is_registered_read_only(ctx: FakeCtx) -> None:
    assert len(ctx.skills) == 1
    skill = ctx.skills[0]
    assert skill["path"].is_file()
    body = skill["path"].read_text()
    assert "Hermes orchestrates" in body
    assert "Claude" in body and "tests" in body


def test_skill_frontmatter_is_valid(ctx: FakeCtx) -> None:
    text = ctx.skills[0]["path"].read_text()
    assert text.startswith("---\n")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["name"] == ctx.skills[0]["name"]
    assert frontmatter["description"].strip()


# ── manifest ───────────────────────────────────────────────────────────────

def manifest() -> dict[str, Any]:
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())


def test_manifest_declares_exactly_the_registered_tools() -> None:
    assert manifest()["provides_tools"] == EXPECTED_TOOLS


def test_manifest_is_a_standalone_plugin_with_a_version() -> None:
    data = manifest()
    assert data["name"] == "hermes-claude-sdk"
    assert data["kind"] == "standalone"
    assert data["version"]
    assert data["description"].strip()


def test_manifest_declares_no_third_party_python_dependency() -> None:
    # The Hermes venv must stay untouched; the plugin only shells out to ssh.
    assert manifest().get("python_dependencies", []) == []


def test_manifest_config_schema_covers_the_transport_settings() -> None:
    schema = manifest()["config_schema"]
    assert schema["ssh_host"]["default"] == "macbook"
    assert schema["remote_command"]["default"] == "~/.local/bin/hermes-claude-runner"
    assert schema["timeout_seconds"]["type"] in ("int", "integer")
    assert schema["timeout_seconds"]["default"] > 0
    assert schema["transport"]["default"] == "ssh", "existing installs must not move"
    assert schema["local_command"]["default"] == "~/.local/bin/hermes-claude-runner"


def test_manifest_documents_both_transports() -> None:
    """A setting nobody can discover is a setting nobody will use correctly."""
    text = manifest()["config_schema"]["transport"]["description"]
    assert "ssh" in text and "local" in text


def test_manifest_defaults_match_the_transports_own_defaults(tools: Any) -> None:
    """The manifest is what a user edits; drift between the two is a trap."""
    schema = manifest()["config_schema"]
    assert schema["ssh_host"]["default"] == tools.DEFAULT_SSH_HOST
    assert schema["remote_command"]["default"] == tools.DEFAULT_REMOTE_COMMAND
    assert schema["timeout_seconds"]["default"] == tools.DEFAULT_TIMEOUT_SECONDS
    assert schema["transport"]["default"] == tools.DEFAULT_TRANSPORT
    assert schema["local_command"]["default"] == tools.DEFAULT_LOCAL_COMMAND


def test_every_documented_setting_reaches_the_transport(plugin: Any) -> None:
    """register() only forwards the keys it knows; a missing one is invisible."""
    assert set(manifest()["config_schema"]) == set(plugin._SETTING_KEYS)


def test_no_documented_default_names_one_persons_machine() -> None:
    """A default carrying any absolute home only works for its author."""
    import re

    homes = re.findall("/Users" + r"/([A-Za-z0-9._-]+)",
                       (PLUGIN_DIR / "plugin.yaml").read_text())
    assert homes == [], f"the manifest hardcodes the home directory of {homes}"


def test_plugin_imports_only_the_standard_library(plugin: Any) -> None:
    sources = [PLUGIN_DIR / name for name in ("__init__.py", "tools.py", "schemas.py")]
    for source in sources:
        for line in source.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "." not in stripped.split()[1]:
                module = stripped.split()[1]
                assert module in {
                    "__future__", "json", "logging", "os", "pathlib", "re", "shlex",
                    "subprocess", "sys", "typing", "collections",
                }, f"{source.name} imports {module}"


def test_plugin_files_exist() -> None:
    for name in ("plugin.yaml", "__init__.py", "schemas.py", "tools.py"):
        assert (PLUGIN_DIR / name).is_file(), name
    assert list(PLUGIN_DIR.glob("skills/*/SKILL.md")), "bundled skill missing"


def test_register_is_idempotent_across_reloads(plugin: Any) -> None:
    first, second = FakeCtx(), FakeCtx()
    plugin.register(first)
    plugin.register(second)
    assert [t["name"] for t in first.tools] == [t["name"] for t in second.tools]


def test_registration_survives_a_context_without_register_skill(plugin: Any) -> None:
    class OldCtx(FakeCtx):
        register_skill = None  # type: ignore[assignment]

    ctx = OldCtx()
    plugin.register(ctx)  # must not raise on an older Hermes
    assert len(ctx.tools) == 7
