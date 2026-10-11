"""Local multilingual Whisper (faster-whisper) transcription.

Implements the Phase 9 ``TranscriptionProvider`` protocol. Offline after
setup: the model is loaded from a hash-verified local directory only
(``local_files_only`` semantics: a directory path, never a Hub id), so no
network access and no runtime download. Only multilingual models from the
trusted allowlist are used. Whisper decodes one language per audio window, so
Code-switching within an utterance is handled best-effort.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from pathlib import Path
from typing import Any

from sam.language.catalog import SUPPORTED_LANGUAGE_CODES
from sam.voice.models import TranscriptionRequest
from sam.voice_identity.errors import ModelSetupError
from sam.voice_local.audio import to_16k_numpy
from sam.voice_local.registry import (
    default_model_root,
    model_dir,
    require_verified,
    stt_model,
)
from sam.voice_local.whisper_decode import decode_bounded_audio

SUPPORTED_LANGUAGES = SUPPORTED_LANGUAGE_CODES


def _supported_language(probabilities: object) -> tuple[str, float]:
    """Use the detector's actual winning language before the single decode."""

    if not isinstance(probabilities, list | tuple) or not (
        0 < len(probabilities) <= len(SUPPORTED_LANGUAGES)
    ):
        raise ValueError("speech language could not be determined")
    scores: dict[str, float] = {}
    for entry in probabilities:
        if not isinstance(entry, list | tuple) or len(entry) != 2:
            raise ValueError("speech language could not be determined")
        language, probability = entry
        if (
            not isinstance(language, str)
            or language not in SUPPORTED_LANGUAGES
            or isinstance(probability, bool)
            or not isinstance(probability, int | float)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
            or language in scores
        ):
            raise ValueError("speech language could not be determined")
        scores[language] = float(probability)
    language = max(scores, key=lambda code: scores[code])
    winner = scores[language]
    if winner == 0 or sum(score == winner for score in scores.values()) != 1:
        raise ValueError("speech language could not be determined")
    return language, winner


class LocalWhisperTranscriptionProvider:
    provider_id = "local-whisper"

    def __init__(
        self, model_size: str = "small", model_root: Path | None = None
    ) -> None:
        self._model = stt_model(model_size)
        self.model_size = model_size
        self._dir = model_dir(model_root or default_model_root(), self._model)
        self._lock = threading.Lock()
        self._whisper: Any | None = None
        self.load_seconds: float | None = None

    def _load(self) -> Any:
        with self._lock:
            if self._whisper is not None:
                return self._whisper
            require_verified(self._dir, self._model)
            started = time.monotonic()
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise ModelSetupError(
                    "install the voice-local dependency group"
                ) from None
            self._whisper = WhisperModel(
                str(self._dir),
                device="cpu",
                compute_type="int8",
                cpu_threads=2,
                num_workers=1,
                local_files_only=True,
            )
            self.load_seconds = time.monotonic() - started
            return self._whisper

    def preload(self) -> None:
        """Load the verified local model ahead of the first request (hands-free
        wake latency). Never downloads; a failure is left for the request."""

        with contextlib.suppress(Exception):
            self._load()

    def transcribe(
        self, request: TranscriptionRequest, *, timeout_seconds: float
    ) -> object:
        deadline = time.monotonic() + timeout_seconds
        whisper = self._load()
        hint = (
            request.language_hint
            if request.language_hint in SUPPORTED_LANGUAGES
            else None
        )
        audio = to_16k_numpy(request.audio)
        try:
            return decode_bounded_audio(
                whisper,
                audio,
                language_hint=hint,
                language_for=_supported_language,
                deadline=deadline,
            )
        finally:
            audio.fill(0)

    def __repr__(self) -> str:
        return f"LocalWhisperTranscriptionProvider(model={self.model_size!r})"


__all__ = ["SUPPORTED_LANGUAGES", "LocalWhisperTranscriptionProvider"]
