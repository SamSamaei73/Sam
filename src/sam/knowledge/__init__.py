"""Sam's Knowledge Layer: ingestion, chunking, indexing, and
source-attributed retrieval for user-provided reference material (PDFs,
books, papers, documentation, Markdown, TXT, JSON, CSV).

The Knowledge Layer is not Memory (``sam.memory``) — see
``sam.knowledge.engine``'s module docstring and ``docs/knowledge.md``.
Every operation is authorized exclusively by the Phase 3 Permission
Engine (``sam.permissions``); nothing here bypasses it.
"""
