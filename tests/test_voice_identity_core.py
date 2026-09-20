"""Policy, template privacy, enrollment, verification."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import pytest

from sam.voice_identity.audit import ALLOWED_EVENTS
from sam.voice_identity.errors import (
    AuthorizationError,
    EnrollmentError,
    ProfileStoreError,
)
from sam.voice_identity.models import OwnerTemplate
from sam.voice_identity.policy import (
    THRESHOLD_MAX,
    THRESHOLD_MIN,
    SpeakerClass,
    SpeakerResult,
    ThresholdPolicy,
    classify_speaker,
)
from sam.voice_identity.providers import (
    FakeSpeakerEmbeddingProvider,
    cosine_similarity,
    normalize,
)
from sam.voice_identity.store import InMemoryVoiceProfileStore
from tests.voice_identity_support import DIM, OTHER_VOICE, OWNER_VOICE, Harness, jitter
from tests.voice_support import reachable

# ------------------------------------------------------------------ policy


@pytest.mark.parametrize(
    "bad", [0.0, 0.29, 0.91, 1.0, -1, math.nan, math.inf, True, "0.5", None]
)
def test_threshold_is_bounded(bad: object) -> None:
    with pytest.raises(ValueError):
        ThresholdPolicy(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("ok", [THRESHOLD_MIN, 0.5, THRESHOLD_MAX])
def test_threshold_accepts_safe_values(ok: float) -> None:
    assert ThresholdPolicy(ok).threshold == ok


def test_threshold_boundary_is_inclusive_and_nan_safe() -> None:
    policy = ThresholdPolicy(0.5)
    assert policy.accepts(0.5) and policy.accepts(0.9)
    assert not policy.accepts(0.4999999)
    assert not policy.accepts(math.nan) and not policy.accepts(math.inf * 0)


@pytest.mark.parametrize(
    ("result", "guest", "expected"),
    [
        (SpeakerResult.OWNER_VERIFIED, False, SpeakerClass.OWNER),
        (SpeakerResult.OWNER_VERIFIED, True, SpeakerClass.OWNER),
        (SpeakerResult.OWNER_NOT_VERIFIED, False, SpeakerClass.BLOCKED),
        (SpeakerResult.OWNER_NOT_VERIFIED, True, SpeakerClass.GUEST),
        (SpeakerResult.UNKNOWN, True, SpeakerClass.BLOCKED),
        (SpeakerResult.INSUFFICIENT_AUDIO, True, SpeakerClass.BLOCKED),
        (SpeakerResult.VERIFICATION_ERROR, True, SpeakerClass.BLOCKED),
        (SpeakerResult.VERIFICATION_ERROR, False, SpeakerClass.BLOCKED),
        (SpeakerResult.NOT_ENROLLED, False, SpeakerClass.BLOCKED),
        (SpeakerResult.NOT_ENROLLED, True, SpeakerClass.BLOCKED),
    ],
)
def test_classification_is_fail_closed(
    result: SpeakerResult, guest: bool, expected: SpeakerClass
) -> None:
    assert classify_speaker(result, guest_active=guest) is expected


def test_not_enrolled_never_implies_owner() -> None:
    for guest in (False, True):
        assert (
            classify_speaker(SpeakerResult.NOT_ENROLLED, guest_active=guest)
            is SpeakerClass.BLOCKED
        )


def test_classification_has_no_enrollment_opt_out() -> None:
    import inspect

    assert "require_enrollment" not in inspect.signature(classify_speaker).parameters


# ---------------------------------------------------------- template privacy


def _template() -> OwnerTemplate:
    return OwnerTemplate(
        vector=tuple(float(x) for x in OWNER_VOICE),
        model_id="fake-ecapa",
        model_revision="r1",
        sample_count=3,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_template_repr_and_str_never_show_biometrics() -> None:
    template = _template()
    for text in (repr(template), str(template), f"{template}", f"{template!r}"):
        assert "redacted" in text
        assert str(OWNER_VOICE[0])[:6] not in text


def test_template_round_trips_and_validates() -> None:
    template = _template()
    again = OwnerTemplate.from_json(template.to_json())
    assert again.vector == template.vector and again.model_id == "fake-ecapa"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(vector=[0.0] * DIM),
        lambda d: d.update(vector=[math.inf] + [0.0] * (DIM - 1)),
        lambda d: d.update(vector=[1.0]),
        lambda d: d.update(v=99),
        lambda d: d.pop("model_id"),
        lambda d: d.update(sample_count=0),
    ],
)
def test_corrupt_stored_template_is_an_error_not_a_template(mutate) -> None:  # type: ignore[no-untyped-def]
    data = json.loads(_template().to_json())
    mutate(data)
    with pytest.raises(ProfileStoreError):
        OwnerTemplate.from_json(json.dumps(data))
    with pytest.raises(ProfileStoreError):
        OwnerTemplate.from_json("not json")


def test_audit_vocabulary_is_closed_and_content_free() -> None:
    h = Harness()
    with pytest.raises(ValueError):
        h.audit.record("transcript_recorded")
    with pytest.raises(ValueError):
        h.audit.record("owner_voice_verified", "has spaces and Capitals")
    assert not any(
        "audio" in e or "embedding" in e or "template_data" in e for e in ALLOWED_EVENTS
    )


# ---------------------------------------------------------------- enrollment


def test_status_before_and_after_enrollment() -> None:
    h = Harness()
    assert h.enrollment.status().enrolled is False
    h.enroll_owner()
    status = h.enrollment.status()
    assert status.enrolled is True and status.sample_count == 4


def test_enrollment_needs_step_up_and_it_is_one_time_and_purpose_bound() -> None:
    h = Harness()
    with pytest.raises(AuthorizationError):
        h.enrollment.begin(None)
    grant = h.step_up.mint("delete_profile")
    with pytest.raises(AuthorizationError):  # wrong purpose
        h.enrollment.begin(grant)
    grant = h.step_up.mint("enroll")
    h.enrollment.begin(grant)
    with pytest.raises(AuthorizationError):  # replayed
        h.enrollment.begin(grant)


def test_forged_and_stale_step_up_grants_are_refused() -> None:
    from dataclasses import replace

    h = Harness()
    grant = h.step_up.mint("enroll")
    with pytest.raises(AuthorizationError):
        h.enrollment.begin(replace(grant, purpose="re_enroll"))
    grant = h.step_up.mint("enroll")
    h.clock.advance(seconds=61)
    with pytest.raises(AuthorizationError):
        h.enrollment.begin(grant)


def test_too_few_samples() -> None:
    h = Harness()
    session = h.enrollment.begin(h.step_up.mint("enroll"))
    h.enrollment.add_sample(session, h.audio(OWNER_VOICE))
    h.enrollment.add_sample(session, h.audio(OWNER_VOICE))
    outcome = h.enrollment.complete(session)
    assert not outcome.completed and outcome.reason_code == "too_few_samples"
    assert h.enrollment.status().enrolled is False


def test_rejects_short_quiet_and_clipped_audio() -> None:
    h = Harness()
    session = h.enrollment.begin(h.step_up.mint("enroll"))
    short = h.audio(OWNER_VOICE, frames=16_000)  # 1 s
    assert h.enrollment.add_sample(session, short).reason_code == "audio_too_short"
    from sam.voice.audio import validate_audio
    from sam.voice.models import AudioFormat, AudioInput
    from tests.voice_support import make_wav

    quiet = validate_audio(
        AudioInput(
            content=make_wav(pcm=b"\x01\x00" * 40_000),
            declared_format=AudioFormat.WAV_PCM16,
        )
    )
    assert h.enrollment.add_sample(session, quiet).reason_code == "audio_too_quiet"
    loud = validate_audio(
        AudioInput(
            content=make_wav(pcm=b"\xff\x7f" * 40_000),
            declared_format=AudioFormat.WAV_PCM16,
        )
    )
    assert h.enrollment.add_sample(session, loud).reason_code == "audio_clipped"


def test_duplicate_samples_are_rejected() -> None:
    h = Harness()
    session = h.enrollment.begin(h.step_up.mint("enroll"))
    clip = h.audio(OWNER_VOICE)
    assert h.enrollment.add_sample(session, clip).accepted
    assert h.enrollment.add_sample(session, clip).reason_code == "duplicate_sample"
    # A different recording with a (near-)identical embedding is also a duplicate.
    twin = h.audio(OWNER_VOICE)
    h.embedder.register(
        twin.metadata.digest_sha256, h.embedder.embed(clip, timeout_seconds=1)
    )
    assert h.enrollment.add_sample(session, twin).reason_code == "duplicate_sample"


def test_provider_failure_and_garbage_embeddings_are_contained() -> None:
    for bad in (
        FakeSpeakerEmbeddingProvider(
            dimension=DIM, raises=RuntimeError("secret /tmp/x")
        ),
        FakeSpeakerEmbeddingProvider(dimension=DIM, result=lambda a: [math.nan] * DIM),
        FakeSpeakerEmbeddingProvider(dimension=DIM, result=lambda a: [0.1] * 3),
        FakeSpeakerEmbeddingProvider(dimension=DIM, result=lambda a: "nope"),
    ):
        h = Harness(embedder=bad)
        session = h.enrollment.begin(h.step_up.mint("enroll"))
        progress = h.enrollment.add_sample(session, h.audio(OWNER_VOICE))
        assert not progress.accepted and progress.reason_code == "provider_error"


def test_samples_from_two_different_speakers_are_refused() -> None:
    h = Harness()
    session = h.enrollment.begin(h.step_up.mint("enroll"))
    for voice in (OWNER_VOICE, OTHER_VOICE, OWNER_VOICE):
        h.enrollment.add_sample(session, h.audio(voice))
    outcome = h.enrollment.complete(session)
    assert not outcome.completed
    assert outcome.reason_code in {"inconsistent_samples", "unstable_samples"}
    assert h.store.load() is None


def test_store_write_failure_leaves_no_profile() -> None:
    h = Harness()
    h.store.fail_save = True
    session = h.enrollment.begin(h.step_up.mint("enroll"))
    for _ in range(3):
        h.enrollment.add_sample(session, h.audio(OWNER_VOICE))
    outcome = h.enrollment.complete(session)
    assert not outcome.completed and outcome.reason_code == "store_error"
    h.store.fail_save = False
    assert h.store.load() is None


def test_enrolling_twice_needs_re_enrollment() -> None:
    h = Harness()
    h.enroll_owner()
    with pytest.raises(EnrollmentError):
        h.enrollment.begin(h.step_up.mint("enroll"))
    fresh = Harness()
    with pytest.raises(EnrollmentError):
        fresh.enrollment.begin(fresh.step_up.mint("re_enroll"), re_enroll=True)


def test_raw_audio_is_never_retained_by_enrollment() -> None:
    h = Harness()
    session = h.enrollment.begin(h.step_up.mint("enroll"))
    clips = [h.audio(OWNER_VOICE) for _ in range(4)]
    for clip in clips:
        h.enrollment.add_sample(session, clip)
    h.enrollment.complete(session)
    pcms = {c.pcm for c in clips}
    for root in (h.enrollment, h.store, h.audit, h.verifier):
        assert not any(isinstance(o, bytes) and o in pcms for o in reachable(root))
    stored = h.store.load()
    assert stored is not None and stored.sample_count == 4
    # Per-sample embeddings were dropped with the session.
    assert h.enrollment._session is None  # noqa: SLF001


def test_enrollment_session_expires() -> None:
    h = Harness()
    session = h.enrollment.begin(h.step_up.mint("enroll"))
    h.clock.advance(minutes=16)
    with pytest.raises(EnrollmentError):
        h.enrollment.add_sample(session, h.audio(OWNER_VOICE))


def test_a_new_attempt_supersedes_the_old_one() -> None:
    h = Harness()
    first = h.enrollment.begin(h.step_up.mint("enroll"))
    h.enrollment.begin(h.step_up.mint("enroll"))
    with pytest.raises(EnrollmentError):
        h.enrollment.add_sample(first, h.audio(OWNER_VOICE))


def test_delete_profile_needs_step_up_and_removes_the_template() -> None:
    h = Harness()
    h.enroll_owner()
    with pytest.raises(AuthorizationError):
        h.enrollment.delete_profile(None)
    h.enrollment.delete_profile(h.step_up.mint("delete_profile"))
    assert h.store.load() is None and h.enrollment.status().enrolled is False


def test_delete_failure_is_reported_not_swallowed() -> None:
    h = Harness()
    h.enroll_owner()
    h.store.fail_delete = True
    with pytest.raises(EnrollmentError):
        h.enrollment.delete_profile(h.step_up.mint("delete_profile"))
    assert h.store.load() is not None


def test_re_enrollment_invalidates_the_previous_profile() -> None:
    h = Harness()
    h.enroll_owner(OWNER_VOICE)
    old_owner = h.audio(OWNER_VOICE)
    assert h.verifier.verify(old_owner).result == "owner_verified"
    grant = h.step_up.mint("re_enroll")
    session = h.enrollment.begin(grant, re_enroll=True)
    for _ in range(4):
        h.enrollment.add_sample(session, h.audio(OTHER_VOICE))
    assert h.enrollment.complete(session).completed
    assert h.verifier.verify(h.audio(OWNER_VOICE)).result == "owner_not_verified"
    assert h.verifier.verify(h.audio(OTHER_VOICE)).result == "owner_verified"


def test_enrollment_audit_is_content_free() -> None:
    h = Harness()
    h.enroll_owner()
    names = [e.event for e in h.audit.events()]
    assert names == ["owner_enrollment_started", "owner_enrollment_completed"]
    dump = repr(h.audit.events())
    assert not any(f"{x:.4f}"[:6] in dump for x in OWNER_VOICE[:3])


# -------------------------------------------------------------- verification


def test_not_enrolled_result() -> None:
    h = Harness()
    assert h.verifier.verify(h.audio(OWNER_VOICE)).result == "not_enrolled"


def test_owner_accepted_and_other_speaker_rejected() -> None:
    h = Harness()
    h.enroll_owner()
    assert h.verifier.verify(h.audio(OWNER_VOICE)).result == "owner_verified"
    other = h.verifier.verify(h.audio(OTHER_VOICE))
    assert other.result == "owner_not_verified"


def test_verification_output_has_no_score_or_biometrics() -> None:
    h = Harness()
    h.enroll_owner()
    result = h.verifier.verify(h.audio(OWNER_VOICE))
    assert set(vars(result)) == {"result", "reason_code", "audio_digest"}


def test_threshold_boundary_with_a_constructed_similarity() -> None:
    h = Harness(threshold=0.5)
    h.enroll_owner()
    template = h.store.load()
    assert template is not None
    # Build embeddings with an exactly-chosen cosine similarity to the template.
    ortho = normalize(
        [
            b
            - sum(a * b for a, b in zip(template.vector, OTHER_VOICE, strict=True)) * a
            for a, b in zip(template.vector, OTHER_VOICE, strict=True)
        ]
    )

    def at(score: float) -> tuple[float, ...]:
        s = math.sqrt(1 - score * score)
        return tuple(
            score * a + s * o for a, o in zip(template.vector, ortho, strict=True)
        )

    for score, expected in [
        (0.60, "owner_verified"),
        (0.5001, "owner_verified"),
        (0.4999, "owner_not_verified"),
        (0.2, "owner_not_verified"),
    ]:
        clip = h.audio(OWNER_VOICE)
        h.embedder.register(clip.metadata.digest_sha256, at(score))
        got, measured = h.verifier.verify_diagnostic(clip)
        assert measured is not None and abs(measured - score) < 1e-6
        assert got.result == expected, score


def test_short_and_quiet_audio_is_insufficient() -> None:
    h = Harness()
    h.enroll_owner()
    assert (
        h.verifier.verify(h.audio(OWNER_VOICE, frames=8_000)).result
        == "insufficient_audio"
    )


def test_store_read_failure_is_an_error_never_not_enrolled() -> None:
    h = Harness()
    h.enroll_owner()
    h.store.fail_load = True
    result = h.verifier.verify(h.audio(OWNER_VOICE))
    assert (
        result.result == "verification_error"
        and result.reason_code == "profile_unreadable"
    )
    assert h.verifier.is_enrolled() is None


def test_model_or_version_mismatch_fails_closed() -> None:
    h = Harness()
    h.enroll_owner()
    for other in (
        FakeSpeakerEmbeddingProvider(dimension=DIM, model_id="different"),
        FakeSpeakerEmbeddingProvider(dimension=DIM, model_revision="test-2"),
    ):
        h.verifier._embedder = other  # noqa: SLF001
        assert h.verifier.verify(h.audio(OWNER_VOICE)).reason_code == "model_mismatch"


def test_provider_errors_timeouts_and_hostile_output_fail_closed() -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"raises": RuntimeError("boom /Users/x")}, "provider_error"),
        ({"delay_seconds": 60.0}, "provider_error"),
        ({"result": lambda a: [math.nan] * DIM}, "provider_invalid"),
        ({"result": lambda a: {"x": 1}}, "provider_invalid"),
        ({"result": lambda a: [True] * DIM}, "provider_invalid"),
    ]
    for kwargs, reason in cases:
        good = Harness()
        good.enroll_owner()
        h = Harness(
            store=good.store,
            embedder=FakeSpeakerEmbeddingProvider(dimension=DIM, **kwargs),
        )
        out = h.verifier.verify(h.audio(OWNER_VOICE))
        assert out.result == "verification_error" and out.reason_code == reason, kwargs


def test_profile_deletion_returns_to_not_enrolled() -> None:
    h = Harness()
    h.enroll_owner()
    h.enrollment.delete_profile(h.step_up.mint("delete_profile"))
    assert h.verifier.verify(h.audio(OWNER_VOICE)).result == "not_enrolled"


def test_cosine_helpers() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        cosine_similarity([1], [1, 2])
    with pytest.raises(ValueError):
        normalize([0.0, 0.0])
    assert isinstance(jitter(OWNER_VOICE, 1), tuple)
    assert InMemoryVoiceProfileStore().load() is None
