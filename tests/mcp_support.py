"""Shared fixtures for the MCP gateway tests.

Everything here is synthetic: fake servers, fake transports, a fake
credential provider, fake verifiers. No network, no real provider, no real
secret. ``SECRET_MAIL``/``SECRET_SOCIAL`` are obviously-fake strings that
deliberately do not resemble any real credential format.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sam.mcp.audit import InMemoryMCPAuditSink, MCPAuditSink
from sam.mcp.credentials import Credential, FakeCredentialProvider
from sam.mcp.discovery import MCPDiscoveryService
from sam.mcp.gateway import MCPGateway
from sam.mcp.models import (
    CredentialReference,
    MCPAuditEvent,
    MCPDiscoveryResult,
    MCPServerDescriptor,
    MCPToolDescriptor,
    MCPToolPermissionBinding,
    MCPToolRequest,
    MCPToolSchema,
)
from sam.mcp.registry import MCPRegistryAdmin
from sam.mcp.transport import FakeMCPTransport, Handler
from sam.mcp.verification import MCPResultVerifier
from sam.permissions.audit import InMemoryAuditSink
from sam.permissions.confirmation import InMemoryConfirmationProvider
from sam.permissions.engine import PermissionEngine
from sam.permissions.models import (
    PermissionAction,
    PermissionGrant,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
)
from sam.permissions.store import InMemoryPermissionStore

NOW = datetime.now(UTC)
ALICE = Principal(kind=PrincipalKind.USER, id="alice")
BOB = Principal(kind=PrincipalKind.USER, id="bob")

SECRET_MAIL = "FAKE-CRED-mail-0001-not-a-real-secret"
SECRET_SOCIAL = "FAKE-CRED-social-0002-not-a-real-secret"
SECRET_CALENDAR = "FAKE-CRED-calendar-0003-not-a-real-secret"

MAIL_CRED = CredentialReference(server_id="mail", credential_id="mail-main")
SOCIAL_CRED = CredentialReference(server_id="social", credential_id="social-main")
CALENDAR_CRED = CredentialReference(server_id="calendar", credential_id="cal-main")

_ACCOUNT = {"type": "string", "minLength": 1, "maxLength": 64}


def _schema(properties: dict[str, Any], required: list[str]) -> MCPToolSchema:
    return MCPToolSchema.from_untrusted(
        {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
    )


def read_message_tool(**over: Any) -> MCPToolDescriptor:
    fields: dict[str, Any] = dict(
        server_id="mail",
        tool_name="read_message",
        description="Read one message.",
        input_schema=_schema(
            {"account": _ACCOUNT, "message_id": {"type": "string", "maxLength": 64}},
            ["account", "message_id"],
        ),
        binding=MCPToolPermissionBinding(
            resource=PermissionResource.GMAIL,
            action=PermissionAction.READ,
            scope_arguments=("account",),
        ),
        credential=MAIL_CRED,
        timeout_seconds=2.0,
    )
    fields.update(over)
    return MCPToolDescriptor(**fields)


def send_message_tool(**over: Any) -> MCPToolDescriptor:
    fields: dict[str, Any] = dict(
        server_id="mail",
        tool_name="send_message",
        description="Send one message.",
        input_schema=_schema(
            {
                "account": _ACCOUNT,
                "to": {"type": "string", "maxLength": 200},
                "subject": {"type": "string", "maxLength": 200},
                "body": {"type": "string", "maxLength": 5000},
            },
            ["account", "to", "subject", "body"],
        ),
        binding=MCPToolPermissionBinding(
            resource=PermissionResource.GMAIL,
            action=PermissionAction.SEND,
            scope_arguments=("account",),
        ),
        credential=MAIL_CRED,
        timeout_seconds=2.0,
        verification_required=True,
    )
    fields.update(over)
    return MCPToolDescriptor(**fields)


def list_events_tool(**over: Any) -> MCPToolDescriptor:
    fields: dict[str, Any] = dict(
        server_id="calendar",
        tool_name="list_events",
        description="List events.",
        input_schema=_schema(
            {
                "calendar_id": _ACCOUNT,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["calendar_id"],
        ),
        binding=MCPToolPermissionBinding(
            resource=PermissionResource.CALENDAR,
            action=PermissionAction.READ,
            scope_arguments=("calendar_id",),
        ),
        credential=CALENDAR_CRED,
        timeout_seconds=2.0,
    )
    fields.update(over)
    return MCPToolDescriptor(**fields)


def create_post_tool(**over: Any) -> MCPToolDescriptor:
    fields: dict[str, Any] = dict(
        server_id="social",
        tool_name="create_post",
        description="Publish a post.",
        input_schema=_schema(
            {"account": _ACCOUNT, "text": {"type": "string", "maxLength": 500}},
            ["account", "text"],
        ),
        binding=MCPToolPermissionBinding(
            resource=PermissionResource.SOCIAL_MEDIA,
            action=PermissionAction.PUBLISH,
            scope_arguments=("account",),
        ),
        credential=SOCIAL_CRED,
        timeout_seconds=2.0,
        verification_required=True,
    )
    fields.update(over)
    return MCPToolDescriptor(**fields)


def default_handlers() -> dict[str, dict[str, Handler]]:
    return {
        "mail": {
            "read_message": lambda args, cred: {
                "id": args["message_id"],
                "subject": "Hello",
                "body": "hi there",
            },
            "send_message": lambda args, cred: {"sent": True},
        },
        "calendar": {"list_events": lambda args, cred: {"events": []}},
        "social": {"create_post": lambda args, cred: {"posted": True}},
    }


class Harness:
    """A complete fake environment: three fake servers, real registry, real
    PermissionEngine, real in-memory confirmation provider."""

    def __init__(
        self,
        *,
        handlers: Mapping[str, Mapping[str, Handler]] | None = None,
        verifiers: Mapping[str, MCPResultVerifier] | None = None,
        audit_sink: MCPAuditSink | None = None,
        extra_tools: tuple[MCPToolDescriptor, ...] = (),
    ) -> None:
        self.pstore = InMemoryPermissionStore()
        self.confirmations = InMemoryConfirmationProvider()
        self.permission_audit = InMemoryAuditSink()
        self.pengine = PermissionEngine(
            store=self.pstore,
            confirmation_provider=self.confirmations,
            audit_sink=self.permission_audit,
        )
        self.admin = MCPRegistryAdmin()
        self.registry = self.admin.reader()
        for server_id in ("mail", "calendar", "social"):
            self.admin.register_server(
                MCPServerDescriptor(server_id=server_id, display_name=server_id.title())
            )
        for tool in (
            read_message_tool(),
            send_message_tool(),
            list_events_tool(),
            create_post_tool(),
            *extra_tools,
        ):
            self.admin.register_tool(tool)
        self.credentials = FakeCredentialProvider()
        self.credentials.add(MAIL_CRED, SECRET_MAIL)
        self.credentials.add(SOCIAL_CRED, SECRET_SOCIAL)
        self.credentials.add(CALENDAR_CRED, SECRET_CALENDAR)
        chosen = handlers or default_handlers()
        self.transports: dict[str, FakeMCPTransport] = {
            server: FakeMCPTransport(server, handlers=chosen.get(server, {}))
            for server in ("mail", "calendar", "social")
        }
        self.discovery = MCPDiscoveryService(
            registry=self.registry, transports=self.transports
        )
        self.audit = audit_sink if audit_sink is not None else InMemoryMCPAuditSink()
        self.verifiers = dict(verifiers or {})
        self.gateway = self._build_gateway()
        self._grant_seq = 0

    def _build_gateway(self) -> MCPGateway:
        return MCPGateway(
            registry=self.registry,
            permission_engine=self.pengine,
            transports=self.transports,
            credential_provider=self.credentials,
            verifiers=self.verifiers,
            audit_sink=self.audit,
        )

    def audit_events(self) -> list[MCPAuditEvent]:
        assert isinstance(self.audit, InMemoryMCPAuditSink)
        return list(self.audit.list_events())

    def discover_and_apply(self, server_id: str) -> MCPDiscoveryResult:
        """Untrusted discovery, then the trusted administrative step."""

        result = self.discovery.discover(server_id)
        self.admin.apply_discovery(result)
        return result

    def rebuild_gateway(self) -> MCPGateway:
        self.gateway = self._build_gateway()
        return self.gateway

    def grant(
        self,
        resource: PermissionResource,
        action: PermissionAction,
        *segments: str,
        principal: Principal = ALICE,
        expires_at: datetime | None = None,
        always_confirm: bool = False,
    ) -> str:
        self._grant_seq += 1
        grant_id = f"g{self._grant_seq}"
        self.pstore.create_grant(
            PermissionGrant(
                grant_id=grant_id,
                principal=principal,
                resource=resource,
                action=action,
                scope=PermissionScope(segments=tuple(segments)),
                always_require_confirmation=always_confirm,
                created_at=NOW,
                updated_at=NOW,
                expires_at=expires_at,
            )
        )
        return grant_id

    def grant_mail_read(self, account: str = "acct-1") -> str:
        return self.grant(
            PermissionResource.GMAIL,
            PermissionAction.READ,
            "mail",
            "read_message",
            account,
        )

    def grant_mail_send(self, account: str = "acct-1") -> str:
        return self.grant(
            PermissionResource.GMAIL,
            PermissionAction.SEND,
            "mail",
            "send_message",
            account,
        )

    def grant_post(self, account: str = "acct-1") -> str:
        return self.grant(
            PermissionResource.SOCIAL_MEDIA,
            PermissionAction.PUBLISH,
            "social",
            "create_post",
            account,
        )

    def grant_calendar(self, calendar_id: str = "cal-1") -> str:
        return self.grant(
            PermissionResource.CALENDAR,
            PermissionAction.READ,
            "calendar",
            "list_events",
            calendar_id,
        )

    def approve(self, confirmation_id: str, *, approved: bool = True) -> None:
        self.confirmations.decide(confirmation_id, approved=approved, now=NOW)

    def expire_time(self) -> datetime:
        return NOW + timedelta(days=1)


def read_request(
    *,
    principal: Principal = ALICE,
    tool_id: str = "mail:read_message",
    arguments: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> MCPToolRequest:
    args = (
        arguments
        if arguments is not None
        else {"account": "acct-1", "message_id": "m-1"}
    )
    kwargs: dict[str, Any] = dict(principal=principal, tool_id=tool_id, arguments=args)
    if request_id is not None:
        kwargs["request_id"] = request_id
    return MCPToolRequest(**kwargs)


def send_request(
    *,
    principal: Principal = ALICE,
    account: str = "acct-1",
    body: str = "hello there",
    request_id: str | None = None,
) -> MCPToolRequest:
    kwargs: dict[str, Any] = dict(
        principal=principal,
        tool_id="mail:send_message",
        arguments={
            "account": account,
            "to": "friend@example.test",
            "subject": "hi",
            "body": body,
        },
    )
    if request_id is not None:
        kwargs["request_id"] = request_id
    return MCPToolRequest(**kwargs)


def post_request(
    *, account: str = "acct-1", text: str = "hello world", request_id: str | None = None
) -> MCPToolRequest:
    kwargs: dict[str, Any] = dict(
        principal=ALICE,
        tool_id="social:create_post",
        arguments={"account": account, "text": text},
    )
    if request_id is not None:
        kwargs["request_id"] = request_id
    return MCPToolRequest(**kwargs)


def secret_handler(
    box: list[Credential | None],
) -> Callable[[Mapping[str, Any], Credential | None], object]:
    """A handler that records the credential object it was handed."""

    def handler(args: Mapping[str, Any], cred: Credential | None) -> object:
        box.append(cred)
        return {"ok": True}

    return handler
