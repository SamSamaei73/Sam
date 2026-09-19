"""Sam's Memory Engine (Phase 4).

Cloud-ready but not cloud-dependent: ``MemoryEngine`` depends only on the
``MemoryStore`` / ``WorkingMemoryStore`` protocols, never on a concrete
storage technology. Phase 4 ships only in-memory implementations of both
— see ``docs/memory.md`` for the intended future PostgreSQL (+ pgvector)
migration, which can implement the same protocols without any change to
``MemoryEngine`` itself.

No tool, MCP server, database driver, or embedding model is implemented
in this package.
"""
