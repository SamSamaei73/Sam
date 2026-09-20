"""Sam's Secure Voice Synthesis layer (Phase 10), with Fish Audio as the first
provider.

    Fish Audio is a synthesis provider, not an authorization or security
    boundary.

    Secret-containing text is never sent to Fish Audio.

    Voice selection, model selection, endpoint selection, credentials, and
    authorization are controlled by Sam, not by the LLM or Fish Audio.

    Phase 10 does not implement voice cloning.

Explicit, non-streaming REST text-to-speech against a trusted preconfigured
voice profile. No playback, no streaming, no background work. See
``sam.tts.gateway.TTSGateway`` and ``docs/tts.md``.
"""
