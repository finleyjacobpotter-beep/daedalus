"""Plans: a DAG of tasks that a team of agents can execute."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator


class PlanError(ValueError):
    pass


class PlanTask(BaseModel):
    id: str
    title: str
    instructions: str
    depends_on: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    acceptance: str = ""


class Plan(BaseModel):
    goal: str
    summary: str = ""
    tasks: list[PlanTask] = Field(min_length=1)
    verification: str = ""

    @model_validator(mode="after")
    def _check_graph(self) -> Plan:
        ids = [t.id for t in self.tasks]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate task ids: {', '.join(dupes)}")
        known = set(ids)
        for task in self.tasks:
            missing = [d for d in task.depends_on if d not in known]
            if missing:
                raise ValueError(f"task {task.id} depends on unknown task(s): {', '.join(missing)}")
            if task.id in task.depends_on:
                raise ValueError(f"task {task.id} depends on itself")
        self.waves()  # raises on cycles
        return self

    def task(self, task_id: str) -> PlanTask:
        return next(t for t in self.tasks if t.id == task_id)

    def waves(self) -> list[list[PlanTask]]:
        """Group tasks into waves; every task in a wave can run in parallel."""
        done: set[str] = set()
        remaining = list(self.tasks)
        waves: list[list[PlanTask]] = []
        while remaining:
            ready = [t for t in remaining if set(t.depends_on) <= done]
            if not ready:
                cycle = ", ".join(t.id for t in remaining)
                raise ValueError(f"dependency cycle among: {cycle}")
            waves.append(ready)
            done.update(t.id for t in ready)
            remaining = [t for t in remaining if t.id not in done]
        return waves

    def file_conflicts(self) -> list[tuple[str, str, str]]:
        """Pairs of tasks that may run concurrently yet claim the same file."""
        ancestors: dict[str, set[str]] = {}

        def ancestors_of(task_id: str) -> set[str]:
            if task_id not in ancestors:
                deps = self.task(task_id).depends_on
                ancestors[task_id] = set(deps).union(*(ancestors_of(d) for d in deps))
            return ancestors[task_id]

        conflicts = []
        for i, a in enumerate(self.tasks):
            for b in self.tasks[i + 1 :]:
                if a.id in ancestors_of(b.id) or b.id in ancestors_of(a.id):
                    continue
                for path in sorted(set(a.files) & set(b.files)):
                    conflicts.append((a.id, b.id, path))
        return conflicts

    def serialize_conflicts(self) -> Plan:
        """Add dependencies so concurrent tasks never edit the same file."""
        plan = self.model_copy(deep=True)
        # Always point the new edge along one topological order, so it cannot close a cycle.
        order = {t.id: n for n, t in enumerate(t for wave in plan.waves() for t in wave)}
        for a_id, b_id, _ in plan.file_conflicts():
            first, second = sorted((a_id, b_id), key=order.__getitem__)
            later = plan.task(second)
            if first not in later.depends_on:
                later.depends_on.append(first)
        return Plan.model_validate(plan.model_dump())

    def to_markdown(self) -> str:
        lines = [f"# Plan: {self.goal}", ""]
        if self.summary:
            lines += [self.summary, ""]
        for n, wave in enumerate(self.waves(), 1):
            plural = "s" if len(wave) != 1 else ""
            lines.append(f"## Wave {n} ({len(wave)} task{plural} in parallel)")
            lines.append("")
            for t in wave:
                deps = f" (after {', '.join(t.depends_on)})" if t.depends_on else ""
                lines.append(f"### {t.id}: {t.title}{deps}")
                lines.append("")
                lines.append(t.instructions)
                if t.files:
                    lines.append(f"\nFiles: {', '.join(t.files)}")
                if t.tools:
                    lines.append(f"Tools: {', '.join(t.tools)}")
                if t.acceptance:
                    lines.append(f"Done when: {t.acceptance}")
                lines.append("")
        if self.verification:
            lines += ["## Final verification", "", self.verification, ""]
        return "\n".join(lines)


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Pull the last JSON object out of a model reply (fenced or bare)."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    candidates.reverse()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise PlanError("no JSON object found in the reply")


def parse_plan(text: str) -> Plan:
    data = extract_json(text)
    try:
        return Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanError(str(exc)) from None
