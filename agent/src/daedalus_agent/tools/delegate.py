"""delegate: let an agent hand sub-tasks to a team of sub-agents running in parallel.

Sub-agents only ever get tools the operator enabled for the session, and
delegation stops at ``ToolContext.max_depth``.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from daedalus_agent.tools.base import ToolContext, ToolError, ToolSpec


class SubTask(BaseModel):
    name: str = Field(description="Short name for the sub-agent, e.g. 'tests' or 'docs'.")
    instructions: str = Field(description="Complete, self-contained instructions.")
    tools: list[str] = Field(
        default_factory=list,
        description="Tools this sub-agent needs (a subset of yours). Empty = none.",
    )


class DelegateArgs(BaseModel):
    tasks: list[SubTask] = Field(min_length=1, max_length=16)


async def _delegate(ctx: ToolContext, args: DelegateArgs) -> str:
    from daedalus_agent.team.roles import WORKER_SYSTEM

    runtime = ctx.runtime
    if runtime is None:
        raise ToolError("delegate is not available here")
    if ctx.depth >= ctx.max_depth:
        raise ToolError(f"delegation depth limit ({ctx.max_depth}) reached; do the work yourself")
    for task in args.tasks:
        denied = sorted(set(task.tools) - set(runtime.enabled_tools))
        if denied:
            raise ToolError(f"{task.name}: tools not enabled for this session: {', '.join(denied)}")

    child = ctx.child()
    results = await asyncio.gather(
        *(
            runtime.run_agent(
                name=task.name,
                system=WORKER_SYSTEM,
                prompt=task.instructions,
                tools=task.tools,
                ctx=child,
            )
            for task in args.tasks
        )
    )
    sections = []
    for task, result in zip(args.tasks, results, strict=True):
        status = "ok" if result.ok else f"FAILED: {result.error}"
        sections.append(f"## {task.name} ({status})\n\n{result.text or '(no output)'}")
    return "\n\n".join(sections)


DELEGATE = ToolSpec(
    name="delegate",
    description=(
        "Run a team of sub-agents in parallel, one per task, and return all their reports. "
        "Use it for independent pieces of work; each sub-agent starts with no context but "
        "your instructions."
    ),
    args=DelegateArgs,
    run=_delegate,
    category="team",
    guidelines=("Give sub-agents disjoint files to change so they do not collide.",),
)
