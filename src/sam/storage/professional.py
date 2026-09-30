"""Durable Professional Intelligence repository (Phase 17).

Exactly the Phase 14 ``InMemoryProfessionalRepository`` semantics (atomic
per-source refresh with full rollback, source independence, lapse of
confirmations whose attestation is gone, retained rejections), write-through
mirrored to the ``documents`` table. An ingestion, refresh or source deletion
is ONE database transaction: if it cannot be written, the in-memory state is
rolled back too and nothing changes. Records are stored as their own
validated models, so a restart restores the same sources, claims,
``EvidenceRef`` provenance, reviews and resolutions; evidence strength is
recomputed by the service from that evidence exactly as before.
"""

from __future__ import annotations

from typing import Any

from sam.professional.models import (
    ClaimReview,
    EvidenceRef,
    ProfessionalClaim,
    Resolution,
    SourceRecord,
)
from sam.professional.repository import InMemoryProfessionalRepository
from sam.storage.database import Database
from sam.storage.documents import DocumentMirror, State, WriteThrough, durable

_SEP = "\x1f"


class SQLiteProfessionalRepository(WriteThrough, InMemoryProfessionalRepository):
    def __init__(self, db: Database) -> None:
        super().__init__()
        mirror = DocumentMirror(db, "professional")
        self._load(mirror.load())
        self._start_mirror(mirror)

    def _serialize(self) -> State:
        return {
            "source": {k: v.model_dump_json() for k, v in self._sources.items()},
            "claim": {k: v.model_dump_json() for k, v in self._claims.items()},
            "evidence": {k: v.model_dump_json() for k, v in self._evidence.items()},
            "resolution": {
                _SEP.join(k): v.model_dump_json() for k, v in self._resolutions.items()
            },
            "review": {k: v.model_dump_json() for k, v in self._reviews.items()},
        }

    # ``_snapshot`` / ``_restore`` are the Phase 14 repository's own.
    def _snapshot(self) -> tuple[object, ...]:
        return InMemoryProfessionalRepository._snapshot(self)

    def _restore(self, snapshot: Any) -> None:
        InMemoryProfessionalRepository._restore(self, snapshot)

    def _load(self, state: State) -> None:
        self._sources = {
            k: SourceRecord.model_validate_json(v)
            for k, v in state.get("source", {}).items()
        }
        self._claims = {
            k: ProfessionalClaim.model_validate_json(v)
            for k, v in state.get("claim", {}).items()
        }
        self._evidence = {
            k: EvidenceRef.model_validate_json(v)
            for k, v in state.get("evidence", {}).items()
        }
        resolutions: dict[tuple[str, str], Resolution] = {}
        for key, body in state.get("resolution", {}).items():
            claim_id, attribute = key.split(_SEP, 1)
            resolutions[(claim_id, attribute)] = Resolution.model_validate_json(body)
        self._resolutions = resolutions
        self._reviews = {
            k: ClaimReview.model_validate_json(v)
            for k, v in state.get("review", {}).items()
        }

    update_source = durable(InMemoryProfessionalRepository.update_source)
    update_claim = durable(InMemoryProfessionalRepository.update_claim)
    delete_claim = durable(InMemoryProfessionalRepository.delete_claim)
    add_evidence = durable(InMemoryProfessionalRepository.add_evidence)
    put_resolution = durable(InMemoryProfessionalRepository.put_resolution)
    confirm = durable(InMemoryProfessionalRepository.confirm)
    add_rejection = durable(InMemoryProfessionalRepository.add_rejection)
    commit_ingestion = durable(InMemoryProfessionalRepository.commit_ingestion)
    delete_source = durable(InMemoryProfessionalRepository.delete_source)


MUTATORS = (
    "update_source",
    "update_claim",
    "delete_claim",
    "add_evidence",
    "put_resolution",
    "confirm",
    "add_rejection",
    "commit_ingestion",
    "delete_source",
)

__all__ = ["MUTATORS", "SQLiteProfessionalRepository"]
