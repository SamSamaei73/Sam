"""Sam's desktop bridge (Phase 11): the narrow local backend the Tauri shell
talks to.

    The Desktop UI is not an authorization boundary.
    PermissionEngine remains the sole authorization authority.

This package adds *no* engine and *no* policy. It composes the existing
engines (AgentCore, Knowledge, Memory, MCP registry reader, Voice, TTS,
PermissionEngine) behind a small set of fixed, typed HTTP routes under
``/desktop/v1`` and translates their results into safe, bounded shapes for the
UI. The principal is bound here, server-side; no request can carry one.
See ``docs/desktop.md``.
"""
