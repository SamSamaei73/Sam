"""CodingOperation → Permission Engine request mapping.

This module makes **no authorization decision itself** — it only
translates one ``CodingOperationRequest`` into a
``sam.permissions.models.PermissionRequest``, using a static mapping
mirroring ``sam.computer.capabilities``/``sam.computer.policy``. The
actual ALLOW / CONFIRM_REQUIRED / DENY decision is made exclusively by
``sam.permissions.engine.PermissionEngine.evaluate`` — see
``sam.coding.executor``, the only caller of both this module and that
engine. Duplicating any part of that decision here would be exactly the
"do not duplicate the Permission Engine" mistake the Phase 6 task warns
against.

Two existing Phase 3 resources are reused, deliberately not merged into
one: ``PermissionResource.GIT`` already exists and already classifies
``READ`` as LOW — ``GET_GIT_STATUS``/``GET_GIT_DIFF`` map onto it
directly rather than duplicating a row under a new resource. Everything
GIT does not cover (file I/O, running tests/lint/typecheck, the
diff-check verification step) uses the new, minimal
``PermissionResource.CODE`` added for this phase.

Scopes always start with the repository id (``PermissionScope.from_path``
/``.identifier`` reused directly from Phase 3), so a grant issued for one
repository can never be reused against a different one, and never
implicitly widens beyond the repository/path/operation it names — see
``docs/coding-agent.md``.
"""

from __future__ import annotations

import re

from sam.coding.models import CodingOperation
from sam.coding.operations import (
    CodingOperationRequest,
    CreateFileOperation,
    GetGitDiffOperation,
    GetGitStatusOperation,
    InspectRepositoryOperation,
    ListFilesOperation,
    ModifyFileOperation,
    ReadFileOperation,
    RunLintOperation,
    RunTestsOperation,
    RunTypecheckOperation,
    VerifyDiffOperation,
)
from sam.memory.sanitization import looks_like_secret as _content_looks_like_secret
from sam.permissions.models import (
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    RiskLevel,
)
from sam.permissions.policy import classify

_R = PermissionResource
_A = PermissionAction

_ResourceAction = tuple[PermissionResource, PermissionAction]

_OPERATION_TO_RESOURCE_ACTION: dict[CodingOperation, _ResourceAction] = {
    CodingOperation.INSPECT_REPOSITORY: (_R.CODE, _A.READ),
    CodingOperation.READ_FILE: (_R.CODE, _A.READ),
    CodingOperation.LIST_FILES: (_R.CODE, _A.READ),
    CodingOperation.GET_GIT_STATUS: (_R.GIT, _A.READ),
    CodingOperation.GET_GIT_DIFF: (_R.GIT, _A.READ),
    CodingOperation.CREATE_FILE: (_R.CODE, _A.WRITE),
    CodingOperation.MODIFY_FILE: (_R.CODE, _A.WRITE),
    CodingOperation.RUN_TESTS: (_R.CODE, _A.EXECUTE),
    CodingOperation.RUN_LINT: (_R.CODE, _A.EXECUTE),
    CodingOperation.RUN_TYPECHECK: (_R.CODE, _A.EXECUTE),
    CodingOperation.VERIFY_DIFF: (_R.CODE, _A.READ),
}

# Every CodingOperation must have a mapping — fail at import time if a
# future edit adds an operation without wiring its permission mapping.
if set(_OPERATION_TO_RESOURCE_ACTION) != set(CodingOperation):
    raise AssertionError("every CodingOperation must have a resource/action mapping")


def _operation_of(request: CodingOperationRequest) -> CodingOperation:
    if isinstance(request, InspectRepositoryOperation):
        return CodingOperation.INSPECT_REPOSITORY
    if isinstance(request, ReadFileOperation):
        return CodingOperation.READ_FILE
    if isinstance(request, ListFilesOperation):
        return CodingOperation.LIST_FILES
    if isinstance(request, GetGitStatusOperation):
        return CodingOperation.GET_GIT_STATUS
    if isinstance(request, GetGitDiffOperation):
        return CodingOperation.GET_GIT_DIFF
    if isinstance(request, CreateFileOperation):
        return CodingOperation.CREATE_FILE
    if isinstance(request, ModifyFileOperation):
        return CodingOperation.MODIFY_FILE
    if isinstance(request, RunTestsOperation):
        return CodingOperation.RUN_TESTS
    if isinstance(request, RunLintOperation):
        return CodingOperation.RUN_LINT
    if isinstance(request, RunTypecheckOperation):
        return CodingOperation.RUN_TYPECHECK
    if isinstance(request, VerifyDiffOperation):
        return CodingOperation.VERIFY_DIFF
    raise TypeError(f"unhandled coding operation request type: {type(request)!r}")


def _scope_for(
    operation: CodingOperation, request: CodingOperationRequest, *, repository_id: str
) -> PermissionScope:
    if isinstance(request, ReadFileOperation):
        return PermissionScope.from_path(f"{repository_id}/{request.path}")
    if isinstance(request, ListFilesOperation) and request.path:
        return PermissionScope.from_path(f"{repository_id}/{request.path}")
    if isinstance(request, CreateFileOperation | ModifyFileOperation):
        return PermissionScope.from_path(f"{repository_id}/{request.change.path}")
    # Operation-scoped (not path-scoped) requests: one segment per kind
    # of operation, so a grant for "tests" never authorizes "lint", and
    # a grant scoped to one repository never authorizes another.
    segment = {
        CodingOperation.INSPECT_REPOSITORY: "repository",
        CodingOperation.LIST_FILES: "repository",
        CodingOperation.GET_GIT_STATUS: "status",
        CodingOperation.GET_GIT_DIFF: "diff",
        CodingOperation.RUN_TESTS: "tests",
        CodingOperation.RUN_LINT: "lint",
        CodingOperation.RUN_TYPECHECK: "typecheck",
        CodingOperation.VERIFY_DIFF: "diff-check",
    }[operation]
    return PermissionScope.identifier(f"{repository_id}:{segment}")


def build_request(
    request: CodingOperationRequest, *, repository_id: str
) -> PermissionRequest:
    """Translate one operation request into a ``PermissionRequest``. Pure
    — no I/O, no calls to the Permission Engine."""

    operation = _operation_of(request)
    resource, action = _OPERATION_TO_RESOURCE_ACTION[operation]
    return PermissionRequest(
        principal=request.principal,
        action=action,
        resource=resource,
        scope=_scope_for(operation, request, repository_id=repository_id),
        reason=request.reason,
    )


def risk_for(operation: CodingOperation) -> RiskLevel:
    """The deterministic risk for one operation, straight from
    ``sam.permissions.policy`` — never computed independently. Used only
    for audit records; the actual authorization decision always comes
    from ``PermissionEngine.evaluate``.
    """

    resource, action = _OPERATION_TO_RESOURCE_ACTION[operation]
    entry = classify(resource, action)
    if entry is None:
        # Unreachable in practice (see the import-time assertion above
        # and sam.permissions.policy's GIT/CODE rows) — maximum caution
        # if it ever were.
        return RiskLevel.CRITICAL
    return entry.risk


def operation_for(request: CodingOperationRequest) -> CodingOperation:
    """Public accessor for the operation kind of a request — used by the
    executor/audit so they never need their own duplicate dispatch."""

    return _operation_of(request)


# --------------------------------------------------------------------- #
# Secret protection
#
# A best-effort, documented-as-non-exhaustive second layer on top of the
# Permission Engine: even an authorized, ALLOW-ed READ_FILE must not
# hand secret-looking material to a caller (which may forward it to an
# external provider) — see sam.coding.executor, the only place these are
# consulted, and docs/coding-agent.md's "Secret protection" section.
# --------------------------------------------------------------------- #

# Filename patterns restricted regardless of content — matched against
# the final path segment (never the full path, so a legitimate file
# named e.g. "src/credentials_test_fixture_reader.py" is judged as a
# whole segment match, not a substring one).
_RESTRICTED_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.env(\..+)?$", re.IGNORECASE),
    re.compile(r".*\.pem$", re.IGNORECASE),
    re.compile(r".*\.key$", re.IGNORECASE),
    re.compile(r"^credentials(\..+)?$", re.IGNORECASE),
    re.compile(r"^secrets?(\..+)?$", re.IGNORECASE),
    re.compile(r".*id_rsa.*", re.IGNORECASE),
    re.compile(r".*id_ed25519.*", re.IGNORECASE),
)


def is_restricted_filename(path: str) -> bool:
    """True if the final path segment matches a known secret-bearing
    filename pattern (``.env``, ``.env.*``, ``*.pem``, ``*.key``,
    ``credentials.*``, ``secret(s).*``, SSH private key names). This is
    a best-effort, documented-as-non-exhaustive allowlist-by-exclusion —
    see docs/coding-agent.md."""

    name = path.rsplit("/", 1)[-1]
    return any(pattern.match(name) for pattern in _RESTRICTED_FILENAME_PATTERNS)


def is_restricted_content(text: str) -> bool:
    """True if the text matches a known secret-like content pattern.
    Reuses ``sam.memory.sanitization.looks_like_secret`` directly rather
    than duplicating its pattern list — see that module's docstring for
    exactly what it catches and its documented limitations."""

    return _content_looks_like_secret(text)


__all__ = [
    "build_request",
    "is_restricted_content",
    "is_restricted_filename",
    "operation_for",
    "risk_for",
]
