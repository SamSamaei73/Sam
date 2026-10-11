"""Independent WeSpeaker frontend/lifecycle review; no model or owner data.

The feature oracle follows the upstream CLI on integer PCM directly. Real
torchaudio runs when the optional local-voice dependencies are installed;
inference and the secure store remain synthetic in every test.
"""

from __future__ import annotations

from typing import Any

import pytest

from sam.voice.audio import validate_audio
from sam.voice.models import AudioFormat, AudioInput, ValidatedAudio
from sam.voice_identity.audit import InMemoryVoiceIdentityAuditSink
from sam.voice_identity.authority import StepUpAuthority
from sam.voice_identity.enrollment import EnrollmentService
from sam.voice_identity.errors import AuthorizationError, ModelSetupError
from sam.voice_identity.models import OwnerTemplate, utc_now
from sam.voice_identity.store import InMemoryVoiceProfileStore
from sam.voice_identity.verification import SpeakerVerifier
from sam.voice_local import speaker_wespeaker as subject
from sam.voice_local.audio import to_16k_numpy
from sam.voice_local.speaker import SpeechBrainEcapaEmbeddingProvider
from tests.voice_support import make_wav


@pytest.fixture
def frontend() -> tuple[Any, Any, Any]:
    np = pytest.importorskip("numpy")
    torch = pytest.importorskip("torch")
    kaldi = pytest.importorskip("torchaudio.compliance.kaldi")
    torch.set_num_threads(2)
    return np, torch, kaldi


def audio_from_pcm(pcm: bytes) -> ValidatedAudio:
    return validate_audio(
        AudioInput(content=make_wav(pcm=pcm), declared_format=AudioFormat.WAV_PCM16)
    )


def signal(frontend: tuple[Any, Any, Any]) -> tuple[ValidatedAudio, Any]:
    np, _torch, _kaldi = frontend
    times = np.arange(14400) / 16000
    sweep = np.sin(2 * np.pi * (210 * times + 690 * times**2))
    modulation = np.sin(2 * np.pi * 31 * times)
    samples = (sweep * (6500 + 4000 * modulation)).astype("<i2")
    samples[:1600] = 0
    samples[6900:7300] = 0
    return audio_from_pcm(samples.tobytes()), samples


def prepared_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Bypass VAD only: this test isolates features, not speech classification."""
    buffers: dict[str, Any] = {}
    convert = to_16k_numpy

    def capture_source(audio: ValidatedAudio) -> Any:
        buffers["source"] = convert(audio)
        return buffers["source"]

    def prepare(source: Any, *, minimum_speech_seconds: float) -> Any:
        assert minimum_speech_seconds == 0.25
        buffers["prepared"] = source.copy()
        return buffers["prepared"]

    monkeypatch.setattr(subject, "to_16k_numpy", capture_source)
    monkeypatch.setattr(subject, "prepare_speaker_waveform", prepare)
    return buffers


def test_features_match_author_cli_integer_pcm_and_return_survives_wipe(
    frontend: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    np, torch, kaldi = frontend
    audio, pcm = signal(frontend)
    with torch.inference_mode():
        # Author's CLI loads unnormalized integer PCM and uses these defaults.
        expected = kaldi.fbank(
            torch.from_numpy(pcm.astype(np.float32)).unsqueeze(0),
            num_mel_bins=80,
            frame_length=25,
            frame_shift=10,
            sample_frequency=16000,
            window_type="hamming",
        )
        expected = (expected - expected.mean(dim=0)).unsqueeze(0).numpy()

    buffers = prepared_copy(monkeypatch)
    output = np.arange(1, 257, dtype=np.float32)[None, :]

    class Session:
        def run(self, names: list[str], feeds: dict[str, Any]) -> list[Any]:
            assert names == ["embs"] and set(feeds) == {"feats"}
            buffers["features"] = feeds["feats"]
            np.testing.assert_allclose(feeds["feats"], expected, rtol=0, atol=1e-6)
            assert feeds["feats"].shape == (1, 88, 80)
            return [output]

    provider = subject.WeSpeakerEmbeddingProvider()
    monkeypatch.setattr(provider, "_load", lambda: Session())
    result = provider.embed(audio, timeout_seconds=5)
    assert result == list(range(1, 257))
    assert all(not np.any(buffer) for buffer in buffers.values())
    assert not np.any(output)
    assert audio.pcm == pcm.tobytes()  # caller-owned PCM was not mutated


def test_real_frontend_buffers_are_wiped_when_onnx_raises(
    frontend: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    np, _torch, kaldi = frontend
    audio, pcm = signal(frontend)
    buffers = prepared_copy(monkeypatch)
    compute_features = kaldi.fbank

    def capture_waveform(waveform: Any, **kwargs: Any) -> Any:
        buffers["waveform"] = waveform.numpy()
        return compute_features(waveform, **kwargs)

    class Session:
        def run(self, _names: list[str], feeds: dict[str, Any]) -> list[Any]:
            buffers["features"] = feeds["feats"]
            assert np.any(feeds["feats"])
            raise RuntimeError("private model failure detail")

    provider = subject.WeSpeakerEmbeddingProvider()
    monkeypatch.setattr(kaldi, "fbank", capture_waveform)
    monkeypatch.setattr(provider, "_load", lambda: Session())
    with pytest.raises(
        ModelSetupError, match="local speaker inference failed"
    ) as error:
        provider.embed(audio, timeout_seconds=5)
    assert "private" not in str(error.value)
    assert all(not np.any(buffer) for buffer in buffers.values())
    assert audio.pcm == pcm.tobytes()


def test_selecting_wespeaker_preserves_and_blocks_existing_ecapa_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = SpeechBrainEcapaEmbeddingProvider()
    original = OwnerTemplate(
        vector=(1.0,) + (0.0,) * (legacy.dimension - 1),
        model_id=legacy.model_id,
        model_revision=legacy.model_revision,
        sample_count=3,
        created_at=utc_now(),
    )
    store = InMemoryVoiceProfileStore(original)
    provider = subject.WeSpeakerEmbeddingProvider()
    monkeypatch.setattr(
        provider, "_load", lambda: pytest.fail("incompatible profile ran inference")
    )
    enrollment = EnrollmentService(
        embedder=provider,
        store=store,
        step_up=StepUpAuthority(),
        audit=InMemoryVoiceIdentityAuditSink(),
    )
    status = enrollment.status()
    assert status.enrolled and status.profile_compatible is False
    audio = audio_from_pcm(b"\xe8\x03" * 8000)
    result = SpeakerVerifier(embedder=provider, store=store).verify(audio)
    assert result.reason_code == "model_mismatch"
    with pytest.raises(AuthorizationError):
        enrollment.begin(None, re_enroll=True)
    assert store.load() is original
    # Selecting the old model again recovers compatibility without migrating
    # or deleting the stored data. Only explicit re-enrollment may replace it.
    enrollment._embedder = legacy
    assert enrollment.status().profile_compatible is True
    assert store.load() is original


def test_each_turn_requires_new_inference_and_raw_cosine_match(
    frontend: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    np, _torch, _kaldi = frontend
    audio, _pcm = signal(frontend)
    prepared_copy(monkeypatch)
    provider = subject.WeSpeakerEmbeddingProvider()
    template = OwnerTemplate(
        vector=(1.0,) + (0.0,) * 255,
        model_id=provider.model_id,
        model_revision=provider.model_revision,
        sample_count=3,
        created_at=utc_now(),
    )

    class Session:
        calls = 0

        def run(self, _names: list[str], _feeds: dict[str, Any]) -> list[Any]:
            self.calls += 1
            vector = np.zeros((1, 256), dtype=np.float32)
            cosine = 0.8 if self.calls == 1 else 0.3
            vector[0, 0] = cosine
            vector[0, 1] = np.sqrt(1 - cosine**2)
            return [vector]

    session = Session()
    monkeypatch.setattr(provider, "_load", lambda: session)
    verifier = SpeakerVerifier(
        embedder=provider, store=InMemoryVoiceProfileStore(template)
    )
    assert verifier.verify(audio).result == "owner_verified"
    # .3 raw cosine must reject even though the upstream display score is .65.
    assert verifier.verify(audio).reason_code == "speaker_mismatch"
    assert session.calls == 2
