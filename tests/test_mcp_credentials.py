"""Tests for the credential boundary, the fake transport, and environment
isolation. All credentials are synthetic."""

from __future__ import annotations

import copy
import logging
import os
import pickle

import pytest

from sam.mcp.credentials import Credential, FakeCredentialProvider
from sam.mcp.errors import MCPCredentialError, MCPRegistrationError, MCPTransportError
from sam.mcp.models import CredentialReference, MCPExecutionContext, MCPToolCall
from sam.mcp.transport import FakeMCPTransport, build_transport_environment
from tests.mcp_support import (
    ALICE,
    MAIL_CRED,
    SECRET_CALENDAR,
    SECRET_MAIL,
    SECRET_SOCIAL,
    SOCIAL_CRED,
)

VALUE = "FAKE-CRED-unit-9999-not-a-real-secret"


class TestCredentialObject:
    def test_reveal_returns_the_value(self) -> None:
        assert Credential(VALUE).reveal() == VALUE

    def test_repr_str_format_and_fstring_are_redacted(self) -> None:
        cred = Credential(VALUE)
        for text in (
            repr(cred),
            str(cred),
            format(cred),
            f"{cred}",
            f"{cred!r}",
            f"{cred}",
        ):
            assert VALUE not in text
            assert "redacted" in text

    def test_container_reprs_are_redacted(self) -> None:
        cred = Credential(VALUE)
        assert VALUE not in repr([cred])
        assert VALUE not in repr({"k": cred})
        assert VALUE not in repr((cred,))

    def test_no_instance_dict_so_vars_cannot_leak(self) -> None:
        cred = Credential(VALUE)
        with pytest.raises(TypeError):
            vars(cred)
        assert not hasattr(cred, "__dict__")

    def test_cannot_be_pickled_or_copied(self) -> None:
        cred = Credential(VALUE)
        with pytest.raises(TypeError):
            pickle.dumps(cred)
        with pytest.raises(TypeError):
            copy.copy(cred)
        with pytest.raises(TypeError):
            copy.deepcopy(cred)

    def test_is_immutable(self) -> None:
        cred = Credential(VALUE)
        with pytest.raises(AttributeError):
            cred.anything = 1
        with pytest.raises(AttributeError):
            cred._value = "other"

    def test_equality_is_identity_only(self) -> None:
        assert Credential(VALUE) != Credential(VALUE)

    def test_appears_in(self) -> None:
        cred = Credential(VALUE)
        assert cred.appears_in(f"prefix {VALUE} suffix")
        assert not cred.appears_in("nothing here")

    @pytest.mark.parametrize("bad", ["", None, 5, "x" * 5000])
    def test_invalid_values_rejected(self, bad: object) -> None:
        with pytest.raises(MCPCredentialError):
            Credential(bad)  # type: ignore[arg-type]

    def test_logging_a_credential_does_not_emit_the_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cred = Credential(VALUE)
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("t").info("cred=%s repr=%r", cred, cred)
        assert VALUE not in caplog.text


class TestFakeProvider:
    def test_resolves_bound_reference(self) -> None:
        provider = FakeCredentialProvider()
        provider.add(MAIL_CRED, SECRET_MAIL)
        cred = provider.resolve(MAIL_CRED, server_id="mail", tool_name="t")
        assert cred.reveal() == SECRET_MAIL

    def test_unknown_reference_raises_generic_error(self) -> None:
        provider = FakeCredentialProvider()
        with pytest.raises(MCPCredentialError) as info:
            provider.resolve(MAIL_CRED, server_id="mail", tool_name="t")
        assert "mail-main" not in str(info.value)

    def test_cross_server_resolution_refused(self) -> None:
        provider = FakeCredentialProvider()
        provider.add(MAIL_CRED, SECRET_MAIL)
        provider.add(SOCIAL_CRED, SECRET_SOCIAL)
        with pytest.raises(MCPCredentialError):
            provider.resolve(MAIL_CRED, server_id="social", tool_name="t")

    def test_same_credential_id_on_two_servers_are_distinct(self) -> None:
        provider = FakeCredentialProvider()
        a = CredentialReference(server_id="alpha", credential_id="main")
        b = CredentialReference(server_id="beta", credential_id="main")
        provider.add(a, SECRET_MAIL)
        provider.add(b, SECRET_CALENDAR)
        assert (
            provider.resolve(a, server_id="alpha", tool_name="t").reveal()
            == SECRET_MAIL
        )
        assert (
            provider.resolve(b, server_id="beta", tool_name="t").reveal()
            == SECRET_CALENDAR
        )

    def test_provider_repr_has_no_secret(self) -> None:
        provider = FakeCredentialProvider()
        provider.add(MAIL_CRED, SECRET_MAIL)
        assert SECRET_MAIL not in repr(provider)
        assert SECRET_MAIL not in str(vars(provider))

    def test_provider_never_reads_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAIL_MAIN", "FROM-ENVIRONMENT")
        monkeypatch.setenv("CREDENTIAL_MAIL_MAIN", "FROM-ENVIRONMENT")
        provider = FakeCredentialProvider()
        with pytest.raises(MCPCredentialError):
            provider.resolve(MAIL_CRED, server_id="mail", tool_name="t")


class TestEnvironmentIsolation:
    def test_builds_only_the_explicitly_trusted_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
        env = build_transport_environment({"LANG": "C", "SAM_MCP_MODE": "test"})
        assert env == {"LANG": "C", "SAM_MCP_MODE": "test"}
        assert "ambient-secret" not in str(env)
        assert "PATH" not in env  # nothing inherited implicitly

    def test_empty_trusted_config_yields_empty_environment(self) -> None:
        assert build_transport_environment({}) == {}

    @pytest.mark.parametrize(
        "name",
        [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "GITHUB_TOKEN",
            "SLACK_BOT_TOKEN",
            "DATABASE_URL",
            "MY_SECRET",
            "DB_PASSWORD",
            "SERVICE_CREDENTIALS",
        ],
    )
    def test_credential_bearing_names_refused(self, name: str) -> None:
        with pytest.raises(MCPRegistrationError):
            build_transport_environment({name: "x"})

    @pytest.mark.parametrize("name", ["lower", "1BAD", "BAD-NAME", "", "A B", "X" * 65])
    def test_malformed_names_refused(self, name: str) -> None:
        with pytest.raises(MCPRegistrationError):
            build_transport_environment({name: "x"})

    @pytest.mark.parametrize("value", ["a\x00b", "a\nb", "x" * 2000])
    def test_malformed_values_refused(self, value: str) -> None:
        with pytest.raises(MCPRegistrationError):
            build_transport_environment({"OK_NAME": value})

    def test_too_many_entries_refused(self) -> None:
        with pytest.raises(MCPRegistrationError):
            build_transport_environment({f"V{i}": "x" for i in range(40)})

    def test_does_not_mutate_or_read_os_environ(self) -> None:
        before = dict(os.environ)
        build_transport_environment({"OK_NAME": "v"})
        assert dict(os.environ) == before


class TestFakeTransport:
    def _call(self, transport: FakeMCPTransport, server: str = "mail") -> object:
        return transport.call_tool(
            MCPToolCall(server_id=server, tool_name="t", arguments={"a": 1}),
            MCPExecutionContext(
                request_id="r", execution_id="e", principal=ALICE, timeout_seconds=1
            ),
            None,
        )

    def test_dispatches_to_handler_and_records_calls(self) -> None:
        transport = FakeMCPTransport(
            "mail", handlers={"t": lambda a, c: {"ok": a["a"]}}
        )
        assert self._call(transport) == {"ok": 1}
        assert transport.call_count == 1
        assert transport.calls == [("t", {"a": 1}, False)]

    def test_records_only_that_a_credential_was_present_not_its_value(self) -> None:
        transport = FakeMCPTransport("mail", handlers={"t": lambda a, c: 1})
        transport.call_tool(
            MCPToolCall(server_id="mail", tool_name="t", arguments={}),
            MCPExecutionContext(
                request_id="r", execution_id="e", principal=ALICE, timeout_seconds=1
            ),
            Credential(SECRET_MAIL),
        )
        assert transport.calls == [("t", {}, True)]
        assert SECRET_MAIL not in repr(transport.calls)

    def test_misrouted_call_is_refused(self) -> None:
        transport = FakeMCPTransport("mail", handlers={"t": lambda a, c: 1})
        with pytest.raises(MCPTransportError):
            self._call(transport, server="social")

    def test_unimplemented_tool_is_refused(self) -> None:
        with pytest.raises(MCPTransportError):
            self._call(FakeMCPTransport("mail"))
