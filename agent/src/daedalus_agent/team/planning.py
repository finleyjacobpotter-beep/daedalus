"""Hyper planning: a team researches, plans, critiques and synthesises one plan.

1. Research   N researchers, each with a different lens, read the workspace in parallel.
2. Plan       M planners, each with a different strategy, write competing plans in parallel.
3. Critique   one critic per candidate plan reviews it in parallel.
4. Synthesis  a lead planner merges the candidates and critiques into one plan.
5. Review     K critics review the merged plan; if any finds real problems, it is revised.
              Repeated for ``review_rounds`` rounds.

Researchers, planners and critics only get the read-only subset of the enabled
tools. The finished plan's per-task tools are clamped to the enabled tools, and
tasks that could run concurrently on the same file are serialised.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from daedalus_agent.runtime import AgentResult, Runtime
from daedalus_agent.team.common import ask_structured, clip
from daedalus_agent.team.plan import Plan, PlanError, extract_json, parse_plan
from daedalus_agent.team.roles import (
    CRITIC_SYSTEM,
    PLAN_FORMAT,
    PLANNER_STRATEGIES,
    PLANNER_SYSTEM,
    RESEARCH_LENSES,
    RESEARCHER_SYSTEM,
    SYNTHESIZER_SYSTEM,
)


@dataclass(slots=True)
class HyperPlanConfig:
    researchers: int = len(RESEARCH_LENSES)
    planners: int = 4
    critique: bool = True
    review_rounds: int = 1
    reviewers: int = 2
    # A reviewed plan is accepted when every reviewer scores it at least this.
    accept_score: int = 8

    def __post_init__(self) -> None:
        self.researchers = max(0, min(self.researchers, len(RESEARCH_LENSES)))
        self.planners = max(1, min(self.planners, len(PLANNER_STRATEGIES)))
        self.review_rounds = max(0, self.review_rounds)
        self.reviewers = max(1, self.reviewers)


@dataclass(slots=True)
class Critique:
    score: int
    strengths: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = [f"Score: {self.score}/10"]
        for title, items in (
            ("Strengths", self.strengths),
            ("Problems", self.problems),
            ("Suggestions", self.suggestions),
        ):
            if items:
                parts.append(f"{title}:\n" + "\n".join(f"- {i}" for i in items))
        return "\n".join(parts)


def parse_critique(text: str) -> Critique:
    data = extract_json(text)
    if not isinstance(data, dict) or "score" not in data:
        raise PlanError('expected {"score": ..., "problems": [...], ...}')
    try:
        score = max(1, min(10, int(data["score"])))
    except (TypeError, ValueError):
        raise PlanError("score must be a number from 1 to 10") from None

    def strings(key: str) -> list[str]:
        value = data.get(key) or []
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    return Critique(score, strings("strengths"), strings("problems"), strings("suggestions"))


@dataclass(slots=True)
class Candidate:
    strategy: str
    plan: Plan | None
    result: AgentResult
    error: str | None = None
    critique: Critique | None = None


@dataclass(slots=True)
class HyperPlanResult:
    plan: Plan
    research: list[tuple[str, AgentResult]]
    candidates: list[Candidate]
    reviews: list[list[Critique]]


class HyperPlanner:
    def __init__(self, runtime: Runtime, config: HyperPlanConfig | None = None) -> None:
        self.runtime = runtime
        self.config = config or HyperPlanConfig()
        self.plan_format = PLAN_FORMAT.replace(
            "{tools}", ", ".join(runtime.enabled_tools) or "(none enabled)"
        )

    @property
    def can_read(self) -> bool:
        return bool(self.runtime.allowed(None, read_only=True))

    async def run(self, goal: str) -> HyperPlanResult:
        research = await self.research(goal)
        candidates = await self.propose(goal, research)
        if self.config.critique:
            await self.critique(goal, research, candidates)
        plan = await self.synthesize(goal, research, candidates)
        reviews: list[list[Critique]] = []
        for round_no in range(1, self.config.review_rounds + 1):
            critiques = await self.review(goal, research, plan, round_no)
            reviews.append(critiques)
            if all(c.score >= self.config.accept_score and not c.problems for c in critiques):
                break
            plan = await self.revise(goal, research, plan, critiques, round_no)
        return HyperPlanResult(self.finalize(plan), research, candidates, reviews)

    # -- phases ----------------------------------------------------------------

    async def research(self, goal: str) -> list[tuple[str, AgentResult]]:
        lenses = RESEARCH_LENSES[: self.config.researchers]
        if not lenses:
            return []
        if not self.can_read:
            self.runtime.reporter.note("research skipped: no read-only tools enabled")
            return []
        self.runtime.reporter.note(f"hyper planning: {len(lenses)} researchers")
        results = await asyncio.gather(
            *(
                self.runtime.run_agent(
                    name=f"research-{lens}",
                    system=RESEARCHER_SYSTEM,
                    prompt=f"Goal: {goal}\n\nYour lens: {focus}",
                    read_only=True,
                )
                for lens, focus in lenses
            )
        )
        return [(lens, r) for (lens, _), r in zip(lenses, results, strict=True)]

    async def propose(self, goal: str, research: list[tuple[str, AgentResult]]) -> list[Candidate]:
        strategies = PLANNER_STRATEGIES[: self.config.planners]
        self.runtime.reporter.note(f"hyper planning: {len(strategies)} competing planners")
        system = PLANNER_SYSTEM.replace("{plan_format}", self.plan_format)
        briefing = self._briefing(goal, research)

        async def one(strategy: str, how: str) -> Candidate:
            plan, result, error = await ask_structured(
                self.runtime,
                name=f"planner-{strategy}",
                system=system,
                prompt=f"{briefing}\n\nYour strategy: {how}",
                parse=parse_plan,
                read_only=True,
            )
            return Candidate(strategy, plan, result, error)

        candidates = await asyncio.gather(*(one(s, how) for s, how in strategies))
        if not any(c.plan for c in candidates):
            errors = "; ".join(f"{c.strategy}: {c.error}" for c in candidates)
            raise PlanError(f"no planner produced a usable plan ({errors})")
        return list(candidates)

    async def critique(
        self, goal: str, research: list[tuple[str, AgentResult]], candidates: list[Candidate]
    ) -> None:
        usable = [c for c in candidates if c.plan]
        self.runtime.reporter.note(f"hyper planning: {len(usable)} critics")
        briefing = self._briefing(goal, research)

        async def one(candidate: Candidate) -> None:
            assert candidate.plan is not None
            critique, _, _ = await ask_structured(
                self.runtime,
                name=f"critic-{candidate.strategy}",
                system=CRITIC_SYSTEM,
                prompt=f"{briefing}\n\nCandidate plan ({candidate.strategy}):\n"
                + _plan_json(candidate.plan),
                parse=parse_critique,
                read_only=True,
            )
            candidate.critique = critique

        await asyncio.gather(*(one(c) for c in usable))

    async def synthesize(
        self, goal: str, research: list[tuple[str, AgentResult]], candidates: list[Candidate]
    ) -> Plan:
        self.runtime.reporter.note("hyper planning: synthesising the final plan")
        sections = []
        for c in candidates:
            if not c.plan:
                continue
            section = f"### Candidate: {c.strategy}\n{_plan_json(c.plan)}"
            if c.critique:
                section += f"\n\nCritique:\n{c.critique.render()}"
            sections.append(section)
        prompt = f"{self._briefing(goal, research)}\n\n## Candidate plans\n\n" + "\n\n".join(
            sections
        )
        return await self._plan_from(
            "lead-planner", SYNTHESIZER_SYSTEM, prompt, fallback=_best(candidates)
        )

    async def review(
        self, goal: str, research: list[tuple[str, AgentResult]], plan: Plan, round_no: int
    ) -> list[Critique]:
        self.runtime.reporter.note(
            f"hyper planning: review round {round_no} ({self.config.reviewers} reviewers)"
        )
        prompt = f"{self._briefing(goal, research)}\n\nPlan under review:\n{_plan_json(plan)}"
        results = await asyncio.gather(
            *(
                ask_structured(
                    self.runtime,
                    name=f"reviewer-{round_no}.{n}",
                    system=CRITIC_SYSTEM,
                    prompt=prompt,
                    parse=parse_critique,
                    read_only=True,
                )
                for n in range(1, self.config.reviewers + 1)
            )
        )
        return [critique for critique, _, _ in results if critique is not None]

    async def revise(
        self,
        goal: str,
        research: list[tuple[str, AgentResult]],
        plan: Plan,
        critiques: list[Critique],
        round_no: int,
    ) -> Plan:
        self.runtime.reporter.note(f"hyper planning: revising after review round {round_no}")
        reviews = "\n\n".join(f"### Review {n}\n{c.render()}" for n, c in enumerate(critiques, 1))
        prompt = (
            f"{self._briefing(goal, research)}\n\n## Current plan\n{_plan_json(plan)}\n\n"
            f"## Reviews\n{reviews}\n\nRevise the plan to address every problem raised."
        )
        return await self._plan_from(f"lead-planner-r{round_no}", SYNTHESIZER_SYSTEM, prompt, plan)

    # -- helpers ---------------------------------------------------------------

    async def _plan_from(self, name: str, system: str, prompt: str, fallback: Plan) -> Plan:
        plan, _, error = await ask_structured(
            self.runtime,
            name=name,
            system=system.replace("{plan_format}", self.plan_format),
            prompt=prompt,
            parse=parse_plan,
            read_only=True,
        )
        if plan is None:
            self.runtime.reporter.note(f"{name} failed ({error}); keeping the best plan so far")
            return fallback
        return plan

    def finalize(self, plan: Plan) -> Plan:
        enabled = set(self.runtime.enabled_tools)
        plan = plan.model_copy(deep=True)
        for task in plan.tasks:
            task.tools = [t for t in task.tools if t in enabled]
        return plan.serialize_conflicts()

    def _briefing(self, goal: str, research: list[tuple[str, AgentResult]]) -> str:
        text = f"# Goal\n\n{goal}"
        findings = [(lens, r) for lens, r in research if r.text]
        if findings:
            text += "\n\n# Research findings\n\n" + "\n\n".join(
                f"## {lens}\n{clip(r.text)}" for lens, r in findings
            )
        return text


def _plan_json(plan: Plan) -> str:
    return "```json\n" + json.dumps(plan.model_dump(), indent=2) + "\n```"


def _best(candidates: list[Candidate]) -> Plan:
    usable = [c for c in candidates if c.plan]
    usable.sort(key=lambda c: c.critique.score if c.critique else 0, reverse=True)
    plan = usable[0].plan
    assert plan is not None
    return plan


async def hyper_plan(
    runtime: Runtime, goal: str, config: HyperPlanConfig | None = None
) -> HyperPlanResult:
    return await HyperPlanner(runtime, config).run(goal)
