"""Deterministic, static risk/confirmation policy.

This is the single source of truth for "how risky is this action on this
resource" and "does it always require confirmation" — never the LLM, never
a grant, never a runtime configuration file. A grant can only make
confirmation *more* strict (see ``PermissionGrant.always_require_confirmation``
and ``PermissionEngine``); nothing here or in a grant can waive the
confirmation this table requires for HIGH/CRITICAL risk.

An (resource, action) pair absent from this table is an *unclassified*
combination: ``classify`` returns ``None`` and the engine denies the
request outright, regardless of any grant that might otherwise match.
Extending the system to a new resource or action means adding a row here
deliberately — it is never inferred at evaluation time.
"""

from __future__ import annotations

from dataclasses import dataclass

from sam.permissions.models import PermissionAction, PermissionResource, RiskLevel

_A = PermissionAction
_R = PermissionResource


@dataclass(frozen=True)
class PolicyEntry:
    """The deterministic risk classification for one (resource, action)."""

    risk: RiskLevel
    requires_confirmation: bool


# Deliberately explicit and reviewable. Every entry is a security decision,
# not an inferred default. See docs/permissions.md for the rationale behind
# entries that might look surprising in isolation (for example, why
# `terminal:execute` is HIGH even though no terminal tool exists yet).
_POLICY: dict[tuple[PermissionResource, PermissionAction], PolicyEntry] = {
    # --- filesystem -------------------------------------------------
    (_R.FILESYSTEM, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.FILESYSTEM, _A.WRITE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.FILESYSTEM, _A.CREATE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.FILESYSTEM, _A.UPDATE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.FILESYSTEM, _A.DELETE): PolicyEntry(RiskLevel.HIGH, True),
    (_R.FILESYSTEM, _A.EXECUTE): PolicyEntry(RiskLevel.HIGH, True),
    # --- terminal -----------------------------------------------------
    # No terminal tool exists yet (Phase 5). Arbitrary command execution
    # is treated as HIGH by default; a future phase may introduce a
    # narrower, lower-risk sub-action for an explicitly allow-listed
    # command set, but the generic action stays HIGH until that design
    # exists.
    (_R.TERMINAL, _A.EXECUTE): PolicyEntry(RiskLevel.HIGH, True),
    # --- git ------------------------------------------------------------
    (_R.GIT, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.GIT, _A.CREATE): PolicyEntry(RiskLevel.MEDIUM, False),  # local commit/branch
    (_R.GIT, _A.WRITE): PolicyEntry(RiskLevel.HIGH, True),  # push / remote state change
    (_R.GIT, _A.DELETE): PolicyEntry(RiskLevel.HIGH, True),  # delete branch/tag
    # --- gmail ------------------------------------------------------
    (_R.GMAIL, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.GMAIL, _A.CREATE): PolicyEntry(RiskLevel.MEDIUM, False),  # draft
    (_R.GMAIL, _A.SEND): PolicyEntry(RiskLevel.HIGH, True),
    (_R.GMAIL, _A.DELETE): PolicyEntry(RiskLevel.HIGH, True),
    # --- calendar ---------------------------------------------------
    (_R.CALENDAR, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.CALENDAR, _A.CREATE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.CALENDAR, _A.UPDATE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.CALENDAR, _A.DELETE): PolicyEntry(RiskLevel.HIGH, True),
    # --- browser ------------------------------------------------------
    # Browser content is external and untrusted, so plain reading is
    # MEDIUM rather than LOW; interacting with a live page can trigger
    # side effects (form submission, purchases) so it is HIGH.
    (_R.BROWSER, _A.READ): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.BROWSER, _A.EXECUTE): PolicyEntry(RiskLevel.HIGH, True),
    # --- social_media -------------------------------------------------
    (_R.SOCIAL_MEDIA, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.SOCIAL_MEDIA, _A.CREATE): PolicyEntry(RiskLevel.MEDIUM, False),  # draft only
    (_R.SOCIAL_MEDIA, _A.PUBLISH): PolicyEntry(RiskLevel.HIGH, True),
    (_R.SOCIAL_MEDIA, _A.DELETE): PolicyEntry(RiskLevel.HIGH, True),
    # --- mcp ------------------------------------------------------------
    # MCP servers are third-party and unreviewed by default; see
    # PROJECT_SPEC.json's mcp_integrations rule. Reading their output is
    # MEDIUM (untrusted content), invoking an MCP tool is HIGH.
    (_R.MCP, _A.READ): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.MCP, _A.EXECUTE): PolicyEntry(RiskLevel.HIGH, True),
    # --- database -----------------------------------------------------
    (_R.DATABASE, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.DATABASE, _A.WRITE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.DATABASE, _A.UPDATE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.DATABASE, _A.DELETE): PolicyEntry(RiskLevel.CRITICAL, True),
    # --- financial_service ----------------------------------------------
    (_R.FINANCIAL_SERVICE, _A.READ): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.FINANCIAL_SERVICE, _A.CREATE): PolicyEntry(RiskLevel.CRITICAL, True),
    (_R.FINANCIAL_SERVICE, _A.UPDATE): PolicyEntry(RiskLevel.CRITICAL, True),
    (_R.FINANCIAL_SERVICE, _A.APPROVE): PolicyEntry(RiskLevel.CRITICAL, True),
    (_R.FINANCIAL_SERVICE, _A.DELETE): PolicyEntry(RiskLevel.CRITICAL, True),
    # --- computer (Phase 5) ---------------------------------------------
    # sam.computer maps its closed capability set onto these three
    # actions (never a new PermissionAction): read-only queries
    # (screenshot, screen size, active window) are READ/LOW; mouse
    # movement/click/scroll are WRITE/MEDIUM (state-changing but
    # reversible); keyboard input (single key, combinations, typed text)
    # is EXECUTE/HIGH because it can trigger arbitrary, potentially
    # externally-consequential application behavior — see
    # sam.computer.capabilities for the exact mapping and
    # docs/computer-control.md for the full rationale.
    (_R.COMPUTER, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.COMPUTER, _A.WRITE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.COMPUTER, _A.EXECUTE): PolicyEntry(RiskLevel.HIGH, True),
    # --- code (Phase 6) --------------------------------------------------
    # sam.coding's repository/file operations (inspect, read, list) are
    # READ/LOW; file creation and modification are WRITE/MEDIUM
    # (reversible via the optimistic-concurrency hash check — see
    # sam.coding.repository); running tests/lint/typecheck and the
    # diff-check verification step are EXECUTE/HIGH because they invoke
    # an external process, matching the same EXECUTE=HIGH=confirm
    # convention as filesystem/terminal/computer-keyboard. Raw git
    # status/diff inspection reuses the pre-existing GIT resource's
    # READ/LOW row below rather than duplicating it here — see
    # sam.coding.policy and docs/coding-agent.md for the exact mapping.
    # git commit/push/reset/clean are never implemented as operations at
    # all, so no policy row for them is meaningful and none is added.
    (_R.CODE, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.CODE, _A.WRITE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.CODE, _A.EXECUTE): PolicyEntry(RiskLevel.HIGH, True),
    # --- knowledge (Phase 7) ---------------------------------------------
    # sam.knowledge's ingestion is WRITE/MEDIUM (reversible — a resource
    # can be removed); reading/listing/retrieving is READ/LOW; deletion
    # is DELETE/HIGH and always requires confirmation, the same
    # convention CRITICAL/HIGH destructive actions use elsewhere in this
    # table. EXECUTE/HIGH is reserved for a future bulk/administrative
    # Knowledge operation (for example a full reindex) — no
    # KnowledgeOperation maps onto it in Phase 7; see
    # sam.knowledge.policy and docs/knowledge.md.
    (_R.KNOWLEDGE, _A.READ): PolicyEntry(RiskLevel.LOW, False),
    (_R.KNOWLEDGE, _A.WRITE): PolicyEntry(RiskLevel.MEDIUM, False),
    (_R.KNOWLEDGE, _A.DELETE): PolicyEntry(RiskLevel.HIGH, True),
    (_R.KNOWLEDGE, _A.EXECUTE): PolicyEntry(RiskLevel.HIGH, True),
}

# Defense in depth: even if a table entry above were ever misconfigured to
# say `requires_confirmation=False` for a CRITICAL row, the engine enforces
# this invariant independently (see PermissionEngine._evaluate_unsafe). We
# also assert it here at import time so a bad edit fails immediately.
for _entry in _POLICY.values():
    if _entry.risk is RiskLevel.CRITICAL and not _entry.requires_confirmation:
        raise AssertionError("CRITICAL policy entries must require confirmation")


def classify(
    resource: PermissionResource, action: PermissionAction
) -> PolicyEntry | None:
    """Return the deterministic policy for this pair, or ``None`` if unknown.

    ``None`` means the engine must deny — see
    ``DenialReason.UNCLASSIFIED_ACTION``.
    """

    return _POLICY.get((resource, action))
