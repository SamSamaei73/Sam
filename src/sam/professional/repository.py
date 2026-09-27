"""Persistence contract for Professional Intelligence.

``ProfessionalRepository`` is an abstraction: the domain model never depends on
a storage backend, so persistence can be added later without changing it.
Phase 14 ships ONLY ``InMemoryProfessionalRepository``: no SQLite, no file
persistence and no cloud persistence. Nothing survives a restart.

Ingestion and refresh are ONE atomic transaction per logical source: the batch
is validated completely before anything changes, then ALL of that source's old
``EvidenceRef`` records are replaced by the batch's, and any failure restores the
exact previous state. Nothing of the old version survives a refresh (no ghost
evidence) and no other source's evidence is touched. Deleting a source removes
ONLY that source's evidence: a claim corroborated by another source keeps that
source's evidence.

Owner reviews live beside the claims, never inside the evidence. A rejection is
kept (so the claim is never re-created by a later ingest); a confirmation lapses
when the ``OWNER_ATTESTATION`` it rests on is removed with its source, so no
claim is ever reported owner-confirmed without attestation evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Protocol

from sam.professional.models import (
    ClaimCategory,
    ClaimReview,
    ClaimReviewState,
    EvidenceNature,
    EvidenceRef,
    ProfessionalClaim,
    Resolution,
    SourceRecord,
)


@dataclass(frozen=True)
class IngestionBatch:
    """Everything one source contributes, validated before anything is stored."""

    source: SourceRecord
    claims: tuple[ProfessionalClaim, ...]
    evidence: tuple[EvidenceRef, ...]


class ProfessionalRepository(Protocol):
    # sources
    def get_source(self, source_id: str) -> SourceRecord | None: ...
    def find_source_by_checksum(self, checksum: str) -> SourceRecord | None: ...
    def update_source(self, source: SourceRecord) -> None: ...
    def list_sources(self) -> tuple[SourceRecord, ...]: ...

    # claims
    def get_claim(self, claim_id: str) -> ProfessionalClaim | None: ...
    def list_claims(
        self, category: ClaimCategory | None = None
    ) -> tuple[ProfessionalClaim, ...]: ...
    def update_claim(self, claim: ProfessionalClaim) -> None: ...
    def delete_claim(self, claim_id: str) -> None: ...

    # evidence
    def list_evidence(self, claim_id: str) -> tuple[EvidenceRef, ...]: ...
    def list_all_evidence(self) -> tuple[EvidenceRef, ...]: ...
    def add_evidence(self, ref: EvidenceRef) -> None: ...

    # owner decisions
    def list_resolutions(self) -> tuple[Resolution, ...]: ...
    def put_resolution(self, resolution: Resolution) -> None: ...
    def get_review(self, claim_id: str) -> ClaimReview | None: ...
    def list_reviews(self) -> tuple[ClaimReview, ...]: ...
    def confirm(self, review: ClaimReview, attestation: EvidenceRef) -> None:
        """Record the owner's confirmation AND its attestation evidence,
        atomically: one is never stored without the other."""
        ...

    def add_rejection(
        self, claim_id: str, reviewed_at: datetime | None = None
    ) -> None: ...
    def is_rejected(self, claim_id: str) -> bool: ...
    def list_rejections(self) -> frozenset[str]: ...

    # atomic operations
    def commit_ingestion(self, batch: IngestionBatch) -> tuple[str, ...]:
        """Replace this source's evidence with the batch, upsert its claims and
        store the source, all-or-nothing. Returns the ids of the SURVIVING claims
        whose evidence changed (so a caller can refresh them)."""
        ...

    def delete_source(self, source_id: str) -> tuple[str, ...]:
        """Remove the source and ONLY its evidence. Claims left with no evidence
        are removed; every other claim keeps its other sources' evidence.
        Returns the ids of the SURVIVING claims whose evidence changed."""
        ...


class InMemoryProfessionalRepository:
    """Thread-safe, process-local, and empty on every start."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sources: dict[str, SourceRecord] = {}
        self._claims: dict[str, ProfessionalClaim] = {}
        self._evidence: dict[str, EvidenceRef] = {}
        self._resolutions: dict[tuple[str, str], Resolution] = {}
        self._reviews: dict[str, ClaimReview] = {}

    # ------------------------------------------------------------- sources

    def get_source(self, source_id: str) -> SourceRecord | None:
        with self._lock:
            return self._sources.get(source_id)

    def find_source_by_checksum(self, checksum: str) -> SourceRecord | None:
        with self._lock:
            return next(
                (
                    s
                    for s in sorted(self._sources.values(), key=lambda s: s.source_id)
                    if s.checksum == checksum
                ),
                None,
            )

    def update_source(self, source: SourceRecord) -> None:
        with self._lock:
            if source.source_id in self._sources:
                self._sources[source.source_id] = source

    def list_sources(self) -> tuple[SourceRecord, ...]:
        with self._lock:
            return tuple(sorted(self._sources.values(), key=lambda s: s.source_id))

    # -------------------------------------------------------------- claims

    def get_claim(self, claim_id: str) -> ProfessionalClaim | None:
        with self._lock:
            return self._claims.get(claim_id)

    def list_claims(
        self, category: ClaimCategory | None = None
    ) -> tuple[ProfessionalClaim, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        c
                        for c in self._claims.values()
                        if category is None or c.category is category
                    ),
                    key=lambda c: c.claim_id,
                )
            )

    def update_claim(self, claim: ProfessionalClaim) -> None:
        with self._lock:
            if claim.claim_id in self._claims:
                self._claims[claim.claim_id] = claim

    def delete_claim(self, claim_id: str) -> None:
        with self._lock:
            self._drop_claim(claim_id)

    def _drop_claim(self, claim_id: str) -> None:
        self._claims.pop(claim_id, None)
        for evidence_id in [
            e.evidence_id for e in self._evidence.values() if e.claim_id == claim_id
        ]:
            del self._evidence[evidence_id]
        for key in [k for k in self._resolutions if k[0] == claim_id]:
            del self._resolutions[key]
        review = self._reviews.get(claim_id)
        if review is not None and review.state is not ClaimReviewState.OWNER_REJECTED:
            del self._reviews[claim_id]

    # ------------------------------------------------------------ evidence

    def list_evidence(self, claim_id: str) -> tuple[EvidenceRef, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (e for e in self._evidence.values() if e.claim_id == claim_id),
                    key=lambda e: e.evidence_id,
                )
            )

    def list_all_evidence(self) -> tuple[EvidenceRef, ...]:
        with self._lock:
            return tuple(sorted(self._evidence.values(), key=lambda e: e.evidence_id))

    def add_evidence(self, ref: EvidenceRef) -> None:
        with self._lock:
            if ref.claim_id not in self._claims:
                raise KeyError("unknown claim")
            self._evidence[ref.evidence_id] = ref

    # ----------------------------------------------------- owner decisions

    def list_resolutions(self) -> tuple[Resolution, ...]:
        with self._lock:
            return tuple(
                sorted(
                    self._resolutions.values(), key=lambda r: (r.claim_id, r.attribute)
                )
            )

    def put_resolution(self, resolution: Resolution) -> None:
        with self._lock:
            self._resolutions[(resolution.claim_id, resolution.attribute)] = resolution

    def get_review(self, claim_id: str) -> ClaimReview | None:
        with self._lock:
            return self._reviews.get(claim_id)

    def list_reviews(self) -> tuple[ClaimReview, ...]:
        with self._lock:
            return tuple(sorted(self._reviews.values(), key=lambda r: r.claim_id))

    def confirm(self, review: ClaimReview, attestation: EvidenceRef) -> None:
        with self._lock:
            if (
                review.state is not ClaimReviewState.OWNER_CONFIRMED
                or attestation.nature is not EvidenceNature.OWNER_ATTESTATION
                or attestation.claim_id != review.claim_id
                or review.claim_id not in self._claims
                or attestation.basis_evidence_id not in self._evidence
                or attestation.source_id not in self._sources
            ):
                raise ValueError("inconsistent confirmation")
            self._evidence[attestation.evidence_id] = attestation
            self._reviews[review.claim_id] = review

    def add_rejection(self, claim_id: str, reviewed_at: datetime | None = None) -> None:
        with self._lock:
            self._drop_claim(claim_id)
            self._reviews[claim_id] = ClaimReview(
                claim_id=claim_id,
                state=ClaimReviewState.OWNER_REJECTED,
                reviewed_at=reviewed_at or _now(),
            )

    def is_rejected(self, claim_id: str) -> bool:
        with self._lock:
            review = self._reviews.get(claim_id)
            return (
                review is not None and review.state is ClaimReviewState.OWNER_REJECTED
            )

    def list_rejections(self) -> frozenset[str]:
        with self._lock:
            return frozenset(
                r.claim_id
                for r in self._reviews.values()
                if r.state is ClaimReviewState.OWNER_REJECTED
            )

    # ------------------------------------------------------ atomic changes

    def _snapshot(self) -> tuple[object, ...]:
        return (
            dict(self._sources),
            dict(self._claims),
            dict(self._evidence),
            dict(self._resolutions),
            dict(self._reviews),
        )

    def _restore(self, snapshot: tuple[object, ...]) -> None:
        sources, claims, evidence, resolutions, reviews = snapshot
        self._sources = sources  # type: ignore[assignment]
        self._claims = claims  # type: ignore[assignment]
        self._evidence = evidence  # type: ignore[assignment]
        self._resolutions = resolutions  # type: ignore[assignment]
        self._reviews = reviews  # type: ignore[assignment]

    def _validate_batch(self, batch: IngestionBatch) -> None:
        """Every check a batch must pass, before ANYTHING is changed."""

        source_id = batch.source.source_id
        claim_ids = {c.claim_id for c in batch.claims} | set(self._claims)
        rejected = self.list_rejections()
        evidence_ids = {e.evidence_id for e in batch.evidence}
        for claim in batch.claims:
            if claim.claim_id in rejected:
                raise ValueError("a rejected claim cannot be re-created")
        for ref in batch.evidence:
            if ref.source_id != source_id or ref.claim_id not in claim_ids:
                raise ValueError("inconsistent ingestion batch")
            if ref.nature is EvidenceNature.OWNER_ATTESTATION and (
                ref.basis_evidence_id not in evidence_ids
            ):
                raise ValueError("an attestation must rest on this batch's evidence")

    def _remove_source_evidence(self, source_id: str) -> set[str]:
        affected: set[str] = set()
        for evidence_id in [
            e.evidence_id for e in self._evidence.values() if e.source_id == source_id
        ]:
            affected.add(self._evidence.pop(evidence_id).claim_id)
        return affected

    def commit_ingestion(self, batch: IngestionBatch) -> tuple[str, ...]:
        with self._lock:
            self._validate_batch(batch)
            snapshot = self._snapshot()
            try:
                source_id = batch.source.source_id
                # 1. remove EVERY old record of this logical source (no ghosts)
                affected = self._remove_source_evidence(source_id)
                # 2. insert the new version
                for claim in batch.claims:
                    existing = self._claims.get(claim.claim_id)
                    self._claims[claim.claim_id] = (
                        claim.model_copy(update={"created_at": existing.created_at})
                        if existing is not None
                        else claim
                    )
                    affected.add(claim.claim_id)
                for ref in batch.evidence:
                    if ref.source_id != source_id or ref.claim_id not in self._claims:
                        raise ValueError("inconsistent ingestion batch")
                    self._evidence[ref.evidence_id] = ref
                    affected.add(ref.claim_id)
                self._sources[source_id] = batch.source
                # 3. drop what no longer has evidence; lapse unbacked reviews
                affected -= self._drop_orphans(affected)
                self._lapse_confirmations(affected)
                return tuple(sorted(affected))
            except BaseException:
                self._restore(snapshot)
                raise

    def delete_source(self, source_id: str) -> tuple[str, ...]:
        with self._lock:
            snapshot = self._snapshot()
            try:
                affected = self._remove_source_evidence(source_id)
                self._sources.pop(source_id, None)
                affected -= self._drop_orphans(affected)
                self._lapse_confirmations(affected)
                return tuple(sorted(affected))
            except BaseException:
                self._restore(snapshot)
                raise

    def _lapse_confirmations(self, claim_ids: Iterable[str]) -> None:
        """A confirmation whose attestation evidence is gone is removed: the
        owner is asked again rather than a claim staying accepted on nothing."""

        for claim_id in claim_ids:
            review = self._reviews.get(claim_id)
            if review is None or review.state is not ClaimReviewState.OWNER_CONFIRMED:
                continue
            if not any(
                e.claim_id == claim_id
                and e.nature is EvidenceNature.OWNER_ATTESTATION
                and e.basis_evidence_id in self._evidence
                for e in self._evidence.values()
            ):
                del self._reviews[claim_id]

    def _drop_orphans(self, candidates: Iterable[str]) -> set[str]:
        """Remove claims that no longer have ANY evidence. Returns the ids that
        were removed. A claim that still has another source's evidence stays."""

        removed: set[str] = set()
        for claim_id in candidates:
            if claim_id in self._claims and not any(
                e.claim_id == claim_id for e in self._evidence.values()
            ):
                self._drop_claim(claim_id)
                removed.add(claim_id)
        return removed


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "InMemoryProfessionalRepository",
    "IngestionBatch",
    "ProfessionalRepository",
]
