"""Pinned, local WeSpeaker ResNet34-LM embeddings with the author's frontend.

Only the verified ONNX graph is loaded. Sam supplies 80-bin Hamming-window
Kaldi filterbanks (25 ms / 10 ms, integer PCM scale) and temporal mean
subtraction, matching wespeaker/cli/speaker.py. No repository Python or
configuration is executed. Scores remain raw cosine in the identity layer;
the upstream CLI's display conversion (cosine + 1) / 2 is not applied.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sam.voice.models import ValidatedAudio
from sam.voice_identity.errors import InsufficientSpeechError, ModelSetupError
from sam.voice_identity.policy import MIN_VERIFICATION_SECONDS
from sam.voice_local.audio import to_16k_numpy
from sam.voice_local.registry import (
    WESPEAKER_MODEL,
    default_model_root,
    model_dir,
    require_verified,
)
from sam.voice_local.speaker_audio import (
    SPEAKER_PREPROCESSING_REVISION,
    prepare_speaker_waveform,
)

WESPEAKER_FEATURE_REVISION = "kaldi80-hamming-cmn-v1"


class WeSpeakerEmbeddingProvider:
    provider_id = "wespeaker-resnet34-lm"
    model_id = WESPEAKER_MODEL.repo_id
    model_revision = (
        f"{WESPEAKER_MODEL.revision}:{WESPEAKER_FEATURE_REVISION}:"
        f"{SPEAKER_PREPROCESSING_REVISION}"
    )
    dimension = 256

    def __init__(self, model_root: Path | None = None) -> None:
        self._dir = model_dir(model_root or default_model_root(), WESPEAKER_MODEL)
        self._lock = threading.Lock()
        self._session: Any = None
        self.load_seconds: float | None = None

    def _load(self) -> Any:
        with self._lock:
            if self._session is not None:
                return self._session
            require_verified(self._dir, WESPEAKER_MODEL)
            started = time.monotonic()
            try:
                import onnxruntime as ort
                import torch
                from torchaudio.compliance.kaldi import fbank  # noqa: F401

                torch.set_num_threads(2)
                ort.disable_telemetry_events()
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                options.enable_cpu_mem_arena = False
                options.enable_mem_pattern = False
                # Do not retain spinning worker threads while Sam is idle.
                options.add_session_config_entry("session.intra_op.allow_spinning", "0")
                options.add_session_config_entry("session.inter_op.allow_spinning", "0")
                session = ort.InferenceSession(
                    str(self._dir / WESPEAKER_MODEL.files[0].name),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
                inputs, outputs = session.get_inputs(), session.get_outputs()
                if (
                    len(inputs) != 1
                    or inputs[0].name != "feats"
                    or inputs[0].type != "tensor(float)"
                    or len(inputs[0].shape) != 3
                    or inputs[0].shape[-1] != 80
                    or len(outputs) != 1
                    or outputs[0].name != "embs"
                    or outputs[0].type != "tensor(float)"
                    or len(outputs[0].shape) != 2
                    or outputs[0].shape[-1] != self.dimension
                ):
                    raise ModelSetupError(
                        "local speaker model has an invalid interface"
                    )
            except ImportError:
                raise ModelSetupError(
                    "install the pinned voice-local dependency group"
                ) from None
            except Exception:
                raise ModelSetupError(
                    "local speaker model could not be loaded"
                ) from None
            self._session = session
            self.load_seconds = time.monotonic() - started
            return session

    def preload(self) -> None:
        """Load local weights only; failures never create an owner decision."""

        with contextlib.suppress(Exception):
            self._load()

    def embed(
        self, audio: ValidatedAudio, *, timeout_seconds: float
    ) -> Sequence[float]:
        return self.embed_speech(
            audio,
            minimum_speech_seconds=MIN_VERIFICATION_SECONDS,
            timeout_seconds=timeout_seconds,
        )

    def embed_speech(
        self,
        audio: ValidatedAudio,
        *,
        minimum_speech_seconds: float,
        timeout_seconds: float,
    ) -> Sequence[float]:
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("speaker time budget is invalid")
        deadline = time.monotonic() + timeout_seconds
        original = to_16k_numpy(audio)
        prepared = waveform = features = feature_mean = output = None
        try:
            prepared = prepare_speaker_waveform(
                original, minimum_speech_seconds=minimum_speech_seconds
            )
            self._check_deadline(deadline)
            session = self._load()
            self._check_deadline(deadline)
            import numpy as np
            import torch
            from torchaudio.compliance.kaldi import fbank

            with torch.inference_mode():
                # Normalized PCM is converted back to the scale used by the
                # author's torchaudio.load(normalize=False) frontend.
                waveform = torch.from_numpy(prepared).unsqueeze(0) * 32768
                features = fbank(
                    waveform,
                    num_mel_bins=80,
                    frame_length=25,
                    frame_shift=10,
                    sample_frequency=16000,
                    window_type="hamming",
                    dither=0.0,
                    snip_edges=True,
                )
                feature_mean = features.mean(dim=0)
                features -= feature_mean
                self._check_deadline(deadline)
                output = session.run(
                    ["embs"], {"feats": features.unsqueeze(0).numpy()}
                )[0]
                self._check_deadline(deadline)
                if (
                    not isinstance(output, np.ndarray)
                    or output.shape != (1, self.dimension)
                    or output.dtype != np.float32
                    or not np.isfinite(output).all()
                ):
                    raise ModelSetupError("local speaker output is invalid")
                norm = float(np.linalg.norm(output))
                if not math.isfinite(norm) or norm <= 0.0:
                    raise ModelSetupError("local speaker output is invalid")
                return [float(value) for value in output[0]]
        except (InsufficientSpeechError, ModelSetupError, TimeoutError):
            raise
        except Exception:
            raise ModelSetupError("local speaker inference failed") from None
        finally:
            original.fill(0)
            if prepared is not None:
                prepared.fill(0)
            # Tensor buffers were created under inference_mode and must also
            # be cleared in that mode, including all failure/deadline paths.
            if waveform is not None or features is not None:
                import torch

                with torch.inference_mode():
                    for tensor in (waveform, features, feature_mean):
                        if tensor is not None:
                            tensor.zero_()
            if output is not None:
                with contextlib.suppress(AttributeError, TypeError, ValueError):
                    output.fill(0)

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        # Each inference is bounded by validated <=60s audio and a CPU-only
        # graph. An over-budget call never returns an identity embedding.
        if time.monotonic() > deadline:
            raise TimeoutError("local speaker inference timed out")

    def __repr__(self) -> str:
        return f"WeSpeakerEmbeddingProvider(model={self.model_id!r})"


__all__ = ["WESPEAKER_FEATURE_REVISION", "WeSpeakerEmbeddingProvider"]
