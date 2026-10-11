"""Reuse the first encoding for bounded speech, using faster-whisper 1.2.1.

The pinned library exposes ``generate_segments(..., encoder_output=...)``.
We use that entry point to share the first encoding with language detection,
while retaining its decoder, token suppression, and no-speech checks. No
monkeypatching, cross-request feature cache, extra decoder, or remote call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from sam.language.catalog import SUPPORTED_LANGUAGE_CODES
from sam.voice_identity.errors import ModelSetupError

SUPPORTED_FASTER_WHISPER_VERSION = "1.2.1"


def _components() -> tuple[Any, Any, Any, Any]:
    from faster_whisper import __version__
    from faster_whisper.audio import pad_or_trim
    from faster_whisper.tokenizer import Tokenizer
    from faster_whisper.transcribe import TranscriptionOptions, get_suppressed_tokens

    if __version__ != SUPPORTED_FASTER_WHISPER_VERSION:
        raise ModelSetupError("install the pinned voice-local dependency group")
    return Tokenizer, TranscriptionOptions, get_suppressed_tokens, pad_or_trim


def decode_bounded_audio(
    whisper: Any,
    audio: Any,
    *,
    language_hint: str | None,
    language_for: Callable[[object], tuple[str, float]],
    deadline: float,
) -> dict[str, object]:
    """Decode a complete mono 16 kHz clip of at most 60 seconds.

    Clips up to 30 seconds need one encoding. Longer clips let the pinned
    upstream generator encode the next window and consume its entire tail.
    """

    tokenizer_type, options_type, suppressed_tokens, pad_or_trim = _components()
    if (
        audio.ndim != 1
        or not 0 < audio.size <= 60 * 16_000
        or (language_hint is not None and language_hint not in SUPPORTED_LANGUAGE_CODES)
    ):
        raise ValueError("interactive speech audio is invalid")
    features: Any = None
    window: Any = None
    segments: Any = None
    try:
        if time.monotonic() > deadline:
            raise TimeoutError
        features = whisper.feature_extractor(audio)
        content_frames = features.shape[-1] - 1
        maximum_frames = whisper.feature_extractor.nb_max_frames
        if maximum_frames != 3_000 or not 0 < content_frames <= 2 * maximum_frames:
            raise ValueError("interactive speech window is invalid")
        # Match generate_segments' first window exactly, including its final
        # feature-frame exclusion. Later windows are encoded by the generator.
        first_frames = min(content_frames, maximum_frames)
        window = pad_or_trim(features[:, :first_frames], length=maximum_frames)
        encoded = whisper.encode(window)
        language = language_hint
        confidence: float | None = None
        if language is None:
            batches = whisper.model.detect_language(encoded)
            if not isinstance(batches, list | tuple) or len(batches) != 1:
                raise ValueError("speech language could not be determined")
            detected = batches[0]
            if not isinstance(detected, list | tuple):
                raise ValueError("speech language could not be determined")
            probabilities = []
            for entry in detected:
                if not isinstance(entry, list | tuple) or len(entry) != 2:
                    raise ValueError("speech language could not be determined")
                token, probability = entry
                if not isinstance(token, str) or not (
                    token.startswith("<|") and token.endswith("|>")
                ):
                    raise ValueError("speech language could not be determined")
                probabilities.append((token[2:-2], probability))
            language, confidence = language_for(probabilities)
        if time.monotonic() > deadline:
            raise TimeoutError
        tokenizer = tokenizer_type(
            whisper.hf_tokenizer,
            whisper.model.is_multilingual,
            task="transcribe",
            language=language,
        )
        options = options_type(
            beam_size=1,
            best_of=1,
            patience=1,
            length_penalty=1,
            repetition_penalty=1,
            no_repeat_ngram_size=0,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
            prompt_reset_on_temperature=0.5,
            temperatures=[0.0],
            initial_prompt=None,
            prefix=None,
            suppress_blank=True,
            suppress_tokens=suppressed_tokens(tokenizer, [-1]),
            # Sam consumes only text. Timestamp decoding can split a short
            # clip into more windows and re-encode an already heard tail.
            without_timestamps=True,
            max_initial_timestamp=1.0,
            word_timestamps=False,
            prepend_punctuations="\"'“¿([{-",
            append_punctuations="\"'.。,，!！?？:：”)]}、",
            multilingual=False,
            max_new_tokens=None,
            clip_timestamps="0",
            hallucination_silence_threshold=None,
            hotwords=None,
        )
        segments = whisper.generate_segments(
            features, tokenizer, options, log_progress=False, encoder_output=encoded
        )
        parts: list[str] = []
        for segment in segments:
            if time.monotonic() > deadline:
                raise TimeoutError
            parts.append(segment.text)
        if time.monotonic() > deadline:
            raise TimeoutError
        return {
            "text": "".join(parts).strip(),
            "language": language,
            "confidence": confidence,
        }
    finally:
        try:
            close = getattr(segments, "close", None)
            if callable(close):
                close()
        finally:
            if features is not None:
                features.fill(0)
            if window is not None:
                window.fill(0)


__all__ = ["decode_bounded_audio"]
