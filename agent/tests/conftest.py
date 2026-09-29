"""A scripted tau provider: tests decide each reply from the request it gets."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from tau_agent import (
    AgentMessage,
    AgentTool,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from tau_agent.provider_events import AssistantDoneEvent

from daedalus_agent.runtime import Runtime
from daedalus_agent.tools import ToolContext

_ids = itertools.count(1)


def reply(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason="stop")


def call(name: str, **arguments: object) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(id=f"call_{next(_ids)}", name=name, arguments=arguments)],
        stop_reason="toolUse",
    )


class Request:
    def __init__(self, system: str, messages: list[AgentMessage], tools: list[AgentTool]):
        self.system = system
        self.messages = messages
        self.tools = [t.name for t in tools]

    @property
    def last(self) -> AgentMessage:
        return self.messages[-1]

    @property
    def prompt(self) -> str:
        users = [m for m in self.messages if isinstance(m, UserMessage)]
        content = users[-1].content
        return content if isinstance(content, str) else "".join(c.text for c in content)

    @property
    def tool_result(self) -> str | None:
        last = self.last
        return last.text if isinstance(last, ToolResultMessage) else None

    def role(self, word: str) -> bool:
        return f"Role: {word}" in self.system


Script = Callable[[Request], AssistantMessage]


class ScriptedProvider:
    def __init__(self, script: Script, delay: float = 0.0) -> None:
        self.script = script
        self.delay = delay
        self.requests: list[Request] = []
        self.active = 0
        self.peak = 0

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: object | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantDoneEvent]:
        request = Request(system, list(messages), list(tools))
        self.requests.append(request)

        async def iterator() -> AsyncIterator[AssistantDoneEvent]:
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                message = self.script(request)
            finally:
                self.active -= 1
            yield AssistantDoneEvent(
                reason="toolUse" if message.tool_calls else "stop", message=message
            )

        return iterator()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws.resolve()


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, command_timeout=10)


@pytest.fixture
def make_runtime(ctx: ToolContext):
    def factory(script: Script, tools: list[str] = (), delay: float = 0.0, **kw) -> Runtime:
        provider = ScriptedProvider(script, delay)
        runtime = Runtime(provider, "test-model", enabled_tools=list(tools), ctx=ctx, **kw)
        runtime.scripted = provider  # type: ignore[attr-defined]
        return runtime

    return factory
