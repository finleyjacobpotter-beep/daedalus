"""Code execution tools: run_bash and run_python.

Both run in the workspace as the current user. Inside `daedalus agent` that means
the fenced labyrinth container; outside it, they are as powerful as your shell,
which is why neither is ever enabled unless named explicitly.
"""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from contextlib import suppress

from pydantic import BaseModel, Field

from daedalus_agent.tools.base import ToolContext, ToolSpec


async def run_process(ctx: ToolContext, argv: list[str], timeout: float | None) -> str:
    timeout = timeout or ctx.command_timeout
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=ctx.workspace,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # own process group, so a timeout kills children too
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        status = f"exit code {proc.returncode}"
    except (TimeoutError, asyncio.CancelledError) as exc:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 5)
        except TimeoutError:  # a grandchild escaped the process group and holds the pipe
            out = b""
        if isinstance(exc, asyncio.CancelledError):
            raise
        status = f"killed after {timeout:g}s timeout"
    text = out.decode("utf-8", errors="replace")
    return f"[{status}]\n{ctx.truncate(text)}" if text else f"[{status}] (no output)"


class RunBashArgs(BaseModel):
    command: str = Field(description="Shell command, run with `bash -c` in the workspace.")
    timeout: float | None = Field(default=None, gt=0, le=3600, description="Seconds.")


async def _run_bash(ctx: ToolContext, args: RunBashArgs) -> str:
    return await run_process(ctx, [ctx.bash, "-c", args.command], args.timeout)


RUN_BASH = ToolSpec(
    name="run_bash",
    description="Run a bash command in the workspace; returns exit code and combined output.",
    args=RunBashArgs,
    run=_run_bash,
    category="exec",
    guidelines=("Commands are non-interactive: stdin is closed.",),
)


class RunPythonArgs(BaseModel):
    code: str = Field(description="Python source to execute as a script in the workspace.")
    timeout: float | None = Field(default=None, gt=0, le=3600, description="Seconds.")


async def _run_python(ctx: ToolContext, args: RunPythonArgs) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(args.code)
        script = fh.name
    try:
        return await run_process(ctx, [ctx.python, script], args.timeout)
    finally:
        os.unlink(script)


RUN_PYTHON = ToolSpec(
    name="run_python",
    description="Run a Python script (python3) in the workspace; returns exit code and output.",
    args=RunPythonArgs,
    run=_run_python,
    category="exec",
    guidelines=("Print what you want to see; the script's return value is not captured.",),
)
