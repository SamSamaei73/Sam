"""Sam's Permission Engine (Phase 3).

Answers "is Sam allowed to perform this specific action on this specific
resource, in this specific scope, right now?" — deterministically, and
never by asking the LLM. See ``sam.permissions.engine.PermissionEngine``
for the evaluation contract and precedence rules, and ``docs/permissions.md``
for the full model and threat-model writeup.

No tool, MCP server, or integration is implemented in this package — it is
a pure authorization boundary that later phases will call before executing
anything real.
"""
