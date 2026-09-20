"""Compose the real local voice stack from trusted settings, or report that it
is not available. Never raises for a missing optional dependency or model: an
unavailable stack simply leaves voice identity 'not configured'."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from sam.core.config import Settings
from sam.voice.transcription import TranscriptionProvider
from sam.voice_identity.providers import SpeakerEmbeddingProvider
from sam.voice_identity.store import MacOSKeychainVoiceProfileStore, VoiceProfileStore
from sam.voice_local.registry import (
    SPEAKER_MODEL,
    default_model_root,
    model_dir,
    stt_model,
    verify_model,
)
from sam.voice_local.speaker import SpeechBrainEcapaEmbeddingProvider
from sam.voice_local.stt import LocalWhisperTranscriptionProvider


@dataclass(frozen=True)
class LocalVoiceStack:
    embedder: SpeakerEmbeddingProvider
    store: VoiceProfileStore
    transcriber: TranscriptionProvider


def local_voice_from_settings(
    settings: Settings, model_root: Path | None = None
) -> LocalVoiceStack | None:
    if not settings.voice_identity_enabled or sys.platform != "darwin":
        return None
    root = model_root or default_model_root()
    stt = stt_model(settings.local_stt_model)
    if not (
        verify_model(model_dir(root, SPEAKER_MODEL), SPEAKER_MODEL)
        and verify_model(model_dir(root, stt), stt)
    ):
        return None  # models are acquired explicitly (sam.voice_local.setup)
    try:
        store = MacOSKeychainVoiceProfileStore()  # no insecure fallback
    except Exception:
        return None
    return LocalVoiceStack(
        embedder=SpeechBrainEcapaEmbeddingProvider(root),
        store=store,
        transcriber=LocalWhisperTranscriptionProvider(settings.local_stt_model, root),
    )


__all__ = ["LocalVoiceStack", "local_voice_from_settings"]
