"""Sam's Coding Agent foundation (Phase 6).

A controlled engineering workflow — repository inspection, bounded file
writes with optimistic concurrency, and a closed set of allowlisted
verification commands (tests/lint/typecheck/git diff --check) — never an
unrestricted shell agent. Every operation passes through the existing
Phase 3 Permission Engine (extended with one new ``CODE`` resource,
reusing the pre-existing ``GIT`` resource for git status/diff) before
``sam.coding.executor.CodingExecutor`` ever calls
``sam.coding.repository.RepositoryBackend``.

No shell/terminal execution, no arbitrary command string, no ``eval``/
``exec``, no git commit/push/reset/clean, and no real external coding
provider (Claude Code/Codex) integration exists anywhere in this
package — see ``docs/coding-agent.md`` for the full scope boundary and
rationale.
"""
