from __future__ import annotations

import json

import pytest
from conftest import Request, call, reply

from daedalus_agent.team import (
    HyperPlanConfig,
    HyperPlanner,
    Plan,
    PlanError,
    Ultrawork,
    UltraworkConfig,
    parse_plan,
)
from daedalus_agent.team.plan import extract_json


def plan_dict(**overrides) -> dict:
    data = {
        "goal": "g",
        "summary": "s",
        "tasks": [
            {"id": "a", "title": "A", "instructions": "do a", "files": ["a.txt"]},
            {"id": "b", "title": "B", "instructions": "do b", "files": ["b.txt"]},
            {
                "id": "c",
                "title": "C",
                "instructions": "combine",
                "depends_on": ["a", "b"],
                "files": ["c.txt"],
            },
        ],
        "verification": "check c.txt",
    }
    data.update(overrides)
    return data


def fenced(data: dict) -> str:
    return "Here you go.\n```json\n" + json.dumps(data) + "\n```"


# --- plans --------------------------------------------------------------------


def test_plan_waves() -> None:
    plan = Plan.model_validate(plan_dict())
    assert [[t.id for t in w] for w in plan.waves()] == [["a", "b"], ["c"]]
    assert "Wave 1 (2 tasks in parallel)" in plan.to_markdown()


@pytest.mark.parametrize(
    "tasks, message",
    [
        ([{"id": "a", "title": "", "instructions": "", "depends_on": ["zz"]}], "unknown"),
        (
            [
                {"id": "a", "title": "", "instructions": "", "depends_on": ["b"]},
                {"id": "b", "title": "", "instructions": "", "depends_on": ["a"]},
            ],
            "cycle",
        ),
        (
            [
                {"id": "a", "title": "", "instructions": ""},
                {"id": "a", "title": "", "instructions": ""},
            ],
            "duplicate",
        ),
    ],
)
def test_plan_graph_errors(tasks, message) -> None:
    with pytest.raises(ValueError, match=message):
        Plan.model_validate(plan_dict(tasks=tasks))


def test_file_conflicts_are_serialised_without_cycles() -> None:
    plan = Plan.model_validate(
        plan_dict(
            tasks=[
                {"id": "a", "title": "", "instructions": "", "depends_on": ["c"], "files": ["x"]},
                {"id": "b", "title": "", "instructions": "", "files": ["x", "y"]},
                {"id": "c", "title": "", "instructions": "", "files": ["y"]},
            ]
        )
    )
    assert plan.file_conflicts()
    fixed = plan.serialize_conflicts()
    assert fixed.file_conflicts() == []
    fixed.waves()  # no cycle


def test_extract_json_variants() -> None:
    assert extract_json('noise {"a": 1} noise') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```\n```json\n{"a": 2}\n```') == {"a": 2}
    with pytest.raises(PlanError):
        extract_json("no json here")
    with pytest.raises(PlanError):
        parse_plan('{"goal": "x", "tasks": []}')


# --- hyper planning -----------------------------------------------------------


def planning_script(bad_first_plan: bool = False):
    seen_bad = {"done": False}

    def script(r: Request):
        if r.role("researcher"):
            if r.tool_result is None:
                return call("list_dir")
            return reply(f"found: {r.tool_result}")
        if r.role("planner"):
            if bad_first_plan and not seen_bad["done"]:
                seen_bad["done"] = True
                return reply("I think we should just do it.")
            return reply(fenced(plan_dict()))
        if r.role("plan critic"):
            return reply(fenced({"score": 9, "strengths": ["fine"], "problems": []}))
        if r.role("lead planner"):
            return reply(fenced(plan_dict(summary="merged")))
        raise AssertionError(r.system)

    return script


async def test_hyper_plan_runs_a_team(make_runtime, workspace) -> None:
    (workspace / "src").mkdir()
    runtime = make_runtime(planning_script(), tools=["list_dir", "write_file"], delay=0.02)
    config = HyperPlanConfig(researchers=3, planners=3)
    result = await HyperPlanner(runtime, config).run("add a feature")
    assert result.plan.summary == "merged"
    assert len(result.research) == 3
    assert "src/" in result.research[0][1].text
    assert len(result.candidates) == 3
    assert all(c.critique and c.critique.score == 9 for c in result.candidates)
    assert len(result.reviews) == 1  # reviewers were happy, so no revision
    assert runtime.scripted.peak >= 3  # researchers/planners/critics ran concurrently
    planners = [r for r in runtime.scripted.requests if r.role("planner")]
    assert all("Research findings" in r.prompt and "src/" in r.prompt for r in planners)
    # Planning never gets write access.
    planning = [r for r in runtime.scripted.requests if not r.role("team member")]
    assert all("write_file" not in r.tools for r in planning)


async def test_planner_bad_reply_is_repaired(make_runtime) -> None:
    runtime = make_runtime(planning_script(bad_first_plan=True), tools=["list_dir"])
    config = HyperPlanConfig(researchers=0, planners=1, critique=False, review_rounds=0)
    result = await HyperPlanner(runtime, config).run("goal")
    assert result.candidates[0].plan is not None


async def test_review_triggers_revision(make_runtime) -> None:
    reviews = iter([3, 9, 9])

    def script(r: Request):
        if r.role("plan critic"):
            score = next(reviews, 9)
            return reply(fenced({"score": score, "problems": ["gap"] if score < 8 else []}))
        if r.role("lead planner"):
            summary = "revised" if "Reviews" in r.prompt else "first"
            return reply(fenced(plan_dict(summary=summary)))
        return reply(fenced(plan_dict()))

    runtime = make_runtime(script)
    config = HyperPlanConfig(researchers=0, planners=1, critique=False, review_rounds=2)
    result = await HyperPlanner(runtime, config).run("goal")
    assert result.plan.summary == "revised"
    assert len(result.reviews) == 2


async def test_finalize_clamps_tools(make_runtime) -> None:
    runtime = make_runtime(lambda r: reply(""), tools=["read_file"])
    plan = Plan.model_validate(
        plan_dict(
            tasks=[{"id": "a", "title": "", "instructions": "", "tools": ["run_bash", "read_file"]}]
        )
    )
    assert HyperPlanner(runtime).finalize(plan).tasks[0].tools == ["read_file"]


# --- ultrawork ----------------------------------------------------------------


def work_script(reject_once: set[str] = frozenset(), fail: set[str] = frozenset()):
    rejected: set[str] = set()

    def script(r: Request):
        if r.role("team member"):
            if r.tool_result is None:
                first = r.messages[0].content
                tid = next(t for t in "abc" if f"Your task: {t.upper()} ({t})" in first)
                if tid in fail:
                    return reply("I could not do it")
                return call("write_file", path=f"{tid}.txt", content=f"{tid} done")
            return reply(f"wrote it: {r.tool_result}")
        if r.role("verifier"):
            for tid in ("a", "b", "c"):
                if f"## Task {tid}:" in r.prompt:
                    break
            if tid in fail:
                return reply(fenced({"passed": False, "feedback": "missing"}))
            if tid in reject_once and tid not in rejected:
                rejected.add(tid)
                return reply(fenced({"passed": False, "feedback": "try again"}))
            return reply(fenced({"passed": True, "feedback": "good"}))
        if r.role("integrator"):
            return reply("all integrated")
        raise AssertionError(r.system)

    return script


async def test_ultrawork_executes_dag_in_parallel(make_runtime, workspace) -> None:
    runtime = make_runtime(work_script(reject_once={"b"}), tools=["write_file"], delay=0.02)
    plan = Plan.model_validate(plan_dict())
    result = await Ultrawork(runtime).run("g", plan=plan)
    assert result.ok, result.outcomes
    assert result.report == "all integrated"
    assert {p.name for p in workspace.iterdir()} == {"a.txt", "b.txt", "c.txt"}
    assert result.outcomes["a"].attempts == 1
    assert result.outcomes["b"].attempts == 2  # verifier rejected once, worker fixed it
    assert runtime.scripted.peak >= 2  # a and b ran as a parallel team
    # c ran only after a and b, and saw their reports.
    c_prompt = next(
        r.messages[0].content
        for r in runtime.scripted.requests
        if "Your task: C" in str(r.messages[0].content)
    )
    assert "Results of the tasks you depend on" in c_prompt and "## a: A" in c_prompt
    # Verifiers cannot write.
    verifiers = [r for r in runtime.scripted.requests if r.role("verifier")]
    assert verifiers and all("write_file" not in r.tools for r in verifiers)


async def test_ultrawork_skips_blocked_tasks(make_runtime) -> None:
    runtime = make_runtime(work_script(fail={"a"}), tools=["write_file"])
    config = UltraworkConfig(max_retries=1)
    result = await Ultrawork(runtime, config).run("g", plan=Plan.model_validate(plan_dict()))
    assert not result.ok
    assert result.outcomes["a"].status == "failed" and result.outcomes["a"].attempts == 2
    assert result.outcomes["b"].status == "done"
    assert result.outcomes["c"].status == "skipped"


async def test_ultrawork_plans_with_hyper_planning(make_runtime, workspace) -> None:
    plan_script = planning_script()
    work = work_script()

    def script(r: Request):
        if r.role("team member") or r.role("verifier") or r.role("integrator"):
            return work(r)
        return plan_script(r)

    runtime = make_runtime(script, tools=["list_dir", "write_file"])
    seen = []
    result = await Ultrawork(runtime).run("g", on_plan=seen.append)
    assert result.ok and seen and seen[0].summary == "merged"
    assert (workspace / "c.txt").read_text() == "c done"
