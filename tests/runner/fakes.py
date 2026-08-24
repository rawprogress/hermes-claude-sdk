"""A scripted stand-in for ClaudeSDKClient. No model is ever contacted."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)


def init(session_id: str = "sess-1", cwd: str = "/tmp") -> SystemMessage:
    return SystemMessage(subtype="init", data={"session_id": session_id, "cwd": cwd,
                                               "model": "claude-opus-5", "tools": ["Bash"]})


def say(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-5")


def use_tool(name: str = "Bash", **inputs: Any) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id="tu_1", name=name, input=inputs)],
                            model="claude-opus-5")


def result(text: str = "done", session_id: str = "sess-1", is_error: bool = False,
           subtype: str = "success") -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=10, duration_api_ms=8, is_error=is_error,
                         num_turns=1, session_id=session_id, result=text, total_cost_usd=0.01)


class FakeClient:
    """Replays a script of turns; records everything the worker did."""

    def __init__(self, options: Any, script: list[list[Any]]) -> None:
        self.options = options
        self.script = [list(turn) for turn in script]
        self.queries: list[str] = []
        self.connected = False
        self.disconnected = False
        self.interrupted = 0

    async def connect(self, prompt: Any = None) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def receive_response(self) -> AsyncIterator[Any]:
        turn = self.script.pop(0) if self.script else [result()]
        for entry in turn:
            if callable(entry):
                produced = await entry(self)
                if produced is None:
                    continue
                entry = produced
            if self.interrupted:
                return
            yield entry


def factory(script: list[list[Any]]) -> Any:
    """Return a client factory closing over *script*, exposing the last client."""

    made: list[FakeClient] = []

    def make(options: Any) -> FakeClient:
        client = FakeClient(options, script)
        made.append(client)
        return client

    make.clients = made  # type: ignore[attr-defined]
    return make
