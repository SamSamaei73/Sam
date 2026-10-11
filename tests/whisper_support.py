"""Synthetic waveform/encoder stubs; no model imports or downloads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sam.voice_local import whisper_decode

np = pytest.importorskip("numpy")


def mock_components(monkeypatch: pytest.MonkeyPatch) -> None:
    def tokenizer(_tokenizer: object, _multilingual: bool, **kwargs: Any) -> object:
        return SimpleNamespace(**kwargs)

    def pad(features: Any, *, length: int) -> Any:
        return np.pad(features, ((0, 0), (0, length - features.shape[-1])))

    monkeypatch.setattr(
        whisper_decode,
        "_components",
        lambda: (tokenizer, SimpleNamespace, lambda *_: [-2], pad),
    )


class FakeWhisper:
    def __init__(self, probabilities: object = None) -> None:
        self.events: list[str] = []
        self.options: list[Any] = []
        self.encoded_inputs: list[Any] = []
        self.feature_buffers: list[Any] = []
        self.encoded: object = object()
        self.hf_tokenizer = object()

        class Extractor:
            nb_max_frames = 3000
            time_per_frame = 0.01

            def __call__(extractor_self, audio: Any) -> Any:
                self.events.append("features")
                frames = audio.size // 160 + 1
                features = np.arange(80 * frames, dtype=np.float32).reshape(80, frames)
                self.feature_buffers.append(features)
                return features

        def detect(encoded: object) -> list[object]:
            assert encoded is self.encoded
            self.events.append("detect")
            if not isinstance(probabilities, list):
                return [probabilities]
            return [
                [(f"<|{code}|>", probability) for code, probability in probabilities]
            ]

        self.feature_extractor = Extractor()
        self.model = SimpleNamespace(is_multilingual=True, detect_language=detect)

    def encode(self, features: Any) -> object:
        self.events.append("encode")
        self.encoded_inputs.append(features.copy())
        return self.encoded

    def generate_segments(
        self,
        features: Any,
        tokenizer: Any,
        options: Any,
        *,
        log_progress: bool,
        encoder_output: object,
    ) -> Any:
        assert encoder_output is self.encoded
        assert log_progress is False
        self.events.append("decode")
        self.options.append(options)
        return iter([SimpleNamespace(text="synthetic transcript")])
