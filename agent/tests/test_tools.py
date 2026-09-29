from __future__ import annotations

import json
from pathlib import Path

import pytest

from daedalus_agent.tools import (
    TOOLS,
    ToolContext,
    ToolError,
    UnknownToolError,
    build_agent_tool,
    parse_tool_names,
)
from daedalus_agent.tools.web import html_to_text


async def run(ctx: ToolContext, name: str, **args: object) -> str:
    tool = build_agent_tool(TOOLS[name], ctx)
    return (await tool.execute("id", args)).text


def test_parse_tool_names_is_explicit() -> None:
    assert parse_tool_names([]) == []
    assert parse_tool_names([""]) == []
    assert parse_tool_names(["read_file,grep", "read_file", " run_bash "]) == [
        "read_file",
        "grep",
        "run_bash",
    ]
    for bad in ("all", "*", "bash"):
        with pytest.raises(UnknownToolError):
            parse_tool_names([bad])


def test_expected_tools_exist() -> None:
    assert {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "glob",
        "grep",
        "web_fetch",
        "run_bash",
        "run_python",
        "delegate",
    } <= set(TOOLS)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_schemas_are_plain(name: str) -> None:
    schema = TOOLS[name].parameters
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert schema["type"] == "object"
    assert "title" not in schema


async def test_write_read_edit(ctx: ToolContext, workspace: Path) -> None:
    assert "Created" in await run(ctx, "write_file", path="a/b.txt", content="one\ntwo\ntwo\n")
    assert (workspace / "a/b.txt").read_text() == "one\ntwo\ntwo\n"
    out = await run(ctx, "read_file", path="a/b.txt")
    assert "1\tone" in out and "3\ttwo" in out
    with pytest.raises(ToolError, match="2 times"):
        await run(ctx, "edit_file", path="a/b.txt", old_text="two", new_text="2")
    await run(ctx, "edit_file", path="a/b.txt", old_text="one", new_text="1")
    await run(ctx, "edit_file", path="a/b.txt", old_text="two", new_text="2", replace_all=True)
    assert (workspace / "a/b.txt").read_text() == "1\n2\n2\n"


async def test_read_paging(ctx: ToolContext, workspace: Path) -> None:
    (workspace / "f").write_text("\n".join(str(i) for i in range(1, 11)))
    out = await run(ctx, "read_file", path="f", offset=3, limit=2)
    assert "3\t3" in out and "4\t4" in out and "5\t5" not in out
    assert "offset=5" in out


async def test_workspace_escape_is_refused(ctx: ToolContext, workspace: Path) -> None:
    (workspace.parent / "secret").write_text("x")
    with pytest.raises(ToolError, match="outside the workspace"):
        await run(ctx, "read_file", path="../secret")
    with pytest.raises(ToolError, match="outside the workspace"):
        await run(ctx, "write_file", path="/etc/passwd-copy", content="x")
    (workspace / "link").symlink_to(workspace.parent / "secret")
    with pytest.raises(ToolError, match="outside the workspace"):
        await run(ctx, "read_file", path="link")
    assert "secret" not in await run(ctx, "glob", pattern="../*")


async def test_validation_errors_are_readable(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="Invalid arguments for read_file: path"):
        await run(ctx, "read_file")


async def test_list_glob_grep(ctx: ToolContext, workspace: Path) -> None:
    (workspace / "pkg").mkdir()
    (workspace / "pkg/mod.py").write_text("def hello():\n    return 'hi'\n")
    (workspace / "README").write_text("Hello world\n")
    (workspace / ".git").mkdir()
    (workspace / ".git/config").write_text("hello\n")
    assert (await run(ctx, "list_dir")).splitlines()[:2] == [".git/", "pkg/"]
    assert await run(ctx, "glob", pattern="**/*.py") == "pkg/mod.py"
    out = await run(ctx, "grep", pattern="hello", ignore_case=True)
    assert "pkg/mod.py:1:" in out and "README:1:" in out and ".git" not in out


async def test_run_bash(ctx: ToolContext, workspace: Path) -> None:
    out = await run(ctx, "run_bash", command="pwd; echo err >&2; exit 3")
    assert out.startswith("[exit code 3]")
    assert str(workspace) in out and "err" in out
    out = await run(ctx, "run_bash", command="sleep 5", timeout=0.2)
    assert "timeout" in out


async def test_run_python(ctx: ToolContext) -> None:
    out = await run(ctx, "run_python", code="import os\nprint(os.getcwd(), 6 * 7)")
    assert "[exit code 0]" in out and "42" in out


async def test_web_fetch_rejects_other_schemes(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="only http"):
        await run(ctx, "web_fetch", url="file:///etc/passwd")


def test_html_to_text() -> None:
    text = html_to_text(
        "<html><head><title>x</title><style>p{}</style></head>"
        "<body><h1>Title</h1><p>Hello &amp; <a href='https://e.x/'>link</a></p>"
        "<script>evil()</script></body></html>"
    )
    assert "Title" in text and "Hello & " in text and "<https://e.x/>" in text
    assert "evil" not in text and "p{}" not in text


def test_output_truncation(ctx: ToolContext) -> None:
    ctx.max_output_chars = 100
    out = ctx.truncate("x" * 1000)
    assert "900 characters truncated" in out and len(out) < 200
