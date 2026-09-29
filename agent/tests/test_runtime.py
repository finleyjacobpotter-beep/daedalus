from __future__ import annotations

import pytest
from conftest import Request, call, reply

from daedalus_agent.tools import UnknownToolError


async def test_no_tools_by_default(make_runtime) -> None:
    runtime = make_runtime(lambda r: reply("hi"))
    result = await runtime.run_agent(name="a", prompt="hello")
    assert result.ok and result.text == "hi"
    request = runtime.scripted.requests[0]
    assert request.tools == []
    assert "You have no tools" in request.system


async def test_only_named_tools_are_exposed(make_runtime) -> None:
    runtime = make_runtime(lambda r: reply("ok"), tools=["read_file", "run_bash"])
    await runtime.run_agent(name="a", prompt="x")
    assert runtime.scripted.requests[0].tools == ["read_file", "run_bash"]
    # Asking for a tool the operator did not enable just leaves it out.
    await runtime.run_agent(name="b", prompt="x", tools=["read_file", "write_file"])
    assert runtime.scripted.requests[1].tools == ["read_file"]
    await runtime.run_agent(name="c", prompt="x", read_only=True)
    assert runtime.scripted.requests[2].tools == ["read_file"]


async def test_unknown_tool_rejected(make_runtime) -> None:
    with pytest.raises(UnknownToolError):
        make_runtime(lambda r: reply("x"), tools=["everything"])


async def test_build_tools_refuses_unenabled(make_runtime) -> None:
    runtime = make_runtime(lambda r: reply("x"), tools=["read_file"])
    with pytest.raises(PermissionError):
        runtime.build_tools(["run_bash"], runtime.ctx)


async def test_tool_loop(make_runtime, workspace) -> None:
    (workspace / "notes.txt").write_text("the answer is 42\n")

    def script(r: Request):
        if r.tool_result is None:
            return call("read_file", path="notes.txt")
        return reply(f"file says: {r.tool_result.split(chr(9))[1].strip()}")

    runtime = make_runtime(script, tools=["read_file"])
    result = await runtime.run_agent(name="a", prompt="what is in notes.txt?")
    assert result.ok
    assert result.text == "file says: the answer is 42"
    assert result.tool_calls == 1
    assert runtime.usage.requests == 2


async def test_model_cannot_call_unenabled_tool(make_runtime) -> None:
    def script(r: Request):
        if r.tool_result is None:
            return call("run_bash", command="rm -rf /")
        return reply(r.tool_result)

    runtime = make_runtime(script, tools=["read_file"])
    result = await runtime.run_agent(name="a", prompt="x")
    assert result.text == "Tool run_bash not found"


async def test_parallel_requests_are_capped(make_runtime) -> None:
    import asyncio

    runtime = make_runtime(lambda r: reply("ok"), delay=0.05, max_parallel=2)
    await asyncio.gather(*(runtime.run_agent(name=f"a{i}", prompt="x") for i in range(6)))
    assert runtime.scripted.peak == 2
