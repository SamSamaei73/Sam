"""Sam's Secure Voice Input & Speech Understanding Foundation (Phase 9).

    Voice identity is an authentication signal, not an authorization decision.

    Voice input never bypasses PermissionEngine, confirmation, or stronger
    authentication requirements.

Phase 9 accepts *explicit, bounded* audio (16-bit PCM / WAV only), validates
it structurally, transcribes it through a controlled, untrusted provider
interface, optionally attaches a transient, context-bound identity signal,
and returns a normalized transcript with provenance. It runs against
deterministic fakes only: no real STT, no microphone, no wake word, no
background listening, no biometrics, no persistence, no network. See
``sam.voice.gateway.VoiceGateway`` and ``docs/voice.md``.
"""
