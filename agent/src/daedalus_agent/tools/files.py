"""File-system tools: read_file, write_file, edit_file, list_dir, glob, grep."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from daedalus_agent.tools.base import ToolContext, ToolError, ToolSpec

_SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "node_modules", ".tox", ".mypy_cache"}


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


# --- read_file ---------------------------------------------------------------


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path, relative to the workspace.")
    offset: int = Field(default=1, ge=1, description="First line to return (1-based).")
    limit: int = Field(default=2000, ge=1, le=20_000, description="Maximum lines to return.")


async def _read_file(ctx: ToolContext, args: ReadFileArgs) -> str:
    path = ctx.resolve(args.path)
    if not path.is_file():
        raise ToolError(f"{args.path}: not a file")
    data = path.read_bytes()
    if _is_binary(data):
        raise ToolError(f"{args.path}: binary file ({len(data)} bytes)")
    lines = data.decode("utf-8", errors="replace").splitlines()
    start = args.offset - 1
    chunk = lines[start : start + args.limit]
    numbered = "\n".join(f"{start + i + 1:6}\t{line}" for i, line in enumerate(chunk))
    more = len(lines) - (start + len(chunk))
    if more > 0:
        numbered += f"\n[{more} more lines; continue with offset={start + len(chunk) + 1}]"
    return ctx.truncate(numbered) if chunk else f"{args.path}: no lines at offset {args.offset}"


READ_FILE = ToolSpec(
    name="read_file",
    description="Read a UTF-8 text file from the workspace, with line numbers.",
    args=ReadFileArgs,
    run=_read_file,
    read_only=True,
    category="files",
)


# --- write_file --------------------------------------------------------------


class WriteFileArgs(BaseModel):
    path: str = Field(description="File path, relative to the workspace.")
    content: str = Field(description="Full new content of the file.")


async def _write_file(ctx: ToolContext, args: WriteFileArgs) -> str:
    path = ctx.resolve(args.path)
    if path.is_dir():
        raise ToolError(f"{args.path}: is a directory")
    async with ctx.lock(path):
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.content, encoding="utf-8")
    verb = "Overwrote" if existed else "Created"
    return f"{verb} {ctx.display(path)} ({len(args.content)} chars)"


WRITE_FILE = ToolSpec(
    name="write_file",
    description="Create or overwrite a text file in the workspace (parent dirs are created).",
    args=WriteFileArgs,
    run=_write_file,
    category="files",
    guidelines=("Prefer edit_file for small changes to existing files.",),
)


# --- edit_file ---------------------------------------------------------------


class EditFileArgs(BaseModel):
    path: str = Field(description="File path, relative to the workspace.")
    old_text: str = Field(description="Exact text to replace. Must match exactly once.")
    new_text: str = Field(description="Replacement text.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


async def _edit_file(ctx: ToolContext, args: EditFileArgs) -> str:
    path = ctx.resolve(args.path)
    if not path.is_file():
        raise ToolError(f"{args.path}: not a file")
    if not args.old_text:
        raise ToolError("old_text must not be empty")
    async with ctx.lock(path):
        text = path.read_text(encoding="utf-8")
        count = text.count(args.old_text)
        if count == 0:
            raise ToolError(f"{args.path}: old_text not found")
        if count > 1 and not args.replace_all:
            raise ToolError(
                f"{args.path}: old_text matches {count} times; add context or set replace_all"
            )
        text = text.replace(args.old_text, args.new_text, -1 if args.replace_all else 1)
        path.write_text(text, encoding="utf-8")
    return f"Edited {ctx.display(path)} ({count if args.replace_all else 1} replacement(s))"


EDIT_FILE = ToolSpec(
    name="edit_file",
    description="Replace an exact snippet of text in a workspace file.",
    args=EditFileArgs,
    run=_edit_file,
    category="files",
)


# --- list_dir ----------------------------------------------------------------


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="Directory, relative to the workspace.")


async def _list_dir(ctx: ToolContext, args: ListDirArgs) -> str:
    path = ctx.resolve(args.path)
    if not path.is_dir():
        raise ToolError(f"{args.path}: not a directory")
    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
    lines = [f"{p.name}/" if p.is_dir() else p.name for p in entries]
    return ctx.truncate("\n".join(lines)) if lines else "(empty directory)"


LIST_DIR = ToolSpec(
    name="list_dir",
    description="List the entries of a workspace directory.",
    args=ListDirArgs,
    run=_list_dir,
    read_only=True,
    category="files",
)


# --- glob --------------------------------------------------------------------


class GlobArgs(BaseModel):
    pattern: str = Field(description="Glob pattern such as '**/*.py'.")
    path: str = Field(default=".", description="Directory to search from.")
    limit: int = Field(default=500, ge=1, le=5000)


async def _glob(ctx: ToolContext, args: GlobArgs) -> str:
    root = ctx.resolve(args.path)
    matches = []
    for p in sorted(root.glob(args.pattern)):
        if _SKIP_DIRS.intersection(p.relative_to(root).parts) or not ctx.contains(p):
            continue
        matches.append(ctx.display(p))
        if len(matches) >= args.limit:
            matches.append(f"[stopped at limit={args.limit}]")
            break
    return "\n".join(matches) or "(no matches)"


GLOB = ToolSpec(
    name="glob",
    description="Find workspace files by glob pattern.",
    args=GlobArgs,
    run=_glob,
    read_only=True,
    category="files",
)


# --- grep --------------------------------------------------------------------


class GrepArgs(BaseModel):
    pattern: str = Field(description="Python regular expression.")
    path: str = Field(default=".", description="File or directory to search.")
    glob: str = Field(default="**/*", description="Only search files matching this glob.")
    ignore_case: bool = False
    limit: int = Field(default=200, ge=1, le=5000, description="Maximum matching lines.")


async def _grep(ctx: ToolContext, args: GrepArgs) -> str:
    try:
        rx = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"bad pattern: {exc}") from None
    root = ctx.resolve(args.path)
    files = [root] if root.is_file() else sorted(root.glob(args.glob))
    out: list[str] = []
    for f in files:
        if not f.is_file() or not ctx.contains(f):
            continue
        if f != root and _SKIP_DIRS.intersection(f.relative_to(root).parts):
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if _is_binary(data):
            continue
        for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                out.append(f"{ctx.display(f)}:{n}: {line[:400]}")
                if len(out) >= args.limit:
                    out.append(f"[stopped at limit={args.limit}]")
                    return "\n".join(out)
    return "\n".join(out) or "(no matches)"


GREP = ToolSpec(
    name="grep",
    description="Search workspace file contents with a regular expression.",
    args=GrepArgs,
    run=_grep,
    read_only=True,
    category="files",
)
