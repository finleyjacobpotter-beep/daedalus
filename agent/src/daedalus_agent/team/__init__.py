"""Agent teams: hyper planning and ultrawork."""

from daedalus_agent.team.plan import Plan, PlanError, PlanTask, parse_plan
from daedalus_agent.team.planning import HyperPlanConfig, HyperPlanner, HyperPlanResult, hyper_plan
from daedalus_agent.team.ultrawork import (
    TaskOutcome,
    Ultrawork,
    UltraworkConfig,
    UltraworkResult,
    ultrawork,
)

__all__ = [
    "HyperPlanConfig",
    "HyperPlanResult",
    "HyperPlanner",
    "Plan",
    "PlanError",
    "PlanTask",
    "TaskOutcome",
    "Ultrawork",
    "UltraworkConfig",
    "UltraworkResult",
    "hyper_plan",
    "parse_plan",
    "ultrawork",
]
