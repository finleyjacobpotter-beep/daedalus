"""The runtime: one model provider, one explicit tool allowlist, many agents.

Every agent Daedalus starts, whether the single ``run`` agent, a hyper planning
researcher or an ultrawork worker, is a tau :class:`~tau_agent.AgentHarness`
created by :meth:`Runtime.run_agent`. That is the one place tools are handed
out, so the allowlist is enforced in one place too.
"""

from __future__ import annotations

import asyncio
import platform
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from tau_agent import (
    AgentEvent,
    AgentHarness,
    AgentHarnessConfig,
    AgentMessage,
    AgentTool,
    AssistantMessage,
    MessageEndEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from tau_agent.provider import ModelProvider

from daedalus_agent.tools import TOOLS, ToolContext, build_agent_tool, parse_tool_names


@dataclass(slots=True)
class UsageTotals:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    requests: int = 0

    def add(self, message: AssistantMessage) -> None:
        self.input += message.usage.input
        self.output += message.usage.output
        self.cache_read += message.usage.cache_read
        self.cache_write += message.usage.cache_write
        self.cost += message.usage.cost.total
        self.requests += 1

    def merge(self, other: UsageTotals) -> None:
        for name in ("input", "output", "cache_read", "cache_write", "cost", "requests"):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def summary(self) -> str:
        cost = f", ${self.cost:.4f}" if self.cost else ""
        return (
            f"{self.requests} requests, {self.input} in / {self.output} out tokens"
            f" ({self.cache_read} cache read){cost}"
        )


@dataclass(slots=True)
class AgentResult:
    name: str
    text: str
    ok: bool
    error: str | None = None
    tool_calls: int = 0
    usage: UsageTotals = field(default_factory=UsageTotals)
    messages: tuple[AgentMessage, ...] = ()


class Reporter(Protocol):
    def event(self, agent: str, event: AgentEvent) -> None: ...

    def note(self, text: str) -> None: ...


class SilentReporter:
    def event(self, agent: str, event: AgentEvent) -> None:
        pass

    def note(self, text: str) -> None:
        pass


class ConsoleReporter:
    """Progress on stderr. With ``stream`` set, that agent's text streams to stdout."""

    def __init__(self, *, stream: str | None = None, verbose: bool = False) -> None:
        self.stream = stream
        self.verbose = verbose
        self._streamed = False

    def note(self, text: str) -> None:
        line = f"\033[1m== {text}\033[0m" if sys.stderr.isatty() else f"== {text}"
        print(line, file=sys.stderr, flush=True)

    def event(self, agent: str, event: AgentEvent) -> None:
        if isinstance(event, ToolExecutionStartEvent):
            args = _short_args(event.args)
            print(f"  [{agent}] {event.tool_name}({args})", file=sys.stderr, flush=True)
        elif isinstance(event, ToolExecutionEndEvent) and (event.is_error or self.verbose):
            label = "error" if event.is_error else "ok"
            print(
                f"  [{agent}] {event.tool_name} {label}: {_first_line(event.result.text)}",
                file=sys.stderr,
                flush=True,
            )
        elif isinstance(event, MessageUpdateEvent) and agent == self.stream:
            inner = event.assistant_message_event
            if inner.type == "text_delta":
                self._streamed = True
                print(inner.delta, end="", flush=True)
        elif isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
            if agent == self.stream:
                # Providers that do not stream deltas still get their text shown.
                text = "" if self._streamed else event.message.text
                if text or self._streamed:
                    print(text, flush=True)
                self._streamed = False
            if event.message.error_message:
                print(f"  [{agent}] error: {event.message.error_message}", file=sys.stderr)


def _first_line(text: str, limit: int = 160) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 3] + "..."


def _short_args(args: dict[str, Any], limit: int = 120) -> str:
    parts = []
    for key, value in args.items():
        text = value if isinstance(value, str) else repr(value)
        text = text.replace("\n", "\\n")
        parts.append(f"{key}={text[:60]}{'...' if len(text) > 60 else ''}")
    joined = ", ".join(parts)
    return joined if len(joined) <= limit else joined[: limit - 3] + "..."


class ThrottledProvider:
    """Caps concurrent model requests across a whole team.

    Only the model stream holds a slot, never tool execution, so an agent
    waiting on its own sub-agents (``delegate``) cannot deadlock the team.
    """

    def __init__(self, inner: ModelProvider, max_parallel: int) -> None:
        self.inner = inner
        self._slots = asyncio.Semaphore(max_parallel)

    def stream_response(self, **kwargs: Any) -> AsyncIterator[Any]:
        async def iterator() -> AsyncIterator[Any]:
            async with self._slots:
                async for event in self.inner.stream_response(**kwargs):
                    yield event

        return iterator()


BASE_SYSTEM = """\
You are Daedalus, a careful software engineering agent working in {workspace}.
Platform: {platform}.

{tools_section}

Be direct. When you are done, reply with a concise report of what you did and \
what you found, including anything left undone."""


def tools_section(tools: Sequence[AgentTool]) -> str:
    if not tools:
        return (
            "You have no tools in this session. Answer from the conversation alone, and say "
            "so if the task needs access you do not have."
        )
    lines = ["Tools available to you (and only these):"]
    for tool in tools:
        lines.append(f"- {tool.name}: {tool.prompt_snippet or tool.description}")
        lines.extend(f"  - {g}" for g in tool.prompt_guidelines)
    return "\n".join(lines)


class Runtime:
    def __init__(
        self,
        provider: ModelProvider,
        model: str,
        *,
        enabled_tools: Sequence[str],
        ctx: ToolContext,
        max_parallel: int = 8,
        max_turns: int = 60,
        reporter: Reporter | None = None,
    ) -> None:
        self.enabled_tools: tuple[str, ...] = tuple(parse_tool_names(enabled_tools))
        self.provider = ThrottledProvider(provider, max_parallel)
        self.model = model
        self.ctx = ctx
        self.ctx.runtime = self
        self.max_parallel = max_parallel
        self.max_turns = max_turns
        self.reporter: Reporter = reporter or SilentReporter()
        self.usage = UsageTotals()

    # -- tools ---------------------------------------------------------------

    def allowed(self, names: Sequence[str] | None, *, read_only: bool = False) -> list[str]:
        """Intersect requested tools with the session allowlist (None = all enabled)."""
        requested = self.enabled_tools if names is None else names
        return [
            name
            for name in self.enabled_tools
            if name in requested and (not read_only or TOOLS[name].read_only)
        ]

    def build_tools(self, names: Sequence[str], ctx: ToolContext) -> list[AgentTool]:
        tools = []
        for name in names:
            if name not in self.enabled_tools:  # defence in depth; allowed() already filters
                raise PermissionError(f"tool {name!r} is not enabled for this session")
            if name == "delegate" and ctx.depth >= ctx.max_depth:
                continue
            tools.append(build_agent_tool(TOOLS[name], ctx))
        return tools

    def system_prompt(self, role: str, tools: Sequence[AgentTool], ctx: ToolContext) -> str:
        base = BASE_SYSTEM.format(
            workspace=ctx.workspace,
            platform=f"{platform.system()} {platform.machine()}",
            tools_section=tools_section(tools),
        )
        return f"{base}\n\n{role}" if role else base

    # -- agents --------------------------------------------------------------

    async def run_agent(
        self,
        *,
        name: str,
        prompt: str,
        system: str = "",
        tools: Sequence[str] | None = None,
        read_only: bool = False,
        max_turns: int | None = None,
        ctx: ToolContext | None = None,
        history: Sequence[AgentMessage] = (),
    ) -> AgentResult:
        harness = self.make_harness(
            system=system,
            tools=tools,
            read_only=read_only,
            max_turns=max_turns,
            ctx=ctx,
            history=history,
        )
        return await self.drive(name, harness, prompt)

    def make_harness(
        self,
        *,
        system: str = "",
        tools: Sequence[str] | None = None,
        read_only: bool = False,
        max_turns: int | None = None,
        ctx: ToolContext | None = None,
        history: Sequence[AgentMessage] = (),
    ) -> AgentHarness:
        ctx = ctx or self.ctx
        agent_tools = self.build_tools(self.allowed(tools, read_only=read_only), ctx)
        return AgentHarness(
            AgentHarnessConfig(
                provider=self.provider,
                model=self.model,
                system=self.system_prompt(system, agent_tools, ctx),
                tools=agent_tools,
                max_turns=max_turns or self.max_turns,
            ),
            messages=history,
        )

    async def drive(self, name: str, harness: AgentHarness, prompt: str) -> AgentResult:
        """Run one prompt on a harness to completion and summarise the outcome."""
        usage = UsageTotals()
        tool_calls = 0
        last: AssistantMessage | None = None
        async for event in harness.prompt(prompt):
            self.reporter.event(name, event)
            if isinstance(event, ToolExecutionStartEvent):
                tool_calls += 1
            elif isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
                last = event.message
                usage.add(event.message)
        self.usage.merge(usage)
        error = None
        if last is None:
            error = "no response from model"
        elif last.stop_reason in {"error", "aborted"}:
            error = last.error_message or last.stop_reason
        return AgentResult(
            name=name,
            text=last.text if last else "",
            ok=error is None,
            error=error,
            tool_calls=tool_calls,
            usage=usage,
            messages=harness.messages,
        )
