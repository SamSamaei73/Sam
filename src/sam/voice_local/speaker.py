"""Local ECAPA-TDNN speaker embeddings (SpeechBrain architecture, Sam loader).

Sam builds the network itself with the parameters published in the model's
hyperparams.yaml and loads ONLY ``embedding_model.ckpt`` after SHA-256
verification, with ``torch.load(weights_only=True)`` (no pickle code
execution). It never calls SpeechBrain's ``from_hparams``/pretrainer, never
evaluates the repo's YAML (``!new:`` = arbitrary object construction), never
loads ``custom.py``, and never touches the network.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sam.voice.models import ValidatedAudio
from sam.voice_identity.errors import ModelSetupError
from sam.voice_local.audio import to_16k_numpy
from sam.voice_local.registry import (
    SPEAKER_MODEL,
    default_model_root,
    model_dir,
    require_verified,
)


class SpeechBrainEcapaEmbeddingProvider:
    provider_id = "speechbrain-ecapa"
    model_id = SPEAKER_MODEL.repo_id
    model_revision = SPEAKER_MODEL.revision
    dimension = 192

    def __init__(self, model_root: Path | None = None) -> None:
        self._dir = model_dir(model_root or default_model_root(), SPEAKER_MODEL)
        self._lock = threading.Lock()
        self._modules: tuple[Any, Any, Any] | None = None  # instance state, no global
        self.load_seconds: float | None = None

    def _load(self) -> tuple[Any, Any, Any]:
        with self._lock:
            if self._modules is not None:
                return self._modules
            require_verified(self._dir, SPEAKER_MODEL)
            started = time.monotonic()
            try:
                import torch
                from speechbrain.lobes.features import Fbank
                from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
                from speechbrain.processing.features import InputNormalization
            except ImportError:
                raise ModelSetupError(
                    "install the voice-local dependency group"
                ) from None
            features = Fbank(n_mels=80)
            norm = InputNormalization(norm_type="sentence", std_norm=False)
            model = ECAPA_TDNN(
                input_size=80,
                channels=[1024, 1024, 1024, 1024, 3072],
                kernel_sizes=[5, 3, 3, 3, 1],
                dilations=[1, 2, 3, 4, 1],
                attention_channels=128,
                lin_neurons=192,
            )
            state = torch.load(
                self._dir / "embedding_model.ckpt",
                map_location="cpu",
                weights_only=True,
            )
            model.load_state_dict(state)
            for module in (features, norm, model):
                module.eval()
            self._modules = (features, norm, model)
            self.load_seconds = time.monotonic() - started
            return self._modules

    def embed(
        self, audio: ValidatedAudio, *, timeout_seconds: float
    ) -> Sequence[float]:
        import torch

        features, norm, model = self._load()
        wav = torch.from_numpy(to_16k_numpy(audio)).unsqueeze(0)
        lengths = torch.ones(1)
        with torch.no_grad():
            feats = features(wav)
            feats = norm(feats, lengths)
            embedding = model(feats, lengths)
        return [float(x) for x in embedding.squeeze().tolist()]

    def __repr__(self) -> str:
        return f"SpeechBrainEcapaEmbeddingProvider(model={self.model_id!r})"


__all__ = ["SpeechBrainEcapaEmbeddingProvider"]
