"""The tool registry.

Every tool the agent could ever use is listed in :data:`TOOLS`. None of them is
enabled by default: the operator names each one (``--tool read_file --tool
run_bash`` or ``DAEDALUS_AGENT_TOOLS=read_file,run_bash``). There is
deliberately no "all" shortcut.
"""

from __future__ import annotations

from collections.abc import Iterable

from daedalus_agent.tools.base import ToolContext, ToolError, ToolSpec, build_agent_tool
from daedalus_agent.tools.delegate import DELEGATE
from daedalus_agent.tools.exec import RUN_BASH, RUN_PYTHON
from daedalus_agent.tools.files import EDIT_FILE, GLOB, GREP, LIST_DIR, READ_FILE, WRITE_FILE
from daedalus_agent.tools.web import WEB_FETCH

TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        READ_FILE,
        WRITE_FILE,
        EDIT_FILE,
        LIST_DIR,
        GLOB,
        GREP,
        WEB_FETCH,
        RUN_BASH,
        RUN_PYTHON,
        DELEGATE,
    )
}


class UnknownToolError(ValueError):
    pass


def parse_tool_names(values: Iterable[str]) -> list[str]:
    """Split comma lists, drop blanks and duplicates, and reject unknown names."""
    names: list[str] = []
    for value in values:
        for raw in value.split(","):
            name = raw.strip()
            if not name or name in names:
                continue
            if name not in TOOLS:
                known = ", ".join(sorted(TOOLS))
                raise UnknownToolError(f"unknown tool {name!r} (known tools: {known})")
            names.append(name)
    return names


__all__ = [
    "TOOLS",
    "ToolContext",
    "ToolError",
    "ToolSpec",
    "UnknownToolError",
    "build_agent_tool",
    "parse_tool_names",
]
