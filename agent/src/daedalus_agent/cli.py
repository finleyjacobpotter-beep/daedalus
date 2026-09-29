"""daedalus-agent command line.

daedalus-agent tools                                list every tool (none is on by default)
daedalus-agent run -t read_file -t grep "question"  one agent, only the tools you name
daedalus-agent hyperplan -t read_file ... "goal"    a planning team produces a plan
daedalus-agent ultrawork -t read_file ... "goal"    plan with a team, build with a team
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from daedalus_agent import __version__
from daedalus_agent.providers import DEFAULT_MODEL, ProviderError, build_provider
from daedalus_agent.runtime import ConsoleReporter, Runtime
from daedalus_agent.team import (
    HyperPlanConfig,
    HyperPlanner,
    Plan,
    Ultrawork,
    UltraworkConfig,
)
from daedalus_agent.team.roles import PLANNER_STRATEGIES, RESEARCH_LENSES
from daedalus_agent.tools import TOOLS, ToolContext, UnknownToolError, parse_tool_names

MAIN_AGENT = "daedalus"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daedalus-agent",
        description="Daedalus coding agent on tau. No tool is enabled unless you name it.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tools", help="list the available tools")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-t",
        "--tool",
        action="append",
        default=[],
        metavar="NAME",
        help="enable a tool (repeat, or comma-separate). Also read from DAEDALUS_AGENT_TOOLS.",
    )
    common.add_argument(
        "-m",
        "--model",
        default=os.environ.get("DAEDALUS_AGENT_MODEL", DEFAULT_MODEL),
        help=f"provider:model (default: $DAEDALUS_AGENT_MODEL or {DEFAULT_MODEL})",
    )
    common.add_argument("-w", "--workspace", default=".", help="workspace root (default: .)")
    common.add_argument("--max-turns", type=int, default=60, help="turn limit per agent")
    common.add_argument(
        "--max-parallel", type=int, default=8, help="max concurrent model requests across a team"
    )
    common.add_argument("--max-tokens", type=int, default=None, help="max output tokens")
    common.add_argument("--command-timeout", type=float, default=120.0, help="run_* timeout (s)")
    common.add_argument(
        "--max-depth", type=int, default=2, help="how deep `delegate` may nest sub-agents"
    )
    common.add_argument(
        "--allow-outside-workspace",
        action="store_true",
        help="let file tools touch paths outside the workspace",
    )
    common.add_argument("-v", "--verbose", action="store_true", help="show every tool result")

    planning = argparse.ArgumentParser(add_help=False)
    planning.add_argument(
        "--researchers",
        type=int,
        default=len(RESEARCH_LENSES),
        help=f"parallel researchers (0-{len(RESEARCH_LENSES)})",
    )
    planning.add_argument(
        "--planners",
        type=int,
        default=4,
        help=f"competing planners (1-{len(PLANNER_STRATEGIES)})",
    )
    planning.add_argument("--no-critique", action="store_true", help="skip per-plan critics")
    planning.add_argument("--review-rounds", type=int, default=1, help="review/revise rounds")
    planning.add_argument("--reviewers", type=int, default=2, help="reviewers per round")

    run = sub.add_parser("run", parents=[common], help="run one agent")
    run.add_argument("prompt", nargs="*", help="prompt (omit for stdin or an interactive session)")

    hp = sub.add_parser(
        "hyperplan", aliases=["hyper-plan"], parents=[common, planning], help="plan with a team"
    )
    hp.add_argument("prompt", nargs="+", help="the goal")
    hp.add_argument("--save", metavar="PATH", help="write the plan as JSON")

    uw = sub.add_parser(
        "ultrawork", parents=[common, planning], help="plan and execute with teams of agents"
    )
    uw.add_argument("prompt", nargs="*", help="the goal (optional with --plan)")
    uw.add_argument("--plan", metavar="PATH", help="execute this plan JSON instead of planning")
    uw.add_argument("--save-plan", metavar="PATH", help="write the plan as JSON before executing")
    uw.add_argument("--max-retries", type=int, default=2, help="re-runs per rejected task")
    uw.add_argument("--no-verify", action="store_true", help="skip per-task verifiers")
    uw.add_argument("--no-integrate", action="store_true", help="skip the final integrator")
    return parser


def _list_tools() -> int:
    width = max(len(n) for n in TOOLS)
    print("Available tools. None is enabled by default; enable each with -t NAME.\n")
    for name, spec in TOOLS.items():
        kind = "read-only" if spec.read_only else spec.category
        print(f"  {name:<{width}}  [{kind}] {spec.description}")
    return 0


def _runtime(args: argparse.Namespace, reporter: ConsoleReporter) -> Runtime:
    tools = parse_tool_names([os.environ.get("DAEDALUS_AGENT_TOOLS", ""), *args.tool])
    provider, model = build_provider(args.model, max_tokens=args.max_tokens)
    ctx = ToolContext(
        workspace=Path(args.workspace).resolve(),
        allow_outside_workspace=args.allow_outside_workspace,
        command_timeout=args.command_timeout,
        max_depth=args.max_depth,
    )
    runtime = Runtime(
        provider,
        model,
        enabled_tools=tools,
        ctx=ctx,
        max_parallel=args.max_parallel,
        max_turns=args.max_turns,
        reporter=reporter,
    )
    reporter.note(
        f"model {args.model}; tools: {', '.join(runtime.enabled_tools) or 'none'}"
        f"; workspace {ctx.workspace}"
    )
    return runtime


def _planning_config(args: argparse.Namespace) -> HyperPlanConfig:
    return HyperPlanConfig(
        researchers=args.researchers,
        planners=args.planners,
        critique=not args.no_critique,
        review_rounds=args.review_rounds,
        reviewers=args.reviewers,
    )


def _save_plan(plan: Plan, path: str) -> None:
    Path(path).write_text(json.dumps(plan.model_dump(), indent=2) + "\n", encoding="utf-8")
    print(f"plan saved to {path}", file=sys.stderr)


async def _cmd_run(args: argparse.Namespace) -> int:
    runtime = _runtime(args, ConsoleReporter(stream=MAIN_AGENT, verbose=args.verbose))
    prompt = " ".join(args.prompt)
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    harness = runtime.make_harness()
    if prompt:
        result = await runtime.drive(MAIN_AGENT, harness, prompt)
        print(f"-- {runtime.usage.summary()}", file=sys.stderr)
        return 0 if result.ok else 1
    print("Interactive session. Ctrl-D to quit.", file=sys.stderr)
    while True:
        try:
            line = input("\n> ").strip()
        except EOFError:
            print(file=sys.stderr)
            break
        if line:
            await runtime.drive(MAIN_AGENT, harness, line)
    print(f"-- {runtime.usage.summary()}", file=sys.stderr)
    return 0


async def _cmd_hyperplan(args: argparse.Namespace) -> int:
    runtime = _runtime(args, ConsoleReporter(verbose=args.verbose))
    result = await HyperPlanner(runtime, _planning_config(args)).run(" ".join(args.prompt))
    if args.save:
        _save_plan(result.plan, args.save)
    print(result.plan.to_markdown())
    print(f"-- {runtime.usage.summary()}", file=sys.stderr)
    return 0


async def _cmd_ultrawork(args: argparse.Namespace) -> int:
    runtime = _runtime(args, ConsoleReporter(verbose=args.verbose))
    plan = None
    if args.plan:
        plan = Plan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    goal = " ".join(args.prompt) or (plan.goal if plan else "")
    if not goal:
        print("ultrawork: give a goal or --plan", file=sys.stderr)
        return 2

    def on_plan(p: Plan) -> None:
        if args.save_plan:
            _save_plan(p, args.save_plan)
        print(p.to_markdown(), file=sys.stderr)

    config = UltraworkConfig(
        planning=_planning_config(args),
        max_retries=args.max_retries,
        verify=not args.no_verify,
        integrate=not args.no_integrate,
    )
    result = await Ultrawork(runtime, config).run(goal, plan=plan, on_plan=on_plan)
    print("\n# Task outcomes\n")
    for task_id, o in result.outcomes.items():
        extra = f" ({o.note})" if o.note else ""
        print(f"- {task_id}: {o.status}, {o.attempts} attempt(s){extra}")
    if result.report:
        print(f"\n# Report\n\n{result.report}")
    print(f"-- {runtime.usage.summary()}", file=sys.stderr)
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "tools":
        return _list_tools()
    handler = {
        "run": _cmd_run,
        "hyperplan": _cmd_hyperplan,
        "hyper-plan": _cmd_hyperplan,
        "ultrawork": _cmd_ultrawork,
    }[args.command]
    try:
        return asyncio.run(handler(args))
    except (UnknownToolError, ProviderError, ValueError) as exc:
        print(f"daedalus-agent: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
