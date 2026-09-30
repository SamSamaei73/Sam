"""Storage error type, dependency-free so domain services can catch it
without importing any storage backend."""

from __future__ import annotations


class StorageError(RuntimeError):
    """A storage failure with a safe reason code only (never database
    content, SQL text or a raw driver message)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


__all__ = ["StorageError"]
