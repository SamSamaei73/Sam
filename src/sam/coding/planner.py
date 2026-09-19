"""The bounded planner and the provider abstraction.

**The planner does not execute anything.** ``CodingPlanner.build_plan``
is a pure validation/bounding function: it takes an already-finite,
caller-supplied sequence of ``PlanStep`` objects and either returns a
bounded ``CodingPlan`` or raises — it never generates steps itself, never
loops, and never calls itself recursively. There is no
``while planner.has_more_work(): planner.generate_more()`` method
anywhere in this class, structurally, not merely by convention.

**Claude Code, Codex, and Sam's internal planning are architecturally
distinct** (see the Phase 6 task and docs/coding-agent.md): a
``CodingProvider`` is a narrow Protocol that returns a bounded
``CodingProposal`` — data, never a direct repository mutation and never
an unrestricted command. Phase 6 ships exactly one implementation,
``FakeCodingProvider`` (a deterministic test double). **No
``ClaudeCodeAdapter`` or ``CodexAdapter`` exists in Phase 6** — building
one that safely shells out to an external CLI tool (bounded, allowlisted,
env-isolated, timeout-bounded, output-bounded, unable to commit/push)
is exactly the kind of scope the task explicitly permits deferring when
it cannot be done safely within the phase: "If a safe real provider
integration cannot be implemented without introducing an unrestricted
command runner, implement the provider Protocol and deterministic fake
provider instead." See docs/coding-agent.md for the full rationale.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sam.coding.errors import InvalidCodingRequestError
from sam.coding.models import (
    MAX_AFFECTED_PATHS,
    MAX_PLAN_STEPS,
    CodingPlan,
    CodingProposal,
    CodingTask,
    PlanStep,
    ProviderName,
    utc_now,
)

DEFAULT_MAX_STEPS = MAX_PLAN_STEPS
DEFAULT_MAX_AFFECTED_PATHS = MAX_AFFECTED_PATHS


class CodingProvider(Protocol):
    """A narrow, bounded source of proposals.

    Returns a ``CodingProposal`` (data) — never mutates a repository,
    never returns an executable command string, never a raw shell
    invocation. See the module docstring for why no real external
    provider adapter exists in Phase 6.
    """

    def propose(self, task: CodingTask) -> CodingProposal: ...


class FakeCodingProvider:
    """A deterministic test double. Each instance owns its own
    configured response — no global state, no real external call."""

    def __init__(self, proposal: CodingProposal | None = None) -> None:
        self._proposal = proposal

    def propose(self, task: CodingTask) -> CodingProposal:
        if self._proposal is not None:
            return self._proposal
        return CodingProposal(
            provider=ProviderName.SAM_INTERNAL,
            summary=f"No steps proposed for task {task.task_id}.",
            steps=(),
        )


class CodingPlanner:
    """Validates and bounds an already-finite list of steps into a
    ``CodingPlan``. Owns no repository, no backend, no permission
    engine — it cannot execute anything even if asked to."""

    def __init__(
        self,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_affected_paths: int = DEFAULT_MAX_AFFECTED_PATHS,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if max_affected_paths <= 0:
            raise ValueError("max_affected_paths must be positive")
        self._max_steps = min(max_steps, MAX_PLAN_STEPS)
        self._max_affected_paths = min(max_affected_paths, MAX_AFFECTED_PATHS)

    def build_plan(self, task: CodingTask, steps: Sequence[PlanStep]) -> CodingPlan:
        """Validate and bound ``steps`` (already a finite, concrete
        sequence — never generated here) into a ``CodingPlan``. Raises
        ``InvalidCodingRequestError`` if the step or affected-path count
        exceeds the configured bound.
        """

        if len(steps) > self._max_steps:
            raise InvalidCodingRequestError(
                f"plan has {len(steps)} steps, exceeding max_steps={self._max_steps}"
            )
        affected = tuple(
            dict.fromkeys(step.path for step in steps if step.path is not None)
        )
        if len(affected) > self._max_affected_paths:
            raise InvalidCodingRequestError(
                f"plan affects {len(affected)} paths, exceeding "
                f"max_affected_paths={self._max_affected_paths}"
            )
        return CodingPlan(
            task_id=task.task_id,
            steps=tuple(steps),
            affected_paths=affected,
            created_at=utc_now(),
        )

    def build_plan_from_proposal(
        self, task: CodingTask, proposal: CodingProposal
    ) -> CodingPlan:
        """The same bounding, applied to a provider's proposal — a
        ``CodingProvider`` never bypasses these limits."""

        return self.build_plan(task, proposal.steps)


__all__ = ["CodingPlanner", "CodingProvider", "FakeCodingProvider"]
