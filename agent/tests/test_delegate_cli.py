from __future__ import annotations

import pytest
from conftest import Request, ScriptedProvider, call, reply

from daedalus_agent import cli


async def test_delegate_runs_subagents_in_parallel(make_runtime) -> None:
    def script(r: Request):
        if r.role("team member"):
            return reply(f"sub report for: {r.prompt}")
        if r.tool_result is None:
            return call(
                "delegate",
                tasks=[
                    {"name": "one", "instructions": "part one", "tools": ["read_file"]},
                    {"name": "two", "instructions": "part two"},
                ],
            )
        return reply(r.tool_result)

    runtime = make_runtime(script, tools=["read_file", "delegate"], delay=0.02)
    result = await runtime.run_agent(name="lead", prompt="split it")
    assert "## one (ok)" in result.text and "sub report for: part two" in result.text
    subs = [r for r in runtime.scripted.requests if r.role("team member")]
    assert sorted(s.tools for s in subs) == [[], ["read_file"]]
    assert runtime.scripted.peak == 2


async def test_delegate_cannot_widen_tools(make_runtime) -> None:
    def script(r: Request):
        if r.tool_result is None:
            return call(
                "delegate",
                tasks=[{"name": "x", "instructions": "y", "tools": ["run_bash"]}],
            )
        return reply(r.tool_result)

    runtime = make_runtime(script, tools=["delegate"])
    result = await runtime.run_agent(name="lead", prompt="go")
    assert "not enabled for this session: run_bash" in result.text


async def test_delegate_depth_limit(make_runtime) -> None:
    def script(r: Request):
        if r.tool_result is None and "delegate" in r.tools:
            return call(
                "delegate",
                tasks=[{"name": "deeper", "instructions": "go on", "tools": ["delegate"]}],
            )
        return reply(r.tool_result or f"leaf with tools {r.tools}")

    runtime = make_runtime(script, tools=["delegate"])
    runtime.ctx.max_depth = 1
    result = await runtime.run_agent(name="lead", prompt="go")
    # The depth-1 sub-agent is not offered delegate at all.
    assert "leaf with tools []" in result.text


def test_cli_tools_lists_everything(capsys) -> None:
    assert cli.main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "None is enabled by default" in out and "run_python" in out


def test_cli_run(monkeypatch, capsys, workspace) -> None:
    provider = ScriptedProvider(lambda r: reply(f"tools={r.tools}"))
    monkeypatch.setattr(cli, "build_provider", lambda spec, max_tokens=None: (provider, "m"))
    monkeypatch.delenv("DAEDALUS_AGENT_TOOLS", raising=False)
    assert cli.main(["run", "-w", str(workspace), "hi"]) == 0
    assert "tools=[]" in capsys.readouterr().out
    monkeypatch.setenv("DAEDALUS_AGENT_TOOLS", "read_file")
    assert cli.main(["run", "-w", str(workspace), "-t", "grep,run_bash", "hi"]) == 0
    assert "tools=['read_file', 'grep', 'run_bash']" in capsys.readouterr().out


def test_cli_rejects_unknown_tool(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "build_provider", lambda spec, max_tokens=None: (None, "m"))
    assert cli.main(["run", "-t", "all", "hi"]) == 2
    assert "unknown tool 'all'" in capsys.readouterr().err


@pytest.mark.parametrize("spec", ["nope:model", "anthropic:"])
def test_bad_model_spec(spec) -> None:
    from daedalus_agent.providers import ProviderError, split_model

    with pytest.raises(ProviderError):
        split_model(spec)
