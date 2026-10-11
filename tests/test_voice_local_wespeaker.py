"""WeSpeaker local loader and fail-closed inference; no model/profile access."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from sam.voice.audio import validate_audio
from sam.voice.models import AudioFormat, AudioInput, ValidatedAudio
from sam.voice_identity.errors import InsufficientSpeechError, ModelSetupError
from sam.voice_local import speaker_audio, speaker_wespeaker
from sam.voice_local.registry import WESPEAKER_MODEL
from sam.voice_local.speaker_wespeaker import WeSpeakerEmbeddingProvider
from tests.voice_support import make_wav

np = pytest.importorskip("numpy")


def clip() -> ValidatedAudio:
    return validate_audio(
        AudioInput(
            content=make_wav(frames=16_000), declared_format=AudioFormat.WAV_PCM16
        )
    )


class Session:
    def __init__(self, output: Any = None) -> None:
        self.output = (
            np.arange(1, 257, dtype=np.float32).reshape(1, 256)
            if output is None
            else output
        )
        self.calls = 0

    def get_inputs(self) -> list[Any]:
        return [
            types.SimpleNamespace(
                name="feats", type="tensor(float)", shape=["B", "T", 80]
            )
        ]

    def get_outputs(self) -> list[Any]:
        return [
            types.SimpleNamespace(name="embs", type="tensor(float)", shape=["B", 256])
        ]

    def run(self, names: list[str], values: dict[str, Any]) -> list[Any]:
        self.calls += 1
        assert names == ["embs"] and set(values) == {"feats"}
        assert values["feats"].ndim == 3 and values["feats"].shape[-1] == 80
        return [self.output]


@pytest.fixture
def inference(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    pytest.importorskip("torch")
    pytest.importorskip("torchaudio")
    provider = WeSpeakerEmbeddingProvider()
    source = np.linspace(-0.2, 0.2, 16_000, dtype=np.float32)
    prepared = source.copy()
    session = Session()
    monkeypatch.setattr(speaker_wespeaker, "to_16k_numpy", lambda _audio: source)
    monkeypatch.setattr(
        speaker_wespeaker, "prepare_speaker_waveform", lambda *_a, **_kw: prepared
    )
    monkeypatch.setattr(provider, "_load", lambda: session)
    return provider, source, prepared, session


def test_provider_returns_raw_embedding_and_clears_private_buffers(
    inference: tuple[Any, ...],
) -> None:
    provider, source, prepared, session = inference
    vector = provider.embed(clip(), timeout_seconds=5)
    assert vector == [float(i) for i in range(1, 257)]
    assert session.calls == 1
    assert not source.any() and not prepared.any() and not session.output.any()


@pytest.mark.parametrize(
    "bad",
    [
        np.ones((256,), dtype=np.float32),
        np.ones((1, 192), dtype=np.float32),
        np.ones((2, 256), dtype=np.float32),
        np.zeros((1, 256), dtype=np.float32),
        np.full((1, 256), float("nan"), dtype=np.float32),
        np.full((1, 256), float("inf"), dtype=np.float32),
        np.ones((1, 256), dtype=np.float64),
    ],
)
def test_invalid_outputs_fail_closed_and_are_cleared(
    bad: Any, inference: tuple[Any, ...]
) -> None:
    provider, source, prepared, session = inference
    session.output = bad.copy()
    with pytest.raises(ModelSetupError, match="^local speaker output is invalid$"):
        provider.embed(clip(), timeout_seconds=5)
    assert not source.any() and not prepared.any() and not session.output.any()


@pytest.mark.parametrize("stage", [1, 2, 3, 4])
def test_deadline_expiry_never_returns_an_embedding(
    stage: int, inference: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, source, prepared, session = inference
    ticks = iter([0.0, *([1.0] * (stage - 1)), 6.0])
    monkeypatch.setattr(
        speaker_wespeaker, "time", types.SimpleNamespace(monotonic=lambda: next(ticks))
    )
    with pytest.raises(TimeoutError, match="^local speaker inference timed out$"):
        provider.embed(clip(), timeout_seconds=5)
    assert not source.any() and not prepared.any()
    assert session.calls == (1 if stage == 4 else 0)
    if session.calls:
        assert not session.output.any()


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf"), True])
def test_bad_time_budget_is_rejected_without_audio_work(
    timeout: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[bool] = []
    monkeypatch.setattr(
        speaker_wespeaker, "to_16k_numpy", lambda _: touched.append(True)
    )
    with pytest.raises(ValueError, match="^speaker time budget is invalid$"):
        WeSpeakerEmbeddingProvider().embed(clip(), timeout_seconds=timeout)
    assert touched == []


@pytest.mark.parametrize("voiced_samples", [0, 3_999])
def test_too_little_actual_speech_never_loads_model(
    voiced_samples: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = np.ones(16_000, dtype=np.float32)
    regions = [{"start": 0, "end": voiced_samples}] if voiced_samples else []
    monkeypatch.setattr(speaker_wespeaker, "to_16k_numpy", lambda _: source)
    monkeypatch.setattr(speaker_audio, "_speech_regions", lambda _: regions)
    provider = WeSpeakerEmbeddingProvider()
    calls: list[bool] = []
    monkeypatch.setattr(provider, "_load", lambda: calls.append(True))
    with pytest.raises(InsufficientSpeechError):
        provider.embed(clip(), timeout_seconds=5)
    assert calls == [] and not source.any()


def test_missing_or_modified_weights_fail_before_runtime_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = WeSpeakerEmbeddingProvider(tmp_path)
    called: list[bool] = []
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        types.SimpleNamespace(InferenceSession=lambda *_a, **_kw: called.append(True)),
    )
    provider.preload()
    assert provider._session is None
    with pytest.raises(ModelSetupError, match="failed verification"):
        provider._load()
    provider._dir.mkdir(parents=True)
    (provider._dir / WESPEAKER_MODEL.files[0].name).write_bytes(b"tampered")
    with pytest.raises(ModelSetupError, match="failed verification"):
        provider._load()
    assert called == [] and provider._session is None


def test_loader_is_lazy_cpu_bounded_and_cached_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("torch")
    pytest.importorskip("torchaudio")
    provider = WeSpeakerEmbeddingProvider(tmp_path)
    session = Session()
    order: list[str] = []
    entries: dict[str, str] = {}
    options = types.SimpleNamespace(add_session_config_entry=entries.__setitem__)

    def verified(path: Path, model: object) -> Path:
        order.append("verify")
        assert path == provider._dir and model is WESPEAKER_MODEL
        return path

    def create(path: str, **kwargs: Any) -> Session:
        order.append("load")
        assert path == str(provider._dir / WESPEAKER_MODEL.files[0].name)
        assert kwargs == {
            "sess_options": options,
            "providers": ["CPUExecutionProvider"],
        }
        return session

    monkeypatch.setattr(speaker_wespeaker, "require_verified", verified)
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        types.SimpleNamespace(
            disable_telemetry_events=lambda: order.append("disable_telemetry"),
            SessionOptions=lambda: options,
            ExecutionMode=types.SimpleNamespace(ORT_SEQUENTIAL="sequential"),
            InferenceSession=create,
        ),
    )
    assert order == [] and provider._session is None
    assert provider._load() is session
    assert provider._load() is session
    assert order == ["verify", "disable_telemetry", "load"]
    assert options.intra_op_num_threads == 2 and options.inter_op_num_threads == 1
    assert options.execution_mode == "sequential"
    assert options.enable_cpu_mem_arena is False and options.enable_mem_pattern is False
    assert entries == {
        "session.intra_op.allow_spinning": "0",
        "session.inter_op.allow_spinning": "0",
    }
