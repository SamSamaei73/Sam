"""KnowledgeOperation → Permission Engine request mapping.

This module makes **no authorization decision itself** — it only
translates one ``KnowledgeOperationRequest`` into a
``sam.permissions.models.PermissionRequest``, mirroring
``sam.coding.policy``/``sam.computer.capabilities``. The actual ALLOW /
CONFIRM_REQUIRED / DENY decision is made exclusively by
``sam.permissions.engine.PermissionEngine.evaluate`` — see
``sam.knowledge.engine``, the only caller of both this module and that
engine.

Scopes always start with the collection id, so a grant issued for one
collection can never be reused against another — see
``docs/knowledge.md``'s "Collection isolation" section. A single-resource
operation (``GET_RESOURCE``/``REMOVE_RESOURCE``) scopes down to that
exact resource id; a collection-wide operation (``INGEST_RESOURCE``
— the resource does not exist yet — ``LIST_RESOURCES``, ``RETRIEVE``)
scopes to the collection plus one fixed operation-kind segment, the same
pattern ``sam.coding.policy`` uses for its non-path-scoped operations.
"""

from __future__ import annotations

from sam.knowledge.models import (
    GetResourceRequest,
    IngestResourceRequest,
    KnowledgeOperation,
    KnowledgeOperationRequest,
    ListResourcesRequest,
    RemoveResourceRequest,
    RetrieveRequest,
)
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

# The scope identifier used for a cross-collection (``collection_id=None``)
# retrieval query. Deliberately distinct from every real collection id, so
# a grant scoped to one real collection's ``retrieve`` segment never
# matches a global, all-collections search — that requires its own,
# separately granted, broader authorization. See docs/knowledge.md's
# "Collection isolation" section.
GLOBAL_COLLECTION_ID = "__all__"

_ResourceAction = tuple[PermissionResource, PermissionAction]

_OPERATION_TO_RESOURCE_ACTION: dict[KnowledgeOperation, _ResourceAction] = {
    KnowledgeOperation.INGEST_RESOURCE: (_R.KNOWLEDGE, _A.WRITE),
    KnowledgeOperation.GET_RESOURCE: (_R.KNOWLEDGE, _A.READ),
    KnowledgeOperation.LIST_RESOURCES: (_R.KNOWLEDGE, _A.READ),
    KnowledgeOperation.RETRIEVE: (_R.KNOWLEDGE, _A.READ),
    KnowledgeOperation.REMOVE_RESOURCE: (_R.KNOWLEDGE, _A.DELETE),
}

# Every KnowledgeOperation must have a mapping — fail at import time if a
# future edit adds an operation without wiring its permission mapping.
if set(_OPERATION_TO_RESOURCE_ACTION) != set(KnowledgeOperation):
    raise AssertionError("every KnowledgeOperation must have a resource/action mapping")


def _operation_of(request: KnowledgeOperationRequest) -> KnowledgeOperation:
    if isinstance(request, IngestResourceRequest):
        return KnowledgeOperation.INGEST_RESOURCE
    if isinstance(request, GetResourceRequest):
        return KnowledgeOperation.GET_RESOURCE
    if isinstance(request, ListResourcesRequest):
        return KnowledgeOperation.LIST_RESOURCES
    if isinstance(request, RetrieveRequest):
        return KnowledgeOperation.RETRIEVE
    if isinstance(request, RemoveResourceRequest):
        return KnowledgeOperation.REMOVE_RESOURCE
    raise TypeError(f"unhandled knowledge operation request type: {type(request)!r}")


def _collection_of(request: KnowledgeOperationRequest) -> str:
    if isinstance(request, RetrieveRequest):
        return request.query.collection_id or GLOBAL_COLLECTION_ID
    return request.collection_id


def _scope_for(
    operation: KnowledgeOperation,
    request: KnowledgeOperationRequest,
    *,
    collection_id: str,
) -> PermissionScope:
    if isinstance(request, GetResourceRequest | RemoveResourceRequest):
        return PermissionScope.from_path(f"{collection_id}/{request.resource_id}")
    segment = {
        KnowledgeOperation.INGEST_RESOURCE: "ingest",
        KnowledgeOperation.LIST_RESOURCES: "list",
        KnowledgeOperation.RETRIEVE: "retrieve",
    }[operation]
    return PermissionScope.identifier(f"{collection_id}:{segment}")


def build_request(request: KnowledgeOperationRequest) -> PermissionRequest:
    """Translate one operation request into a ``PermissionRequest``. Pure
    — no I/O, no calls to the Permission Engine."""

    operation = _operation_of(request)
    collection_id = _collection_of(request)
    resource, action = _OPERATION_TO_RESOURCE_ACTION[operation]
    return PermissionRequest(
        principal=request.principal,
        action=action,
        resource=resource,
        scope=_scope_for(operation, request, collection_id=collection_id),
        reason=request.reason,
    )


def risk_for(operation: KnowledgeOperation) -> RiskLevel:
    """The deterministic risk for one operation, straight from
    ``sam.permissions.policy`` — never computed independently."""

    resource, action = _OPERATION_TO_RESOURCE_ACTION[operation]
    entry = classify(resource, action)
    if entry is None:
        # Unreachable given the import-time assertion above and the
        # permissions policy table's KNOWLEDGE rows — maximum caution if
        # it ever were.
        return RiskLevel.CRITICAL
    return entry.risk


def operation_for(request: KnowledgeOperationRequest) -> KnowledgeOperation:
    """Public accessor for the operation kind of a request — used by the
    engine/audit so they never need their own duplicate dispatch."""

    return _operation_of(request)


def collection_for(request: KnowledgeOperationRequest) -> str:
    """Public accessor mirroring ``operation_for`` — the collection id a
    request targets, with ``RetrieveRequest``'s "search everywhere" case
    (``query.collection_id is None``) resolved to ``GLOBAL_COLLECTION_ID``
    rather than any real collection."""

    return _collection_of(request)


__all__ = [
    "GLOBAL_COLLECTION_ID",
    "build_request",
    "collection_for",
    "operation_for",
    "risk_for",
]
