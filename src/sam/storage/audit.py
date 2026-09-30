"""Durable, bounded, metadata-only security audit.

What is persisted (``audit_events``): time, domain, operation, status and at
most a reason code, an item id, an attempt id and a manifest hash. Never a
prompt, document text, answer, e-mail body, address or credential: the
columns do not exist. Sources:

* Career: every audited Career operation (the in-memory sinks already hold
  metadata only; the same fields are appended here);
* permissions: every HIGH / CRITICAL decision (resource, action, outcome,
  reason);
* system: startup recovery, backup, restore, migration events.

Retention: the newest ``MAX_AUDIT_ROWS`` rows are kept. This is structured,
durable metadata, NOT a tamper-proof log: the owner of the machine can edit
local files. Nothing here claims otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sam.career.audit import CareerAuditEvent, InMemoryCareerAuditSink
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.models import AuditEvent, RiskLevel
from sam.storage.database import Database

MAX_AUDIT_ROWS = 50_000
PRUNE_EVERY = 500


def _clip(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


class DurableAuditLog:
    def __init__(self, db: Database, max_rows: int = MAX_AUDIT_ROWS) -> None:
        self._db = db
        self._max = max_rows
        self._since_prune = 0

    def append(
        self,
        domain: str,
        operation: str,
        status: str,
        *,
        reason_code: str | None = None,
        item_id: str | None = None,
        attempt_id: str | None = None,
        manifest_hash: str | None = None,
        at: datetime | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO audit_events (occurred_at, domain, operation, status,"
            " reason_code, item_id, attempt_id, manifest_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (at or datetime.now(UTC)).isoformat(),
                _clip(domain, 32),
                _clip(operation, 40),
                _clip(status, 40),
                _clip(reason_code, 64),
                _clip(item_id, 64),
                _clip(attempt_id, 64),
                _clip(manifest_hash, 64),
            ),
        )
        self._since_prune += 1
        if self._since_prune >= PRUNE_EVERY:
            self.prune()

    def prune(self) -> None:
        self._since_prune = 0
        self._db.execute(
            "DELETE FROM audit_events WHERE seq <="
            " (SELECT coalesce(max(seq), 0) FROM audit_events) - ?",
            (self._max,),
        )

    def count(self) -> int:
        return int(self._db.query("SELECT count(*) FROM audit_events")[0][0])

    def recent(self, limit: int = 100) -> list[tuple[object, ...]]:
        return self._db.query(
            "SELECT occurred_at, domain, operation, status, reason_code, item_id,"
            " attempt_id, manifest_hash FROM audit_events ORDER BY seq DESC LIMIT ?",
            (max(1, min(limit, 1_000)),),
        )


class DurableCareerAuditSink(InMemoryCareerAuditSink):
    """The Phase 16 sink, also appending the same metadata durably. Inside a
    Career reservation this write joins the reservation's transaction, so an
    attempt is never recorded without its audit row (or vice versa)."""

    def __init__(self, log: DurableAuditLog) -> None:
        super().__init__()
        self._log = log

    def record(self, event: CareerAuditEvent) -> None:
        self._log.append(
            "career",
            event.operation.value,
            event.status,
            reason_code=event.reason_code,
            item_id=event.item_id,
            attempt_id=event.attempt_id,
            manifest_hash=event.manifest_hash,
            at=event.occurred_at,
        )
        super().record(event)


class DurablePermissionAuditSink(InMemoryAuditSink):
    """Keeps every decision in memory (Phase 3) and HIGH / CRITICAL ones
    durably. The engine treats a failing sink as best-effort: an audit write
    never changes an authorization decision."""

    def __init__(self, log: DurableAuditLog) -> None:
        super().__init__()
        self._log = log

    def record(self, event: AuditEvent) -> None:
        super().record(event)
        if event.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            self._log.append(
                "permission",
                f"{event.resource.value}.{event.action.value}",
                event.outcome.value,
                reason_code=event.reason.value if event.reason else None,
                at=event.occurred_at,
            )


__all__ = [
    "MAX_AUDIT_ROWS",
    "DurableAuditLog",
    "DurableCareerAuditSink",
    "DurablePermissionAuditSink",
]
