# Security notes

Sam is a development project, not an approved production release. Phase 17 is
pending independent re-review. No supported security-release series or response
SLA is promised at this stage.

## Boundaries

The deterministic PermissionEngine is the authorization authority. A model
reply, voice match, UI control or MCP description cannot grant permissions.
Sensitive actions retain scoped, bound confirmations and authentication.
Read [architecture](docs/architecture.md), [permissions](docs/permissions.md),
[privacy](docs/privacy.md), [voice identity](docs/voice_identity.md) and
[production hardening](docs/production-hardening.md) for assumptions and limits.

## Reporting a concern

Do not put credentials, owner recordings, biometric material, private documents,
application data or a working exploit in a public issue. GitHub private
vulnerability reporting is not enabled at this documentation update.
Contact the [repository owner](https://github.com/SamSamaei73) through an existing
trusted channel to agree on a confidential route before sending sensitive
details. This document does not establish a new disclosure mailbox.

Identify the affected commit, environment, boundary and a minimal reproduction
using synthetic data. Sanitized, non-sensitive documentation/usability issues
can use the normal issue template.

Security fixes remain subject to independent review. Do not disable a
protection or weaken a test to make a demonstration pass.
