"""Narrow, read-only ports from Career to the owner's evidence.

Career never owns facts about the owner. Every experience, skill, date, degree,
title, publication and metric comes from Phase 14 Professional Intelligence
through ``EvidencePort``; papers come from Knowledge through ``PaperPort``. The
adapters are built by Sam's composition root; this package imports neither
domain, and there is no write method on either port, so Career can never alter
a Professional fact or a Knowledge document.

Every call is made AS the principal, so the other domain's own permission
check applies (a denied read returns ``None``, which Career treats as UNKNOWN).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from sam.career.models import EvidenceLink, FitStatus
from sam.permissions.models import Principal


@dataclass(frozen=True)
class EvidenceClaim:
    """One ACCEPTED Phase 14 claim, as Career may use it."""

    claim_id: str
    category: str
    statement: str
    evidence_ids: tuple[str, ...]
    attributes: Mapping[str, str] = field(default_factory=dict)
    strength: str = "none"

    @property
    def link(self) -> EvidenceLink:
        return EvidenceLink(claim_id=self.claim_id, evidence_ids=self.evidence_ids)


@dataclass(frozen=True)
class RequirementResult:
    status: FitStatus
    claims: tuple[EvidenceClaim, ...] = ()
    unsupported: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatementCheck:
    """``supported`` None means the statement had nothing checkable."""

    supported: bool | None
    reasons: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaperRecord:
    """A paper Sam actually ingested (Knowledge), with its provenance."""

    resource_id: str
    title: str


class EvidencePort(Protocol):
    def claims(self, principal: Principal) -> tuple[EvidenceClaim, ...] | None: ...

    def requirement(
        self, principal: Principal, text: str
    ) -> RequirementResult | None: ...

    def check_statement(
        self, principal: Principal, text: str
    ) -> StatementCheck | None: ...


class PaperPort(Protocol):
    def retrieved(self, principal: Principal, title: str) -> PaperRecord | None: ...


class NoPapers:
    """The default: Sam has read nothing it can cite."""

    def retrieved(self, principal: Principal, title: str) -> PaperRecord | None:
        return None


__all__ = [
    "EvidenceClaim",
    "EvidencePort",
    "NoPapers",
    "PaperPort",
    "PaperRecord",
    "RequirementResult",
    "StatementCheck",
]
