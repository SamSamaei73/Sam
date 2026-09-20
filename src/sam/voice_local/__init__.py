"""Local (offline) speaker verification and Persian/English speech-to-text.

Heavy ML libraries (torch, speechbrain, faster-whisper) are imported lazily
inside the provider classes, so importing this package needs none of them and
the core test suite never downloads a model. Model files come from a trusted,
pinned registry (``registry``), are acquired only by the explicit ``setup``
step, and are hash-verified before use. Runtime processing makes no network
calls.
"""
