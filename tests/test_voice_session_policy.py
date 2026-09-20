"""Voice sessions (bounded, id-only, isolated) and the permission policy."""

from __future__ import annotations

import dataclasses

import pytest

import sam.voice.session as session_module
from sam.permissions.models import PermissionAction, PermissionResource, RiskLevel
from sam.voice import policy
from sam.voice.errors import (
    DuplicateUtteranceError,
    VoiceLimitError,
    VoicePolicyError,
    VoiceSessionError,
)
from sam.voice.models import MAX_VOICE_SESSION_UTTERANCES, VoiceOperation
from sam.voice.session import VoiceSessionManager
from tests.voice_support import ALICE, BOB

DIGEST = "ab" * 32


class TestSessions:
    def test_open_returns_a_fresh_safe_id(self) -> None:
        mgr = VoiceSessionManager()
        a, b = mgr.open(ALICE), mgr.open(ALICE)
        assert a != b and len(a) == 32
        assert mgr.open_count() == 2

    def test_owner_can_use_its_session(self) -> None:
        mgr = VoiceSessionManager()
        sid = mgr.open(ALICE)
        mgr.require_open(sid, ALICE)
        assert mgr.info(sid, ALICE).utterance_count == 0

    def test_other_principal_cannot_use_or_probe_a_session(self) -> None:
        mgr = VoiceSessionManager()
        sid = mgr.open(ALICE)
        for call in (
            lambda: mgr.require_open(sid, BOB),
            lambda: mgr.reserve_utterance(sid, BOB, "u"),
            lambda: mgr.close(sid, BOB),
            lambda: mgr.info(sid, BOB),
            lambda: mgr.has_utterance(sid, BOB, "u"),
        ):
            with pytest.raises(VoiceSessionError):
                call()
        # An unknown id and someone else's id are indistinguishable.
        with pytest.raises(VoiceSessionError) as unknown:
            mgr.require_open("nope", BOB)
        with pytest.raises(VoiceSessionError) as foreign:
            mgr.require_open(sid, BOB)
        assert str(unknown.value) == str(foreign.value)

    def test_close_removes_the_session(self) -> None:
        mgr = VoiceSessionManager()
        sid = mgr.open(ALICE)
        mgr.close(sid, ALICE)
        with pytest.raises(VoiceSessionError):
            mgr.require_open(sid, ALICE)
        assert mgr.open_count() == 0

    def test_double_close_is_an_error(self) -> None:
        mgr = VoiceSessionManager()
        sid = mgr.open(ALICE)
        mgr.close(sid, ALICE)
        with pytest.raises(VoiceSessionError):
            mgr.close(sid, ALICE)

    def test_duplicate_utterance_id_rejected(self) -> None:
        mgr = VoiceSessionManager()
        sid = mgr.open(ALICE)
        mgr.reserve_utterance(sid, ALICE, "u1")
        with pytest.raises(DuplicateUtteranceError):
            mgr.reserve_utterance(sid, ALICE, "u1")

    def test_same_utterance_id_is_allowed_in_different_sessions(self) -> None:
        mgr = VoiceSessionManager()
        a, b = mgr.open(ALICE), mgr.open(ALICE)
        mgr.reserve_utterance(a, ALICE, "u1")
        mgr.reserve_utterance(b, ALICE, "u1")

    def test_utterance_bound(self) -> None:
        mgr = VoiceSessionManager()
        sid = mgr.open(ALICE)
        for i in range(MAX_VOICE_SESSION_UTTERANCES):
            mgr.reserve_utterance(sid, ALICE, f"u{i}")
        with pytest.raises(VoiceLimitError):
            mgr.reserve_utterance(sid, ALICE, "one-too-many")
        assert mgr.info(sid, ALICE).utterance_count == MAX_VOICE_SESSION_UTTERANCES

    def test_open_session_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_module, "MAX_OPEN_VOICE_SESSIONS", 3)
        mgr = VoiceSessionManager()
        for _ in range(3):
            mgr.open(ALICE)
        with pytest.raises(VoiceLimitError):
            mgr.open(ALICE)

    def test_managers_do_not_share_state(self) -> None:
        a, b = VoiceSessionManager(), VoiceSessionManager()
        sid = a.open(ALICE)
        with pytest.raises(VoiceSessionError):
            b.require_open(sid, ALICE)

    def test_session_records_only_identifiers_never_content(self) -> None:
        names = {f.name for f in dataclasses.fields(session_module._Session)}
        assert names == {"session_id", "owner", "created_at", "utterances"}
        assert not {"transcript", "audio", "text", "history"} & names

    def test_session_info_is_a_plain_snapshot(self) -> None:
        mgr = VoiceSessionManager()
        sid = mgr.open(ALICE)
        info = mgr.info(sid, ALICE)
        mgr.reserve_utterance(sid, ALICE, "u")
        assert info.utterance_count == 0  # snapshot, not a live handle


class TestPolicy:
    def test_operation_mapping_is_explicit(self) -> None:
        assert policy.resource_action_for(VoiceOperation.START_SESSION) == (
            PermissionResource.VOICE,
            PermissionAction.CREATE,
        )
        assert policy.resource_action_for(VoiceOperation.PROCESS_UTTERANCE) == (
            PermissionResource.VOICE,
            PermissionAction.READ,
        )
        assert policy.resource_action_for(VoiceOperation.END_SESSION) == (
            PermissionResource.VOICE,
            PermissionAction.UPDATE,
        )

    def test_risk_comes_from_the_permissions_table(self) -> None:
        assert policy.risk_for(VoiceOperation.START_SESSION) is RiskLevel.LOW
        assert policy.risk_for(VoiceOperation.PROCESS_UTTERANCE) is RiskLevel.MEDIUM
        assert policy.risk_for(VoiceOperation.END_SESSION) is RiskLevel.LOW

    def test_scope_is_deterministic_and_hierarchical(self) -> None:
        start = policy.build_permission_request(VoiceOperation.START_SESSION, ALICE)
        proc = policy.build_permission_request(
            VoiceOperation.PROCESS_UTTERANCE,
            ALICE,
            session_id="s1",
            utterance_id="u1",
            audio_digest=DIGEST,
        )
        assert start.scope.segments == ("session",)
        assert proc.scope.segments == ("session", "s1")
        assert start.scope.contains(proc.scope)

    def test_confirmation_target_binds_the_utterance_and_audio(self) -> None:
        proc = policy.build_permission_request(
            VoiceOperation.PROCESS_UTTERANCE,
            ALICE,
            session_id="s1",
            utterance_id="u1",
            audio_digest=DIGEST,
        )
        assert proc.target == f"process_utterance u1 #{DIGEST[:16]}"
        assert proc.correlation_id == "u1"

    def test_missing_context_is_a_policy_error(self) -> None:
        with pytest.raises(VoicePolicyError):
            policy.build_permission_request(VoiceOperation.END_SESSION, ALICE)
        with pytest.raises(VoicePolicyError):
            policy.build_permission_request(
                VoiceOperation.PROCESS_UTTERANCE, ALICE, session_id="s"
            )

    def test_execute_row_is_reserved_high_and_unused(self) -> None:
        from sam.permissions.policy import classify

        entry = classify(PermissionResource.VOICE, PermissionAction.EXECUTE)
        assert (
            entry is not None
            and entry.risk is RiskLevel.HIGH
            and entry.requires_confirmation
        )
        used = {policy.resource_action_for(op)[1] for op in VoiceOperation}
        assert PermissionAction.EXECUTE not in used

    def test_no_voice_row_grants_send_publish_delete_or_approve(self) -> None:
        from sam.permissions.policy import classify

        for action in (
            PermissionAction.SEND,
            PermissionAction.PUBLISH,
            PermissionAction.DELETE,
            PermissionAction.APPROVE,
            PermissionAction.WRITE,
        ):
            assert classify(PermissionResource.VOICE, action) is None
