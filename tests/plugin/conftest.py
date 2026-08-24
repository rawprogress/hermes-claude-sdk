"""Load the plugin exactly the way the Hermes plugin loader does.

Hermes imports a directory plugin as ``hermes_plugins.<slug>`` via
``spec_from_file_location``; the directory name contains hyphens, so this is
the only faithful import path. The plugin therefore must use relative imports.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import types
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "hermes_plugin" / "hermes-claude-sdk"
NAMESPACE = "hermes_plugins"
MODULE_NAME = f"{NAMESPACE}.hermes_claude_sdk"

HERMES = shutil.which("hermes")
requires_hermes = pytest.mark.skipif(HERMES is None, reason="hermes CLI is not installed")


def load_plugin() -> Any:
    if NAMESPACE not in sys.modules:
        package = types.ModuleType(NAMESPACE)
        package.__path__ = []  # type: ignore[attr-defined]
        package.__package__ = NAMESPACE
        sys.modules[NAMESPACE] = package

    for name in [n for n in sys.modules if n == MODULE_NAME or n.startswith(f"{MODULE_NAME}.")]:
        del sys.modules[name]

    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = MODULE_NAME
    module.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin() -> Any:
    return load_plugin()


@pytest.fixture()
def tools(plugin: Any) -> Any:
    return sys.modules[f"{MODULE_NAME}.tools"]


class FakeCtx:
    """Records what a plugin registered, like Hermes' PluginContext."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.tools: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        self.settings = settings or {}
        self.manifest = types.SimpleNamespace(name="hermes-claude-sdk", key="hermes-claude-sdk")

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_skill(self, name: str, path: Path, description: str = "", **kw: Any) -> None:
        self.skills.append({"name": name, "path": path, "description": description})

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)
