"""Persistence contract for the Career & PhD Agent.

``CareerRepository`` is an abstraction; Phase 16 ships ONLY
``InMemoryCareerRepository``: no SQLite, no file persistence, no cloud
persistence. Everything is lost on restart. Every collection is bounded.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Protocol, TypeVar

from sam.career.models import (
    ApplicationDocument,
    ApplicationDraft,
    CareerOpportunity,
    CareerPreferences,
    Contact,
    FollowUp,
    OutreachDraft,
)

T = TypeVar("T")
MAX_ITEMS = 1_000


class RepositoryFull(ValueError):
    def __init__(self) -> None:
        super().__init__("repository_full")
        self.code = "repository_full"


class CareerRepository(Protocol):
    def put_opportunity(self, item: CareerOpportunity) -> None: ...
    def get_opportunity(self, item_id: str) -> CareerOpportunity | None: ...
    def list_opportunities(self) -> tuple[CareerOpportunity, ...]: ...
    def put_draft(self, item: ApplicationDraft) -> None: ...
    def get_draft(self, item_id: str) -> ApplicationDraft | None: ...
    def list_drafts(self) -> tuple[ApplicationDraft, ...]: ...
    def put_document(self, item: ApplicationDocument) -> None: ...
    def get_document(self, item_id: str) -> ApplicationDocument | None: ...
    def list_documents(self) -> tuple[ApplicationDocument, ...]: ...
    def put_contact(self, item: Contact) -> None: ...
    def get_contact(self, item_id: str) -> Contact | None: ...
    def list_contacts(self) -> tuple[Contact, ...]: ...
    def put_outreach(self, item: OutreachDraft) -> None: ...
    def get_outreach(self, item_id: str) -> OutreachDraft | None: ...
    def list_outreach(self) -> tuple[OutreachDraft, ...]: ...
    def put_follow_up(self, item: FollowUp) -> None: ...
    def list_follow_ups(self) -> tuple[FollowUp, ...]: ...
    def get_preferences(self, owner_id: str) -> CareerPreferences: ...
    def set_preferences(self, owner_id: str, prefs: CareerPreferences) -> None: ...


class _Table[V]:
    def __init__(self, key: Callable[[V], str]) -> None:
        self._key = key
        self._items: dict[str, V] = {}

    def put(self, item: V) -> None:
        key = self._key(item)
        if key not in self._items and len(self._items) >= MAX_ITEMS:
            raise RepositoryFull()
        self._items[key] = item

    def get(self, key: str) -> V | None:
        return self._items.get(key)

    def all(self) -> tuple[V, ...]:
        return tuple(self._items.values())


class InMemoryCareerRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._opportunities = _Table[CareerOpportunity](lambda o: o.opportunity_id)
        self._drafts = _Table[ApplicationDraft](lambda d: d.draft_id)
        self._documents = _Table[ApplicationDocument](lambda d: d.document_id)
        self._contacts = _Table[Contact](lambda c: c.contact_id)
        self._outreach = _Table[OutreachDraft](lambda o: o.outreach_id)
        self._follow_ups = _Table[FollowUp](lambda f: f.follow_up_id)
        self._preferences: dict[str, CareerPreferences] = {}

    def put_opportunity(self, item: CareerOpportunity) -> None:
        with self._lock:
            self._opportunities.put(item)

    def get_opportunity(self, item_id: str) -> CareerOpportunity | None:
        with self._lock:
            return self._opportunities.get(item_id)

    def list_opportunities(self) -> tuple[CareerOpportunity, ...]:
        with self._lock:
            return self._opportunities.all()

    def put_draft(self, item: ApplicationDraft) -> None:
        with self._lock:
            self._drafts.put(item)

    def get_draft(self, item_id: str) -> ApplicationDraft | None:
        with self._lock:
            return self._drafts.get(item_id)

    def list_drafts(self) -> tuple[ApplicationDraft, ...]:
        with self._lock:
            return self._drafts.all()

    def put_document(self, item: ApplicationDocument) -> None:
        with self._lock:
            self._documents.put(item)

    def get_document(self, item_id: str) -> ApplicationDocument | None:
        with self._lock:
            return self._documents.get(item_id)

    def list_documents(self) -> tuple[ApplicationDocument, ...]:
        with self._lock:
            return self._documents.all()

    def put_contact(self, item: Contact) -> None:
        with self._lock:
            self._contacts.put(item)

    def get_contact(self, item_id: str) -> Contact | None:
        with self._lock:
            return self._contacts.get(item_id)

    def list_contacts(self) -> tuple[Contact, ...]:
        with self._lock:
            return self._contacts.all()

    def put_outreach(self, item: OutreachDraft) -> None:
        with self._lock:
            self._outreach.put(item)

    def get_outreach(self, item_id: str) -> OutreachDraft | None:
        with self._lock:
            return self._outreach.get(item_id)

    def list_outreach(self) -> tuple[OutreachDraft, ...]:
        with self._lock:
            return self._outreach.all()

    def put_follow_up(self, item: FollowUp) -> None:
        with self._lock:
            self._follow_ups.put(item)

    def list_follow_ups(self) -> tuple[FollowUp, ...]:
        with self._lock:
            return self._follow_ups.all()

    def get_preferences(self, owner_id: str) -> CareerPreferences:
        with self._lock:
            return self._preferences.get(owner_id, CareerPreferences())

    def set_preferences(self, owner_id: str, prefs: CareerPreferences) -> None:
        with self._lock:
            self._preferences[owner_id] = prefs


__all__ = ["CareerRepository", "InMemoryCareerRepository", "RepositoryFull"]
