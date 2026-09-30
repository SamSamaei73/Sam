"""Durable Career repository (Phase 17).

The Phase 16 ``InMemoryCareerRepository`` logic, write-through mirrored to the
``documents`` table (see ``sam.storage.documents``).

Minimum retention for sensitive values: an owner's answers to sensitive
application questions (salary, notice period, relocation, work
authorization, sponsorship, disability, demographics, criminal history,
legal attestations, references, privacy consent, availability: every
``DraftAnswer`` marked ``sensitive``) and the optional sensitive preferences
(``salary_preference``, ``needs_sponsorship``) are NEVER written to disk.
They live only in this process; after a restart the owner answers again, and
startup recovery sends any draft that relied on them back to the owner with
its approval cleared (its approved manifest can no longer match anyway).
Everything else (opportunities with provenance, drafts and versions, document
text and hashes, questions, contacts, outreach and follow-ups) survives.
"""

from __future__ import annotations

from typing import Any

from sam.career.models import (
    ApplicationDocument,
    ApplicationDraft,
    CareerOpportunity,
    CareerPreferences,
    Contact,
    FollowUp,
    OutreachDraft,
)
from sam.career.repository import InMemoryCareerRepository
from sam.storage.database import Database
from sam.storage.documents import DocumentMirror, State, WriteThrough, durable


def persistable_draft(draft: ApplicationDraft) -> ApplicationDraft:
    return draft.model_copy(
        update={"answers": tuple(a for a in draft.answers if not a.sensitive)}
    )


def persistable_preferences(prefs: CareerPreferences) -> CareerPreferences:
    return prefs.model_copy(
        update={"salary_preference": None, "needs_sponsorship": None}
    )


class SQLiteCareerRepository(WriteThrough, InMemoryCareerRepository):
    def __init__(self, db: Database) -> None:
        super().__init__()
        mirror = DocumentMirror(db, "career")
        self._load(mirror.load())
        self._start_mirror(mirror)

    # ---------------------------------------------------------- mirror

    def _tables(self) -> dict[str, Any]:
        return {
            "opportunity": self._opportunities,
            "draft": self._drafts,
            "document": self._documents,
            "contact": self._contacts,
            "outreach": self._outreach,
            "follow_up": self._follow_ups,
        }

    def _serialize(self) -> State:
        state: State = {}
        for kind, table in self._tables().items():
            items = table._items
            if kind == "draft":
                state[kind] = {
                    k: persistable_draft(v).model_dump_json() for k, v in items.items()
                }
            else:
                state[kind] = {k: v.model_dump_json() for k, v in items.items()}
        state["preferences"] = {
            owner: persistable_preferences(p).model_dump_json()
            for owner, p in self._preferences.items()
        }
        return state

    def _snapshot(self) -> Any:
        return (
            {k: dict(t._items) for k, t in self._tables().items()},
            dict(self._preferences),
        )

    def _restore(self, snapshot: Any) -> None:
        tables, preferences = snapshot
        for kind, table in self._tables().items():
            table._items = dict(tables[kind])
        self._preferences = dict(preferences)

    def _load(self, state: State) -> None:
        models: dict[str, Any] = {
            "opportunity": CareerOpportunity,
            "draft": ApplicationDraft,
            "document": ApplicationDocument,
            "contact": Contact,
            "outreach": OutreachDraft,
            "follow_up": FollowUp,
        }
        for kind, table in self._tables().items():
            table._items = {
                key: models[kind].model_validate_json(body)
                for key, body in state.get(kind, {}).items()
            }
        self._preferences = {
            owner: CareerPreferences.model_validate_json(body)
            for owner, body in state.get("preferences", {}).items()
        }

    # -------------------------------------------------------- mutators

    put_opportunity = durable(InMemoryCareerRepository.put_opportunity)
    put_draft = durable(InMemoryCareerRepository.put_draft)
    put_document = durable(InMemoryCareerRepository.put_document)
    put_contact = durable(InMemoryCareerRepository.put_contact)
    put_outreach = durable(InMemoryCareerRepository.put_outreach)
    put_follow_up = durable(InMemoryCareerRepository.put_follow_up)
    set_preferences = durable(InMemoryCareerRepository.set_preferences)


MUTATORS = (
    "put_opportunity",
    "put_draft",
    "put_document",
    "put_contact",
    "put_outreach",
    "put_follow_up",
    "set_preferences",
)

__all__ = [
    "MUTATORS",
    "SQLiteCareerRepository",
    "persistable_draft",
    "persistable_preferences",
]
