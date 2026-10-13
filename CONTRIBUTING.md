# Contributing to Sam

Sam is under active development. Phase 17 is pending independent re-review
and is not approved. Begin with [README.md](README.md),
[architecture](docs/architecture.md) and [PROJECT_SPEC.json](PROJECT_SPEC.json).

## Workflow

1. Check existing issues/PRs and describe a real problem. Small documentation
   corrections can go directly into a PR.
2. Create a focused branch from an appropriate committed base. Use an isolated
   checkout/worktree when recovery, voice or hardening work is in progress.
3. Implement one coherent change and run relevant [checks](docs/development.md#checks).
4. Open a PR explaining the problem, resulting behavior, evidence, limitations
   and verification. Link an issue when one exists.
5. Merge after applicable checks and existing review requirements are met.
   Do not bypass review or force-push shared history.

## Phase and security review

The specification requires implementation, tests, lint/typecheck, diff review,
documentation and a structured report for external senior review after each
phase. Do not silently advance phases. A docs-only merge does not approve
unfinished phase work or authorize production use.

Changes to permissions, identity, confirmations, credentials, provider data
handling, persistence or consequential external actions need relevant security
evidence and independent phase review. Keep real integrations gated until
reviewed. See [security notes](SECURITY.md).

## Evidence, privacy and attribution

- Use deterministic fakes/synthetic fixtures where practical. Never commit
  secrets, local configuration, owner audio/embeddings, private documents,
  databases, application records or unredacted logs.
- Report commands actually run. Separate baseline failures and unavailable
  live checks from passing results.
- For docs-only PRs, check links, rendering, commands and factual status claims.
  Do not add unsupported test/coverage badges.
- Use coauthor attribution only for people who actually contributed to that
  work. Do not credit an unrelated past contributor or invented identity.
- Keep issues/PRs useful. Do not split trivial changes to increase counts.

## License status

No project license has been selected. This guide does not assign one or alter
existing contribution terms. License selection and any new contribution terms
require an explicit owner decision.
