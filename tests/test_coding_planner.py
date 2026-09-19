"""Tests for CodingPlanner and the provider abstraction."""

from datetime import UTC, datetime

import pytest

from sam.coding.errors import InvalidCodingRequestError
from sam.coding.models import (
    CodingOperation,
    CodingProposal,
    CodingTask,
    PlanStep,
    ProviderName,
)
from sam.coding.planner import CodingPlanner, FakeCodingProvider
from sam.permissions.models import Principal, PrincipalKind

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _principal() -> Principal:
    return Principal(kind=PrincipalKind.USER, id="ali")


def _task() -> CodingTask:
    return CodingTask(
        task_id="t1",
        principal=_principal(),
        repository_id="sam-core",
        instruction="fix the bug",
        created_at=_NOW,
    )


def test_build_plan_from_a_small_step_list() -> None:
    planner = CodingPlanner()
    steps = [
        PlanStep(operation=CodingOperation.READ_FILE, path="src/x.py"),
        PlanStep(operation=CodingOperation.MODIFY_FILE, path="src/x.py"),
    ]
    plan = planner.build_plan(_task(), steps)
    assert len(plan.steps) == 2
    assert plan.affected_paths == ("src/x.py",)


def test_build_plan_deduplicates_affected_paths() -> None:
    planner = CodingPlanner()
    steps = [
        PlanStep(operation=CodingOperation.READ_FILE, path="src/x.py"),
        PlanStep(operation=CodingOperation.READ_FILE, path="src/x.py"),
        PlanStep(operation=CodingOperation.READ_FILE, path="src/y.py"),
    ]
    plan = planner.build_plan(_task(), steps)
    assert plan.affected_paths == ("src/x.py", "src/y.py")


def test_build_plan_rejects_too_many_steps() -> None:
    planner = CodingPlanner(max_steps=3)
    steps = [
        PlanStep(operation=CodingOperation.READ_FILE, path=f"f{i}.py") for i in range(4)
    ]
    with pytest.raises(InvalidCodingRequestError):
        planner.build_plan(_task(), steps)


def test_build_plan_rejects_too_many_affected_paths() -> None:
    planner = CodingPlanner(max_affected_paths=2)
    steps = [
        PlanStep(operation=CodingOperation.READ_FILE, path=f"f{i}.py") for i in range(3)
    ]
    with pytest.raises(InvalidCodingRequestError):
        planner.build_plan(_task(), steps)


def test_build_plan_accepts_an_empty_step_list() -> None:
    planner = CodingPlanner()
    plan = planner.build_plan(_task(), [])
    assert plan.steps == ()
    assert plan.affected_paths == ()


def test_planner_bounds_are_clamped_to_the_absolute_maximum() -> None:
    """A caller cannot construct a planner that exceeds the hard-coded
    absolute maximum by passing a huge max_steps."""

    planner = CodingPlanner(max_steps=1_000_000)
    steps = [
        PlanStep(operation=CodingOperation.READ_FILE, path=f"f{i}.py")
        for i in range(25)
    ]
    with pytest.raises(InvalidCodingRequestError):
        planner.build_plan(_task(), steps)


def test_planner_rejects_non_positive_bounds() -> None:
    with pytest.raises(ValueError, match="max_steps"):
        CodingPlanner(max_steps=0)
    with pytest.raises(ValueError, match="max_affected_paths"):
        CodingPlanner(max_affected_paths=0)


def test_planner_has_no_step_generation_method() -> None:
    """Structural proof there is no recursive/autonomous plan-generation
    surface: the planner's public API is exactly build_plan and
    build_plan_from_proposal, nothing that could loop."""

    public_methods = {
        name for name in dir(CodingPlanner) if not name.startswith("_")
    }
    assert public_methods == {"build_plan", "build_plan_from_proposal"}


# --------------------------------------------------------------------- #
# Provider abstraction
# --------------------------------------------------------------------- #


def test_fake_provider_returns_configured_proposal() -> None:
    proposal = CodingProposal(
        provider=ProviderName.SAM_INTERNAL,
        summary="do the thing",
        steps=(PlanStep(operation=CodingOperation.READ_FILE, path="x.py"),),
    )
    provider = FakeCodingProvider(proposal)
    result = provider.propose(_task())
    assert result is proposal


def test_fake_provider_default_returns_empty_proposal() -> None:
    provider = FakeCodingProvider()
    result = provider.propose(_task())
    assert result.steps == ()


def test_build_plan_from_proposal_applies_the_same_bounds() -> None:
    planner = CodingPlanner(max_steps=1)
    proposal = CodingProposal(
        provider=ProviderName.CLAUDE_CODE,
        summary="x",
        steps=(
            PlanStep(operation=CodingOperation.READ_FILE, path="a.py"),
            PlanStep(operation=CodingOperation.READ_FILE, path="b.py"),
        ),
    )
    with pytest.raises(InvalidCodingRequestError):
        planner.build_plan_from_proposal(_task(), proposal)


def test_no_real_external_provider_adapter_exists() -> None:
    """Claude Code / Codex adapters are explicitly not implemented in
    Phase 6 — proven by their absence from the module."""

    import sam.coding.planner as planner_module

    assert not hasattr(planner_module, "ClaudeCodeAdapter")
    assert not hasattr(planner_module, "CodexAdapter")
