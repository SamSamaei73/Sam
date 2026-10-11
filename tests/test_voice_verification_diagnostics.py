"""Verification logs expose only fixed diagnostic categories and timings."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from unittest.mock import Mock

import pytest

from sam.voice_identity import verification
from sam.voice_identity.models import SpeakerVerification
from sam.voice_identity.policy import ThresholdPolicy
from tests.voice_identity_support import OWNER_VOICE, Harness


class FixedScore:
    """Synthetic comparison output, unrelated to any real person's voice."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def score(self, template: Sequence[float], embedding: Sequence[float]) -> float:
        self.calls += 1
        return self.value


@pytest.mark.parametrize(
    ("value", "result"),
    [
        (0.45, "owner_not_verified"),
        (0.50, "owner_verified"),
    ],
)
def test_diagnostics_never_change_policy_or_repeat_model_or_profile_work(
    value: float,
    result: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    harness.enroll_owner()
    template = harness.store.load()
    audio = harness.audio(OWNER_VOICE)
    policy = ThresholdPolicy(0.50)
    scorer = FixedScore(value)
    verifier = verification.SpeakerVerifier(
        embedder=harness.embedder,
        store=harness.store,
        policy=policy,
        scorer=scorer,
    )
    load = Mock(wraps=harness.store.load)
    save = Mock(side_effect=AssertionError("verification must not write a profile"))
    delete = Mock(side_effect=AssertionError("verification must not delete a profile"))
    monkeypatch.setattr(harness.store, "load", load)
    monkeypatch.setattr(harness.store, "save", save)
    monkeypatch.setattr(harness.store, "delete", delete)
    before = harness.embedder.call_count
    with caplog.at_level(logging.INFO, logger=verification.__name__):
        actual = verifier.verify(audio)
    assert actual.result == result
    assert policy.threshold == 0.50
    assert harness.embedder.call_count == before + 1
    assert scorer.calls == load.call_count == 1
    assert not save.called and not delete.called
    assert harness.store.load() is template
    records = [
        record for record in caplog.records if record.name == verification.__name__
    ]
    assert len(records) == 1
    assert f"result={result}" in records[0].getMessage()
    assert records[0].exc_info is None
    assert str(value) not in caplog.text
    assert str(OWNER_VOICE) not in caplog.text
    assert audio.metadata.digest_sha256 not in caplog.text
    assert set(vars(actual)) == {"result", "reason_code", "audio_digest"}
    assert "score" not in vars(verifier)


def test_verification_log_excludes_scores_vectors_digests_and_audio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness()
    harness.enroll_owner()
    audio = harness.audio(OWNER_VOICE)
    with caplog.at_level(logging.INFO, logger=verification.__name__):
        result = harness.verifier.verify(audio)
    assert result.result == "owner_verified"
    records = [r for r in caplog.records if r.name == verification.__name__]
    assert len(records) == 1
    assert re.fullmatch(
        r"Speaker verification result=owner_verified reason=speaker_match "
        r"duration_ms=2500 elapsed_ms=\d+",
        records[0].getMessage(),
    )
    assert audio.metadata.digest_sha256 not in caplog.text
    assert str(OWNER_VOICE) not in caplog.text
    assert "score" not in caplog.text and "threshold" not in caplog.text
    assert records[0].exc_info is None
    assert set(vars(result)) == {"result", "reason_code", "audio_digest"}


def test_short_recording_diagnostic_reports_duration_without_running_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness()
    harness.verifier = verification.SpeakerVerifier(
        embedder=harness.embedder, store=harness.store
    )
    audio = harness.audio(OWNER_VOICE, frames=3_984)
    with caplog.at_level(logging.INFO, logger=verification.__name__):
        result = harness.verifier.verify(audio)
    assert result.result == "insufficient_audio"
    assert "reason=audio_too_short duration_ms=249" in caplog.text
    assert harness.embedder.call_count == 0


def test_diagnostic_tags_are_allowlisted_even_for_an_unexpected_result(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness()
    harness.verifier = verification.SpeakerVerifier(
        embedder=harness.embedder, store=harness.store
    )
    audio = harness.audio(OWNER_VOICE)
    unexpected = SpeakerVerification(
        "private result", "private reason", "private digest"
    )
    monkeypatch.setattr(harness.verifier, "_verify", lambda _: (unexpected, 0.81234))
    with caplog.at_level(logging.INFO, logger=verification.__name__):
        result = harness.verifier.verify(audio)
    assert result is unexpected
    assert "result=verification_error reason=unknown duration_ms=2500" in caplog.text
    assert "private" not in caplog.text and "0.81234" not in caplog.text


def test_logging_failure_cannot_change_a_verification_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("logger failure")

    harness = Harness()
    harness.enroll_owner()
    verifier = verification.SpeakerVerifier(
        embedder=harness.embedder,
        store=harness.store,
        scorer=FixedScore(0.45),
        policy=ThresholdPolicy(0.50),
    )
    monkeypatch.setattr(verification.logger, "info", broken)
    assert verifier.verify(harness.audio(OWNER_VOICE)).result == "owner_not_verified"
