"""Helpers shared by the team modes."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from daedalus_agent.runtime import AgentResult, Runtime


async def ask_structured[T](
    runtime: Runtime,
    *,
    name: str,
    system: str,
    prompt: str,
    parse: Callable[[str], T],
    tools: Sequence[str] | None = None,
    read_only: bool = False,
    repairs: int = 1,
) -> tuple[T | None, AgentResult, str | None]:
    """Run an agent whose answer must parse; on a bad answer, ask it to fix it.

    Returns (parsed value or None, last agent result, last parse error or None).
    """
    result = await runtime.run_agent(
        name=name, system=system, prompt=prompt, tools=tools, read_only=read_only
    )
    error: str | None = None
    for attempt in range(repairs + 1):
        if not result.ok:
            return None, result, result.error
        try:
            return parse(result.text), result, None
        except ValueError as exc:
            error = str(exc)
        if attempt == repairs:
            break
        runtime.reporter.note(f"{name}: reply did not parse ({error}); asking again")
        result = await runtime.run_agent(
            name=name,
            system=system,
            prompt=(
                f"Your reply could not be used: {error}\n"
                "Reply again with only the corrected JSON object in a ```json fence."
            ),
            tools=tools,
            read_only=read_only,
            history=result.messages,
        )
    return None, result, error


def clip(text: str, limit: int = 6000) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n[... truncated ...]"
