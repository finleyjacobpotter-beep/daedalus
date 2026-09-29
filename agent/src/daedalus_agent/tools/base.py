"""Tool plumbing shared by every Daedalus tool.

A tool is declared once as a :class:`ToolSpec` (name, pydantic argument model,
async implementation). Nothing is exposed to a model until it is turned into a
tau :class:`~tau_agent.AgentTool` by :func:`build_agent_tool`, and that only
happens for tools the operator named explicitly (see ``tools/__init__.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError
from tau_agent import AgentTool, AgentToolResult, TextContent

if TYPE_CHECKING:
    from daedalus_agent.runtime import Runtime


class ToolError(Exception):
    """A tool failed in a way the model should see and can recover from."""


@dataclass(slots=True)
class ToolContext:
    """Everything a tool needs from the outside world, and nothing more."""

    workspace: Path
    # Allow file tools to touch paths outside the workspace (off by default).
    allow_outside_workspace: bool = False
    command_timeout: float = 120.0
    max_output_chars: int = 30_000
    python: str = "python3"
    bash: str = "bash"
    web_timeout: float = 30.0
    web_max_chars: int = 50_000
    # Set by the runtime so the `delegate` tool can start sub-agents.
    runtime: Runtime | None = None
    depth: int = 0
    max_depth: int = 2
    _locks: dict[Path, asyncio.Lock] = field(default_factory=dict)

    def resolve(self, raw: str) -> Path:
        """Resolve a model-supplied path, keeping it inside the workspace."""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if not self.allow_outside_workspace and not path.is_relative_to(self.workspace):
            raise ToolError(f"{raw}: outside the workspace ({self.workspace})")
        return path

    def contains(self, path: Path) -> bool:
        """Whether a path (after following symlinks) is one the tools may touch."""
        return self.allow_outside_workspace or path.resolve().is_relative_to(self.workspace)

    def display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace)) or "."
        except ValueError:
            return str(path)

    def lock(self, path: Path) -> asyncio.Lock:
        """Per-file lock so parallel team members never interleave writes."""
        return self._locks.setdefault(path, asyncio.Lock())

    def truncate(self, text: str, limit: int | None = None) -> str:
        limit = limit or self.max_output_chars
        if len(text) <= limit:
            return text
        head = limit * 2 // 3
        tail = limit - head
        dropped = len(text) - limit
        return f"{text[:head]}\n\n[... {dropped} characters truncated ...]\n\n{text[-tail:]}"

    def child(self) -> ToolContext:
        """Context for a sub-agent: same workspace and locks, one level deeper."""
        return ToolContext(
            workspace=self.workspace,
            allow_outside_workspace=self.allow_outside_workspace,
            command_timeout=self.command_timeout,
            max_output_chars=self.max_output_chars,
            python=self.python,
            bash=self.bash,
            web_timeout=self.web_timeout,
            web_max_chars=self.web_max_chars,
            runtime=self.runtime,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            _locks=self._locks,
        )


ToolImpl = Callable[[ToolContext, Any], Awaitable[str | AgentToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Declarative description of one tool."""

    name: str
    description: str
    args: type[BaseModel]
    run: ToolImpl
    # Read-only tools cannot change the workspace or run code; hyper planning
    # researchers and planners only ever get these.
    read_only: bool = False
    category: str = "misc"
    guidelines: tuple[str, ...] = ()

    @property
    def parameters(self) -> dict[str, Any]:
        return _clean_schema(self.args.model_json_schema())


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline $refs and drop pydantic's titles: plainer schemas suit every provider."""
    defs = schema.pop("$defs", {})

    def walk(node: Any, *, names: bool = False) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].rsplit("/", 1)[-1]]))
            # Keys of "properties" are field names, which may well be "title".
            return {
                k: walk(v, names=(k == "properties" and not names))
                for k, v in node.items()
                if names or k != "title"
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def _format_validation_error(name: str, exc: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
        for err in exc.errors()
    )
    return f"Invalid arguments for {name}: {problems}"


def build_agent_tool(spec: ToolSpec, ctx: ToolContext) -> AgentTool:
    """Bind a spec to a context and wrap it as a tau AgentTool."""

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, Any],
        signal: object | None = None,
        on_update: object | None = None,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        try:
            args = spec.args.model_validate(dict(arguments))
        except ValidationError as exc:
            raise ToolError(_format_validation_error(spec.name, exc)) from None
        result = await spec.run(ctx, args)
        if isinstance(result, AgentToolResult):
            return result
        return AgentToolResult(content=[TextContent(text=result or "(no output)")])

    return AgentTool(
        name=spec.name,
        label=spec.name,
        description=spec.description,
        parameters=spec.parameters,
        execute_fn=execute,
        prompt_snippet=spec.description.split("\n", 1)[0],
        prompt_guidelines=spec.guidelines,
        execution_mode="sequential" if not spec.read_only else "parallel",
    )
