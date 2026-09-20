"""Local Whisper (faster-whisper) transcription for Persian and English.

Implements the Phase 9 ``TranscriptionProvider`` protocol. Offline after
setup: the model is loaded from a hash-verified local directory only
(``local_files_only`` semantics: a directory path, never a Hub id), so no
network access and no runtime download. Only multilingual models from the
trusted allowlist are used. Whisper decodes one language per audio window, so
Persian-English code-switching is handled best-effort (documented).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from sam.voice.models import TranscriptionRequest
from sam.voice_identity.errors import ModelSetupError
from sam.voice_local.audio import to_16k_numpy
from sam.voice_local.registry import (
    default_model_root,
    model_dir,
    require_verified,
    stt_model,
)

SUPPORTED_LANGUAGES = frozenset({"fa", "en"})


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
                str(self._dir), device="cpu", compute_type="int8", local_files_only=True
            )
            self.load_seconds = time.monotonic() - started
            return self._whisper

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
        segments, info = whisper.transcribe(
            to_16k_numpy(request.audio),
            language=hint,  # None -> Whisper detects fa/en
            task="transcribe",
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        parts: list[str] = []
        for segment in segments:  # cooperative timeout between segments
            if time.monotonic() > deadline:
                raise TimeoutError
            parts.append(segment.text)
        text = "".join(parts).strip()
        return {
            "text": text or "",
            "language": info.language if info.language in SUPPORTED_LANGUAGES else None,
            "confidence": max(0.0, min(1.0, float(info.language_probability))),
        }

    def __repr__(self) -> str:
        return f"LocalWhisperTranscriptionProvider(model={self.model_size!r})"


__all__ = ["SUPPORTED_LANGUAGES", "LocalWhisperTranscriptionProvider"]
