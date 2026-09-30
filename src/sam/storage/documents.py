"""Write-through persistence of a domain repository's records.

Each durable domain repository keeps the proven in-memory implementation (its
validation, atomicity and query logic are unchanged) and mirrors its state to
the ``documents`` table: one row per record, keyed by (domain, kind, id), the
body being the record's own validated JSON.

After every mutating call the repository serializes its state, diffs it
against what was last committed, and writes the difference in ONE
transaction. If that write fails (disk full, I/O error), the in-memory state
is restored to its snapshot and the error propagates: no caller ever sees a
success that is not on disk, and memory never runs ahead of the database.
On start, the state is loaded back and validated through the same models.

``atomic()`` groups several repository calls (and other writes on the same
database, such as the external-action ledger) into one transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from threading import RLock
from typing import Any, cast

from sam.storage.database import Database

State = dict[str, dict[str, str]]  # kind -> key -> JSON body
# Each domain's fixed table (code constants only: never built from input).
# Memory and Knowledge have their own tables, keeping their domains separate.
TABLES = {
    "professional": "documents",
    "career": "documents",
    "proactive": "documents",
    "memory": "memory_documents",
    "knowledge": "knowledge_documents",
}
DOMAINS = frozenset(TABLES)


class DocumentMirror:
    def __init__(self, db: Database, domain: str) -> None:
        if domain not in DOMAINS:
            raise ValueError("unknown document domain")
        self.db = db
        self.domain = domain
        self._table = TABLES[domain]
        self._shared = self._table == "documents"

    def load(self) -> State:
        if self._shared:
            rows = self.db.query(
                "SELECT kind, key, body FROM documents WHERE domain = ?"
                " ORDER BY kind, key",
                (self.domain,),
            )
        else:
            rows = self.db.query(
                f"SELECT kind, key, body FROM {self._table} ORDER BY kind, key"
            )
        state: State = {}
        for kind, key, body in rows:
            state.setdefault(str(kind), {})[str(key)] = str(body)
        return state

    def apply(self, before: State, after: State) -> None:
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            for kind in sorted(set(before) | set(after)):
                old, new = before.get(kind, {}), after.get(kind, {})
                for key in sorted(set(old) - set(new)):
                    if self._shared:
                        conn.execute(
                            "DELETE FROM documents WHERE domain = ? AND kind = ?"
                            " AND key = ?",
                            (self.domain, kind, key),
                        )
                    else:
                        conn.execute(
                            f"DELETE FROM {self._table} WHERE kind = ? AND key = ?",
                            (kind, key),
                        )
                for key, body in sorted(new.items()):
                    if old.get(key) == body:
                        continue
                    if self._shared:
                        conn.execute(
                            "INSERT INTO documents (domain, kind, key, body,"
                            " updated_at) VALUES (?, ?, ?, ?, ?)"
                            " ON CONFLICT (domain, kind, key) DO UPDATE SET"
                            " body = excluded.body, updated_at = excluded.updated_at",
                            (self.domain, kind, key, body, now),
                        )
                    else:
                        conn.execute(
                            f"INSERT INTO {self._table} (kind, key, body, updated_at)"
                            " VALUES (?, ?, ?, ?) ON CONFLICT (kind, key) DO UPDATE"
                            " SET body = excluded.body,"
                            " updated_at = excluded.updated_at",
                            (kind, key, body, now),
                        )


class WriteThrough:
    """Mixin for an in-memory repository subclass. Subclasses provide
    ``_serialize`` / ``_snapshot`` / ``_restore`` and must hold ``_lock``."""

    _lock: RLock
    _mirror: DocumentMirror | None = None
    _committed: State

    def _serialize(self) -> State:
        raise NotImplementedError

    def _snapshot(self) -> Any:
        raise NotImplementedError

    def _restore(self, snapshot: Any) -> None:
        raise NotImplementedError

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._mirror is None:
            yield
        else:
            with self._mirror.db.transaction():
                yield

    def _start_mirror(self, mirror: DocumentMirror) -> None:
        self._mirror = mirror
        self._committed = self._serialize()

    def _persist(self) -> None:
        if self._mirror is None:
            return
        after = self._serialize()
        if after != self._committed:
            self._mirror.apply(self._committed, after)
            self._committed = after

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Several calls (and same-database writes) in one transaction."""

        with self._lock:
            if self._mirror is None:
                yield
                return
            snapshot, committed = self._snapshot(), self._committed
            try:
                with self._mirror.db.transaction():
                    yield
            except BaseException:
                self._restore(snapshot)
                self._committed = committed
                raise


def durable[F: Callable[..., Any]](method: F) -> F:
    """Wrap a mutating repository method: run it, then persist, all under
    the repository lock; on any failure restore the pre-call state."""

    @wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            snapshot = self._snapshot()
            committed = getattr(self, "_committed", {})
            try:
                with self._transaction():
                    result = method(self, *args, **kwargs)
                    self._persist()
            except BaseException:
                self._restore(snapshot)
                self._committed = committed
                raise
            return result

    return cast(F, wrapper)


def load_models[M](
    bodies: Mapping[str, str], parse: Callable[[str], M]
) -> dict[str, M]:
    return {key: parse(body) for key, body in bodies.items()}


__all__ = [
    "DOMAINS",
    "TABLES",
    "DocumentMirror",
    "State",
    "WriteThrough",
    "durable",
    "load_models",
]
