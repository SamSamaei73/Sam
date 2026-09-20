"""Challenge/replay, Guest Mode, and 'voice is not authorization'."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from sam.permissions.models import (
    PermissionAction,
    PermissionRequest,
    PermissionResource,
    PermissionScope,
    Principal,
    PrincipalKind,
    RiskLevel,
)
from sam.voice_identity.challenge import (
    DIGIT_COUNT,
    Challenge,
    extract_digits_and_words,
)
from sam.voice_identity.coordinator import SpeakerDecision
from sam.voice_identity.errors import AuthorizationError, ChallengeError, GuestModeError
from sam.voice_identity.guest import GUEST_DENIED, GuestCapability
from sam.voice_identity.policy import (
    GUEST_DEFAULT_MINUTES,
    GUEST_MAX_MINUTES,
    IdentityMode,
    SpeakerClass,
)
from tests.voice_identity_support import (
    OTHER_VOICE,
    OWNER_VOICE,
    Harness,
    permission_engine,
)


def say(h: Harness, challenge: Challenge, text: str | None = None) -> None:
    h.stt._result = (
        text
        if text is not None
        else f"{' '.join(challenge.digits)} {challenge.word_id}"
    )


def enrolled() -> Harness:
    h = Harness()
    h.enroll_owner()
    return h


# --------------------------------------------------------------- challenges


def test_challenges_are_random_and_bounded() -> None:
    h = Harness()
    seen = {h.coordinator.issue_challenge("s").digits for _ in range(60)}
    assert len(seen) > 30 and all(len(d) == DIGIT_COUNT and d.isdigit() for d in seen)
    assert h.challenges.pending_count() <= 64


def test_challenge_display_in_english_and_persian() -> None:
    c = Harness().coordinator.issue_challenge("s")
    assert c.display("en").startswith("Please say:")
    assert c.display("fa").startswith("بگویید:")
    assert " ".join(c.digits) in c.display("en")


@pytest.mark.parametrize(
    ("text", "digits", "words"),
    [
        ("7 4 9 2 blue", "7492", {"blue"}),
        ("seven four nine two Blue", "7492", {"blue"}),
        ("7492 blue", "7492", {"blue"}),
        ("۷ ۴ ۹ ۲ آبی", "7492", {"blue"}),
        ("هفت چهار نه دو سبز", "7492", {"green"}),
        ("٧٤٩٢ red", "7492", {"red"}),
        ("please say 7, 4, 9, 2, gold.", "7492", {"gold"}),
        ("زرد  ۷۴۹۲", "7492", {"yellow"}),
        ("nothing useful here", "", set()),
    ],
)
def test_transcript_normalization_for_both_languages(
    text: str, digits: str, words: set[str]
) -> None:
    got_digits, got_words = extract_digits_and_words(text)
    assert got_digits == digits and set(got_words) == words


def test_challenge_expires_and_is_one_time_and_session_bound() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    h.clock.advance(seconds=61)
    with pytest.raises(ChallengeError):
        h.challenges.evaluate(c.challenge_id, "s1", "x")
    c = h.coordinator.issue_challenge("s1")
    assert h.challenges.evaluate(
        c.challenge_id, "s1", f"{' '.join(c.digits)} {c.word_id}"
    )
    with pytest.raises(ChallengeError):  # consumed
        h.challenges.evaluate(c.challenge_id, "s1", f"{' '.join(c.digits)} {c.word_id}")
    c = h.coordinator.issue_challenge("s1")
    with pytest.raises(ChallengeError):  # other session
        h.challenges.evaluate(c.challenge_id, "s2", f"{' '.join(c.digits)} {c.word_id}")
    with pytest.raises(ChallengeError):  # unknown/forged id
        h.challenges.evaluate("f" * 32, "s1", "x")


def test_a_wrong_answer_burns_the_challenge() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    assert h.challenges.evaluate(c.challenge_id, "s1", "0 0 0 0 blue") in (False, True)
    with pytest.raises(ChallengeError):
        h.challenges.evaluate(c.challenge_id, "s1", f"{' '.join(c.digits)} {c.word_id}")


def test_owner_speaking_the_challenge_yields_a_proof() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    proof = h.coordinator.prove_owner(
        session_id="s1", challenge_id=c.challenge_id, audio=h.audio_input(OWNER_VOICE)
    )
    assert proof is not None and proof.purpose == "start_guest"


def test_correct_phrase_from_the_wrong_speaker_fails() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    assert (
        h.coordinator.prove_owner(
            session_id="s1",
            challenge_id=c.challenge_id,
            audio=h.audio_input(OTHER_VOICE),
        )
        is None
    )


def test_owner_with_the_wrong_phrase_fails() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    say(h, c, "enable guest mode please")
    assert (
        h.coordinator.prove_owner(
            session_id="s1",
            challenge_id=c.challenge_id,
            audio=h.audio_input(OWNER_VOICE),
        )
        is None
    )


def test_replayed_recording_cannot_reuse_a_challenge() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    audio = h.audio_input(OWNER_VOICE)
    assert h.coordinator.prove_owner(
        session_id="s1", challenge_id=c.challenge_id, audio=audio
    )
    # The same recording replayed against the same (now consumed) challenge...
    assert (
        h.coordinator.prove_owner(
            session_id="s1", challenge_id=c.challenge_id, audio=audio
        )
        is None
    )
    # ...or against a NEW challenge (the old recording says the old digits).
    c2 = h.coordinator.issue_challenge("s1")
    assert (
        h.coordinator.prove_owner(
            session_id="s1", challenge_id=c2.challenge_id, audio=audio
        )
        is None
        or c2.digits == c.digits
    )


def test_cross_session_and_stale_challenges_fail() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    assert (
        h.coordinator.prove_owner(
            session_id="other",
            challenge_id=c.challenge_id,
            audio=h.audio_input(OWNER_VOICE),
        )
        is None
    )
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    h.clock.advance(seconds=90)
    assert (
        h.coordinator.prove_owner(
            session_id="s1",
            challenge_id=c.challenge_id,
            audio=h.audio_input(OWNER_VOICE),
        )
        is None
    )


def test_failure_is_indistinguishable_no_oracle() -> None:
    h = enrolled()
    outcomes = []
    for voice, phrase in [(OTHER_VOICE, True), (OWNER_VOICE, False)]:
        c = h.coordinator.issue_challenge("s1")
        say(h, c, None if phrase else "wrong words")
        outcomes.append(
            h.coordinator.prove_owner(
                session_id="s1", challenge_id=c.challenge_id, audio=h.audio_input(voice)
            )
        )
    assert outcomes == [None, None]
    assert h.audit.events()[-1].reason_code == "verification_failed"


def test_proofs_are_forgery_and_replay_resistant() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    proof = h.coordinator.prove_owner(
        session_id="s1", challenge_id=c.challenge_id, audio=h.audio_input(OWNER_VOICE)
    )
    assert proof is not None
    with pytest.raises(AuthorizationError):
        h.proofs.consume(
            replace(proof, session_id="s2"), session_id="s2", purpose="start_guest"
        )
    with pytest.raises(AuthorizationError):
        h.proofs.consume(proof, session_id="other", purpose="start_guest")
    h.proofs.consume(proof, session_id="s1", purpose="start_guest")
    with pytest.raises(AuthorizationError):
        h.proofs.consume(proof, session_id="s1", purpose="start_guest")
    with pytest.raises(AuthorizationError):
        h.proofs.consume(None, session_id="s1", purpose="start_guest")


def test_stale_proof_is_refused() -> None:
    h = enrolled()
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    proof = h.coordinator.prove_owner(
        session_id="s1", challenge_id=c.challenge_id, audio=h.audio_input(OWNER_VOICE)
    )
    h.clock.advance(seconds=61)
    with pytest.raises(AuthorizationError):
        h.proofs.consume(proof, session_id="s1", purpose="start_guest")


# --------------------------------------------------------------- guest mode


def test_guest_mode_is_off_by_default_and_guests_are_blocked() -> None:
    h = enrolled()
    assert not h.guests.status().active
    d = h.coordinator.decide(h.audio_input(OTHER_VOICE))
    assert d.speaker_class is SpeakerClass.BLOCKED and d.mode is IdentityMode.OWNER_ONLY


def test_owner_is_recognised_and_unknown_audio_is_blocked() -> None:
    h = enrolled()
    d = h.coordinator.decide(h.audio_input(OWNER_VOICE))
    assert d.speaker_class is SpeakerClass.OWNER
    from sam.voice.models import AudioFormat, AudioInput

    bad = h.coordinator.decide(
        AudioInput(content=b"not audio", declared_format=AudioFormat.WAV_PCM16)
    )
    assert (
        bad.speaker_class is SpeakerClass.BLOCKED and bad.reason_code == "audio_invalid"
    )


def test_activation_requires_owner_proof_and_step_up() -> None:
    h = enrolled()
    with pytest.raises(AuthorizationError):
        h.guests.activate(None, h.step_up.mint("start_guest"), session_id="s1")
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    proof = h.coordinator.prove_owner(
        session_id="s1", challenge_id=c.challenge_id, audio=h.audio_input(OWNER_VOICE)
    )
    with pytest.raises(AuthorizationError):  # trusted authentication is mandatory too
        h.guests.activate(proof, None, session_id="s1")
    assert not h.guests.status().active


def test_a_recorded_enable_command_cannot_start_guest_mode() -> None:
    h = enrolled()
    h.stt._result = "Sam, allow guest conversation for 20 minutes."
    c = h.coordinator.issue_challenge("s1")
    proof = h.coordinator.prove_owner(
        session_id="s1", challenge_id=c.challenge_id, audio=h.audio_input(OWNER_VOICE)
    )
    assert proof is None  # the transcript alone is worthless; no fresh challenge spoken
    with pytest.raises(AuthorizationError):
        h.guests.activate(proof, h.step_up.mint("start_guest"), session_id="s1")


def test_guest_activation_default_bounds_and_expiry() -> None:
    h = enrolled()
    session = h.activate_guest()
    assert (
        session.expires_at - session.started_at
    ).total_seconds() == GUEST_DEFAULT_MINUTES * 60
    assert h.guests.status().active
    h.clock.advance(minutes=GUEST_DEFAULT_MINUTES - 1)
    assert h.guests.status().active
    h.clock.advance(minutes=2)
    assert not h.guests.status().active
    events = [e.event for e in h.audit.events()]
    assert "guest_mode_started" in events and "guest_mode_expired" in events


@pytest.mark.parametrize(
    "minutes", [0, -5, GUEST_MAX_MINUTES + 1, 10_000, True, 1.5, "20"]
)
def test_guest_duration_is_bounded(minutes: object) -> None:
    h = enrolled()
    with pytest.raises(GuestModeError):
        h.guests.activate(None, None, session_id="s1", minutes=minutes)  # type: ignore[arg-type]


def test_maximum_duration_is_thirty_minutes() -> None:
    h = enrolled()
    session = h.activate_guest(GUEST_MAX_MINUTES)
    assert (session.expires_at - session.started_at).total_seconds() == 30 * 60
    assert GUEST_MAX_MINUTES <= 30


def test_manual_revoke_is_immediate_and_idempotent() -> None:
    h = enrolled()
    h.activate_guest()
    assert h.guests.revoke() is True and not h.guests.status().active
    assert h.guests.revoke() is False


def test_guest_is_classified_only_while_active_and_unknowns_stay_blocked() -> None:
    h = enrolled()
    h.activate_guest()
    d = h.coordinator.decide(h.audio_input(OTHER_VOICE))
    assert d.speaker_class is SpeakerClass.GUEST and d.mode is IdentityMode.GUEST_MODE
    assert d.guest is not None and d.guest.capabilities == {
        GuestCapability.CONVERSATION
    }
    short = h.coordinator.decide(h.audio(OTHER_VOICE, frames=4_000))
    assert (
        short.speaker_class is SpeakerClass.BLOCKED
    )  # unclassifiable speech never becomes a guest
    h.guests.revoke()
    assert (
        h.coordinator.decide(h.audio_input(OTHER_VOICE)).speaker_class
        is SpeakerClass.BLOCKED
    )
    h.clock.advance(hours=1)
    assert (
        h.coordinator.decide(h.audio_input(OTHER_VOICE)).speaker_class
        is SpeakerClass.BLOCKED
    )


def test_guest_cannot_extend_start_another_or_become_owner() -> None:
    h = enrolled()
    h.activate_guest()
    with pytest.raises(GuestModeError):  # no extension / second guest while active
        h.guests.activate(None, None, session_id="s1", minutes=20)
    # A guest speaker can never produce an owner proof (speaker mismatch).
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    assert (
        h.coordinator.prove_owner(
            session_id="s1",
            challenge_id=c.challenge_id,
            audio=h.audio_input(OTHER_VOICE),
        )
        is None
    )
    assert (
        h.coordinator.decide(h.audio_input(OTHER_VOICE)).speaker_class
        is not SpeakerClass.OWNER
    )


def test_guest_context_is_session_local_and_erased() -> None:
    h = enrolled()
    h.activate_guest()
    h.guests.remember_turn("user", "hello")
    h.guests.remember_turn("sam", "hi there")
    assert h.guests.context_size() == 2
    h.guests.revoke()
    assert h.guests.context_size() == 0
    h.activate_guest()
    h.guests.remember_turn("user", "again")
    h.clock.advance(minutes=20)
    assert h.guests.context_size() == 0
    h.guests.remember_turn("user", "late")
    assert h.guests.context_size() == 0  # nothing is kept without a live session


def test_guest_has_only_conversation_and_a_long_deny_list() -> None:
    assert set(GuestCapability) == {GuestCapability.CONVERSATION}
    for item in (
        "personal_memory", "private_knowledge", "gmail", "mcp_tools",
        "coding_agent", "computer_control", "permission_management", "send",
        "publish", "delete", "execute", "approve", "extend_session",
        "enable_another_guest", "reenroll_owner",
    ):  # fmt: skip
        assert item in GUEST_DENIED


def test_guest_principal_has_no_grants_so_every_engine_call_is_denied() -> None:
    h = enrolled()
    guest = h.activate_guest().principal
    engine, _store, _confirmations = permission_engine()
    combos = [
        (PermissionResource.KNOWLEDGE, PermissionAction.READ),
        (PermissionResource.KNOWLEDGE, PermissionAction.WRITE),
        (PermissionResource.KNOWLEDGE, PermissionAction.DELETE),
        (PermissionResource.GMAIL, PermissionAction.SEND),
        (PermissionResource.GMAIL, PermissionAction.READ),
        (PermissionResource.SPEECH_SYNTHESIS, PermissionAction.SEND),
        (PermissionResource.VOICE, PermissionAction.READ),
    ]
    for resource, action in combos:
        decision = engine.evaluate(
            PermissionRequest(
                principal=guest,
                action=action,
                resource=resource,
                scope=PermissionScope.from_path("default"),
            )
        )
        assert decision.outcome.value == "deny", (resource, action)
    assert guest.id.startswith("guest-") and guest.kind is PrincipalKind.USER


# ----------------------------------------- owner voice != authorization


def test_speaker_decision_has_no_permission_fields() -> None:
    names = {f.name for f in fields(SpeakerDecision)}
    assert names == {
        "result",
        "speaker_class",
        "reason_code",
        "mode",
        "audio_digest",
        "guest",
    }
    assert (
        not {
            "allow",
            "permission",
            "approved",
            "authorized",
            "principal",
            "scope",
            "risk",
        }
        & names
    )


def test_verified_owner_voice_is_still_denied_without_a_grant() -> None:
    h = enrolled()
    assert (
        h.coordinator.decide(h.audio_input(OWNER_VOICE)).speaker_class
        is SpeakerClass.OWNER
    )
    engine, _store, _c = permission_engine()
    owner = Principal(kind=PrincipalKind.USER, id="local-user")
    for action in (
        PermissionAction.SEND,
        PermissionAction.DELETE,
        PermissionAction.READ,
    ):
        d = engine.evaluate(
            PermissionRequest(
                principal=owner, action=action, resource=PermissionResource.GMAIL,
                scope=PermissionScope.from_path("acct"),
            )
        )  # fmt: skip
        assert d.outcome.value == "deny"


def test_owner_voice_cannot_create_grants_or_consume_a_confirmation() -> None:
    h = enrolled()
    engine, store, confirmations = permission_engine()
    owner = Principal(kind=PrincipalKind.USER, id="local-user")
    before = list(store.list_grants(owner))
    h.coordinator.decide(h.audio_input(OWNER_VOICE))
    c = h.coordinator.issue_challenge("s1")
    say(h, c)
    h.coordinator.prove_owner(
        session_id="s1", challenge_id=c.challenge_id, audio=h.audio_input(OWNER_VOICE)
    )
    assert list(store.list_grants(owner)) == before  # voice made no grant
    record = confirmations.request(
        PermissionRequest(
            principal=owner,
            action=PermissionAction.DELETE,
            resource=PermissionResource.KNOWLEDGE,
            scope=PermissionScope.from_path("default"),
        ),
        risk=RiskLevel.HIGH,
        now=h.clock(),
    )  # fmt: skip
    h.coordinator.decide(h.audio_input(OWNER_VOICE))
    fresh = confirmations.get(record.confirmation_id)
    assert (
        fresh is not None and fresh.status.value == "pending"
    )  # a voice match never approves it


def test_owner_voice_cannot_satisfy_step_up() -> None:
    h = enrolled()
    d = h.coordinator.decide(h.audio_input(OWNER_VOICE))
    assert d.speaker_class is SpeakerClass.OWNER
    with pytest.raises(
        AuthorizationError
    ):  # no StepUpGrant exists just because the voice matched
        h.enrollment.begin(None, re_enroll=True)
    with pytest.raises(AuthorizationError):
        h.enrollment.delete_profile(None)
