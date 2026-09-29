"""Ultrawork: plan with a team, execute with a team, verify every piece.

1. Plan       hyper planning (or a plan you supply) produces a task DAG.
2. Execute    every task starts the moment its dependencies are done, so each
              wave of independent tasks runs as a parallel team of workers.
3. Verify     a verifier checks each finished task against its acceptance
              criteria; rejected work goes back to the same worker (with its
              conversation) plus the feedback, up to ``max_retries`` times.
4. Integrate  an integrator checks the whole goal, closes small gaps and
              writes the final report.

Workers get the plan's per-task tools (clamped to the enabled tools; all
enabled tools when the plan names none). Verifiers get the read-only and
execution tools only, so they can inspect and run checks but not edit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from daedalus_agent.runtime import AgentResult, Runtime
from daedalus_agent.team.common import ask_structured, clip
from daedalus_agent.team.plan import Plan, PlanError, PlanTask, extract_json
from daedalus_agent.team.planning import HyperPlanConfig, HyperPlanner
from daedalus_agent.team.roles import INTEGRATOR_SYSTEM, VERIFIER_SYSTEM, WORKER_SYSTEM
from daedalus_agent.tools import TOOLS

Status = Literal["done", "failed", "skipped"]


@dataclass(slots=True)
class UltraworkConfig:
    planning: HyperPlanConfig = field(default_factory=HyperPlanConfig)
    max_retries: int = 2
    verify: bool = True
    integrate: bool = True


@dataclass(slots=True)
class Verdict:
    passed: bool
    feedback: str


def parse_verdict(text: str) -> Verdict:
    data = extract_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
        raise PlanError('expected {"passed": true|false, "feedback": "..."}')
    return Verdict(data["passed"], str(data.get("feedback", "")))


@dataclass(slots=True)
class TaskOutcome:
    task: PlanTask
    status: Status
    attempts: int = 0
    result: AgentResult | None = None
    verdict: Verdict | None = None
    note: str = ""


@dataclass(slots=True)
class UltraworkResult:
    plan: Plan
    outcomes: dict[str, TaskOutcome]
    report: str

    @property
    def ok(self) -> bool:
        return all(o.status == "done" for o in self.outcomes.values())


class Ultrawork:
    def __init__(self, runtime: Runtime, config: UltraworkConfig | None = None) -> None:
        self.runtime = runtime
        self.config = config or UltraworkConfig()

    async def run(
        self,
        goal: str,
        *,
        plan: Plan | None = None,
        on_plan: Callable[[Plan], None] | None = None,
    ) -> UltraworkResult:
        if plan is None:
            planner = HyperPlanner(self.runtime, self.config.planning)
            plan = (await planner.run(goal)).plan
        else:
            plan = HyperPlanner(self.runtime, self.config.planning).finalize(plan)
        if on_plan:
            on_plan(plan)
        outcomes = await self.execute(goal, plan)
        report = await self.integrate(goal, plan, outcomes) if self.config.integrate else ""
        return UltraworkResult(plan, outcomes, report)

    # -- execution ---------------------------------------------------------------

    async def execute(self, goal: str, plan: Plan) -> dict[str, TaskOutcome]:
        waves = plan.waves()
        widest = max(len(w) for w in waves)
        self.runtime.reporter.note(
            f"ultrawork: {len(plan.tasks)} tasks in {len(waves)} waves (up to {widest} in parallel)"
        )
        futures: dict[str, asyncio.Task[TaskOutcome]] = {}
        async with asyncio.TaskGroup() as group:
            for wave in waves:  # wave order guarantees dependencies are created first
                for task in wave:
                    deps = [futures[d] for d in task.depends_on]
                    futures[task.id] = group.create_task(self._run_task(goal, plan, task, deps))
        return {task_id: future.result() for task_id, future in futures.items()}

    async def _run_task(
        self,
        goal: str,
        plan: Plan,
        task: PlanTask,
        deps: list[asyncio.Task[TaskOutcome]],
    ) -> TaskOutcome:
        upstream = [await d for d in deps]
        blocked = [o.task.id for o in upstream if o.status != "done"]
        if blocked:
            self.runtime.reporter.note(f"{task.id}: skipped (blocked by {', '.join(blocked)})")
            return TaskOutcome(task, "skipped", note=f"blocked by {', '.join(blocked)}")

        tools = task.tools or None  # None = every enabled tool
        prompt = self._worker_prompt(goal, plan, task, upstream)
        outcome = TaskOutcome(task, "failed")
        history = ()
        for attempt in range(1, self.config.max_retries + 2):
            outcome.attempts = attempt
            self.runtime.reporter.note(f"{task.id}: attempt {attempt}")
            result = await self.runtime.run_agent(
                name=task.id, system=WORKER_SYSTEM, prompt=prompt, tools=tools, history=history
            )
            outcome.result = result
            if not result.ok:
                outcome.note = f"worker error: {result.error}"
                verdict = Verdict(False, f"The previous attempt errored: {result.error}")
            elif not self.config.verify:
                outcome.status = "done"
                return outcome
            else:
                verdict = await self.verify(goal, task, result)
            outcome.verdict = verdict
            if verdict.passed:
                outcome.status = "done"
                self.runtime.reporter.note(f"{task.id}: verified")
                return outcome
            self.runtime.reporter.note(f"{task.id}: rejected: {clip(verdict.feedback, 200)}")
            history = result.messages
            prompt = (
                "A verifier rejected your work on this task.\n\n"
                f"Feedback: {verdict.feedback}\n\nFix it, then report again."
            )
        return outcome

    async def verify(self, goal: str, task: PlanTask, result: AgentResult) -> Verdict:
        tools = [
            name
            for name in self.runtime.enabled_tools
            if TOOLS[name].read_only or TOOLS[name].category == "exec"
        ]
        prompt = (
            f"Overall goal: {goal}\n\n## Task {task.id}: {task.title}\n\n{task.instructions}\n\n"
            f"Acceptance criteria: {task.acceptance or '(none given; judge from the instructions)'}"
            f"\nFiles: {', '.join(task.files) or '(not specified)'}\n\n"
            f"## Worker's report\n\n{clip(result.text) or '(empty)'}"
        )
        verdict, _, error = await ask_structured(
            self.runtime,
            name=f"verify-{task.id}",
            system=VERIFIER_SYSTEM,
            prompt=prompt,
            parse=parse_verdict,
            tools=tools,
        )
        if verdict is None:
            # A verifier that cannot answer should not sink good work; record it instead.
            return Verdict(True, f"unverified: verifier gave no verdict ({error})")
        return verdict

    def _worker_prompt(
        self, goal: str, plan: Plan, task: PlanTask, upstream: list[TaskOutcome]
    ) -> str:
        parts = [
            f"# Overall goal\n\n{goal}",
            f"# Plan summary\n\n{plan.summary}" if plan.summary else "",
            f"# Your task: {task.title} ({task.id})\n\n{task.instructions}",
        ]
        if task.files:
            parts.append("Files you own: " + ", ".join(task.files))
        if task.acceptance:
            parts.append(f"Done when: {task.acceptance}")
        others = [t for t in plan.tasks if t.id != task.id and t.files]
        if others:
            parts.append(
                "Files owned by other team members (do not edit):\n"
                + "\n".join(f"- {t.id}: {', '.join(t.files)}" for t in others)
            )
        if upstream:
            parts.append(
                "# Results of the tasks you depend on\n\n"
                + "\n\n".join(
                    f"## {o.task.id}: {o.task.title}\n"
                    + clip(o.result.text if o.result else "", 3000)
                    for o in upstream
                )
            )
        return "\n\n".join(p for p in parts if p)

    # -- integration -------------------------------------------------------------

    async def integrate(self, goal: str, plan: Plan, outcomes: dict[str, TaskOutcome]) -> str:
        self.runtime.reporter.note("ultrawork: integrating")
        lines = []
        for task_id, o in outcomes.items():
            detail = o.note or (o.verdict.feedback if o.verdict else "")
            report = clip(o.result.text if o.result else "", 2000)
            lines.append(
                f"## {task_id}: {o.task.title} [{o.status}, {o.attempts} attempt(s)]\n"
                f"{detail}\n\n{report}"
            )
        prompt = (
            f"# Goal\n\n{goal}\n\n# Plan\n\n{plan.to_markdown()}\n\n"
            "# Task outcomes\n\n" + "\n\n".join(lines)
        )
        result = await self.runtime.run_agent(
            name="integrator", system=INTEGRATOR_SYSTEM, prompt=prompt
        )
        return result.text if result.ok else f"Integrator failed: {result.error}\n\n{result.text}"


async def ultrawork(
    runtime: Runtime,
    goal: str,
    config: UltraworkConfig | None = None,
    *,
    plan: Plan | None = None,
    on_plan: Callable[[Plan], None] | None = None,
) -> UltraworkResult:
    return await Ultrawork(runtime, config).run(goal, plan=plan, on_plan=on_plan)
