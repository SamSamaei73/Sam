# Roadmap

This records existing readiness work, not release dates or permission to start
another phase. [PROJECT_SPEC.json](../PROJECT_SPEC.json) remains authoritative.

## Current priority: Phase 17 review

- Re-review durability, startup failure, backup/restore and external-action
  idempotency remediation against [production hardening](production-hardening.md).
- Preserve recovered work and record reproducible checks on the candidate
  commit. Distinguish baseline failures from regressions.
- Keep real Career submission/sending unavailable until readiness requirements
  and independent review are satisfied.

## Voice reliability

- Resolve short-phrase owner verification with measured evidence while
  preserving enrollment and permission boundaries.
- Validate owner and other-speaker recordings, languages, microphone denial,
  cancellation, hiding the window and follow-up turns.
- Measure wake latency, memory, noise and acoustic endpointing on the target
  laptop. A different speaker model is not a proven fix by itself.

See [voice activation](voice-activation.md) for observed limits and manual
validation. Do not lower identity thresholds to create a successful demo.

## Before a supported release

- Complete required phase/security review and record the exact candidate and
  applicable verification results.
- Resolve or explicitly account for reproducible baseline test failures.
- Validate installation and packaging, including signing/notarization decisions
  and a clean first run on the supported target.
- Have the owner select a project license after reviewing dependency and model
  redistribution terms. This roadmap does not assign a license.
- Prepare release notes from actual merged changes and capture labeled
  screenshots using synthetic data. Publish a release only when it exists.

Documentation corrections, reproducible bug reports and focused regression
fixes can use the [contribution workflow](../CONTRIBUTING.md). Group one coherent
change into one PR. Quality and review evidence take priority over counts.
