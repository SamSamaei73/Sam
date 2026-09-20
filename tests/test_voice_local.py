"""Trusted model registry, explicit setup, language hint, factory, static
security of the local-voice package. No model is downloaded and no heavy
library is imported by these tests; the real-model check is opt-in."""

from __future__ import annotations

import ast
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import sam.voice_local as voice_local_pkg
from sam.core.config import Settings
from sam.language.hint import HintedTranscriptionProvider, language_hint_scope
from sam.voice.audio import validate_audio
from sam.voice.models import AudioFormat, AudioInput, TranscriptionRequest
from sam.voice.transcription import FakeTranscriptionProvider
from sam.voice_identity.errors import ModelSetupError
from sam.voice_local import registry, setup
from sam.voice_local.factory import local_voice_from_settings
from tests.voice_support import make_wav

ROOT = pathlib.Path(voice_local_pkg.__file__).parent
SOURCES = {p.name: p.read_text() for p in ROOT.glob("*.py")}


def fake_model(tmp_path: Path, content: dict[str, bytes]) -> registry.TrustedModel:
    files = tuple(
        registry.ModelFile(n, hashlib.sha256(b).hexdigest(), len(b))
        for n, b in content.items()
    )
    model = registry.TrustedModel("fake", "org/fake", "a" * 40, "MIT", files)
    directory = registry.model_dir(tmp_path, model)
    directory.mkdir(parents=True)
    for name, data in content.items():
        (directory / name).write_bytes(data)
    return model


# ------------------------------------------------------------------ registry


def test_registry_pins_every_model_to_a_commit_and_a_hash() -> None:
    models = [registry.SPEAKER_MODEL, *registry.STT_MODELS.values()]
    for m in models:
        assert re.fullmatch(r"[0-9a-f]{40}", m.revision), m.key
        assert m.license in {"Apache-2.0", "MIT"}
        for f in m.files:
            assert re.fullmatch(r"[0-9a-f]{64}", f.sha256) and f.size > 0
    assert set(registry.STT_MODELS) == {"small", "medium", "large-v3"}
    assert not any(
        ".en" in m.repo_id for m in registry.STT_MODELS.values()
    )  # multilingual only
    assert registry.SPEAKER_MODEL.repo_id == "speechbrain/spkrec-ecapa-voxceleb"


def test_only_allowlisted_stt_models_can_be_selected() -> None:
    assert registry.stt_model("small").repo_id == "Systran/faster-whisper-small"
    for bad in ("tiny.en", "org/other-model", "../x", "", "large"):
        with pytest.raises(ModelSetupError):
            registry.stt_model(bad)


def test_the_hub_endpoint_is_fixed_not_from_the_environment() -> None:
    assert registry.HF_ENDPOINT == "https://huggingface.co"
    code = _code(SOURCES["registry.py"])
    assert "os.environ" not in code and "getenv" not in code


def test_verification_checks_existence_size_and_hash(tmp_path: Path) -> None:
    model = fake_model(tmp_path, {"a.bin": b"alpha", "b.json": b"{}"})
    directory = registry.model_dir(tmp_path, model)
    assert registry.verify_model(directory, model)
    assert registry.require_verified(directory, model) == directory
    (directory / "a.bin").write_bytes(b"alphb")  # same size, tampered
    assert not registry.verify_model(directory, model)
    with pytest.raises(ModelSetupError):
        registry.require_verified(directory, model)
    (directory / "a.bin").write_bytes(b"alpha")
    (directory / "b.json").unlink()
    assert not registry.verify_model(directory, model)
    assert not registry.verify_model(tmp_path / "missing", model)


# --------------------------------------------------------------------- setup


def test_setup_downloads_only_pinned_files_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"m.bin": b"weights", "c.json": b"{}"}
    files = tuple(
        registry.ModelFile(n, hashlib.sha256(b).hexdigest(), len(b))
        for n, b in payload.items()
    )
    model = registry.TrustedModel("fake", "org/fake", "b" * 40, "MIT", files)
    calls: list[dict[str, Any]] = []

    def fake_download(**kwargs: Any) -> str:
        calls.append(kwargs)
        (Path(kwargs["local_dir"]) / kwargs["filename"]).write_bytes(
            payload[kwargs["filename"]]
        )
        return ""

    import types

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(hf_hub_download=fake_download),
    )
    directory = setup.acquire(model, tmp_path)
    assert registry.verify_model(directory, model)
    assert {c["filename"] for c in calls} == set(payload)
    for c in calls:
        assert c["revision"] == "b" * 40 and c["repo_id"] == "org/fake"
        assert c["endpoint"] == "https://huggingface.co"
    assert not list(directory.parent.glob(".staging-*"))
    assert (
        setup.acquire(model, tmp_path) == directory and len(calls) == 2
    )  # cached: no re-download


def test_setup_rejects_a_tampered_download_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = registry.TrustedModel(
        "fake", "org/fake", "c" * 40, "MIT",
        (registry.ModelFile("m.bin", hashlib.sha256(b"good").hexdigest(), 4),),
    )  # fmt: skip

    def evil_download(**kwargs: Any) -> str:
        (Path(kwargs["local_dir"]) / kwargs["filename"]).write_bytes(b"evil")
        return ""

    import types

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(hf_hub_download=evil_download),
    )
    with pytest.raises(ModelSetupError):
        setup.acquire(model, tmp_path)
    assert not registry.model_dir(tmp_path, model).exists()
    assert not list(registry.model_dir(tmp_path, model).parent.glob(".staging-*"))


def test_setup_is_never_imported_by_runtime_code() -> None:
    src = pathlib.Path(voice_local_pkg.__file__).parents[1]
    for path in src.rglob("*.py"):
        if path.name == "setup.py" and path.parent.name == "voice_local":
            continue
        text = _code(path.read_text())
        assert "voice_local.setup" not in text and "acquire(" not in text, path


# ------------------------------------------------------------- language hint


def _request(hint: str | None = None):  # type: ignore[no-untyped-def]
    audio = validate_audio(
        AudioInput(
            content=make_wav(frames=32000), declared_format=AudioFormat.WAV_PCM16
        )
    )
    return TranscriptionRequest(
        session_id="s", utterance_id="u", audio=audio, language_hint=hint
    )


class _Spy(FakeTranscriptionProvider):
    def __init__(self) -> None:
        super().__init__("ok")
        self.hints: list[str | None] = []

    def transcribe(
        self, request: TranscriptionRequest, *, timeout_seconds: float
    ) -> object:
        self.hints.append(request.language_hint)
        return super().transcribe(request, timeout_seconds=timeout_seconds)


def test_hint_reaches_the_recognizer_only_inside_its_scope() -> None:
    spy = _Spy()
    provider = HintedTranscriptionProvider(spy)
    provider.transcribe(_request(), timeout_seconds=5)
    with language_hint_scope("fa"):
        provider.transcribe(_request(), timeout_seconds=5)
    with language_hint_scope("klingon"):  # unknown hints are dropped
        provider.transcribe(_request(), timeout_seconds=5)
    with language_hint_scope(None):
        provider.transcribe(_request(), timeout_seconds=5)
    provider.transcribe(_request(), timeout_seconds=5)
    assert spy.hints == [None, "fa", None, None, None]
    assert provider.provider_id == spy.provider_id


def test_an_explicit_request_hint_wins_over_the_scope() -> None:
    spy = _Spy()
    with language_hint_scope("fa"):
        HintedTranscriptionProvider(spy).transcribe(_request("en"), timeout_seconds=5)
    assert spy.hints == ["en"]


# -------------------------------------------------------------------- factory


def test_factory_is_off_unless_enabled_and_models_are_present(tmp_path: Path) -> None:
    assert local_voice_from_settings(Settings(), tmp_path) is None
    enabled = Settings(voice_identity_enabled=True)
    assert (
        local_voice_from_settings(enabled, tmp_path) is None
    )  # no models: not configured
    assert (
        local_voice_from_settings(
            Settings(
                voice_identity_enabled=True, desktop_bridge_token=SecretStr("x" * 40)
            ),
            tmp_path,
        )
        is None
    )


def test_settings_bounds_for_local_voice() -> None:
    with pytest.raises(ValueError):
        Settings(speaker_verification_threshold=0.1)
    with pytest.raises(ValueError):
        Settings(speaker_verification_threshold=0.99)
    with pytest.raises(ValueError):
        Settings(local_stt_model="tiny.en")  # type: ignore[arg-type]
    assert (
        Settings().local_stt_model == "small"
        and Settings().voice_identity_enabled is False
    )


# ------------------------------------------------------------ static security


def _code(text: str) -> str:
    """Source without docstrings and comments (prose may mention what's banned)."""

    text = re.sub(r'""".*?"""', "", text, flags=re.S)
    return re.sub(r"(?m)#.*$", "", text)


def test_no_unsafe_loading_or_remote_code_anywhere() -> None:
    joined = "\n".join(_code(t) for t in SOURCES.values())
    for banned in (
        "trust_remote_code", "from_pretrained", "from_hparams", "snapshot_download",
        "pickle", "marshal", "import_module", "subprocess", "os.system",
    ):  # fmt: skip
        assert banned not in joined, banned
    assert not re.search(
        r"(?<![.\w])(eval|exec)\(", joined
    )  # builtins, not Module.eval()
    assert "yaml" not in joined.lower()  # the model repo's HyperPyYAML is never loaded


def test_torch_load_is_weights_only_and_only_in_the_speaker_loader() -> None:
    for name, text in SOURCES.items():
        code = _code(text)
        if "torch.load(" in code:
            assert name == "speaker.py"
            call = code[code.index("torch.load(") :].split(")")[0]
            assert (
                "weights_only=True"
                in code[code.index("torch.load(") : code.index("torch.load(") + 200]
            ), call


def test_hub_downloads_exist_only_in_the_explicit_setup_module() -> None:
    for name, text in SOURCES.items():
        code = _code(text)
        has_hub = "hf_hub_download" in code or "huggingface_hub" in code
        assert has_hub == (name == "setup.py"), name


def test_no_network_or_persistence_in_the_runtime_providers() -> None:
    for name in ("speaker.py", "stt.py", "audio.py", "registry.py", "factory.py"):
        tree = ast.parse(SOURCES[name])
        imported = {
            (n.module or "").split(".")[0]
            if isinstance(n, ast.ImportFrom)
            else a.name.split(".")[0]
            for n in ast.walk(tree)
            for a in (n.names if isinstance(n, ast.Import | ast.ImportFrom) else [])
        }
        assert not imported & {
            "requests",
            "httpx",
            "urllib",
            "socket",
            "http",
            "huggingface_hub",
            "logging",
        }, name
        code = _code(SOURCES[name])
        for banned in (
            "write_bytes(",
            "write_text(",
            "open(",
            "np.save",
            "sf.write",
            "soundfile",
        ):
            assert banned not in code.replace("path.open(", "").replace(
                "Path.open(", ""
            ), (name, banned)


def test_stt_loads_from_a_local_verified_directory_only() -> None:
    code = _code(SOURCES["stt.py"])
    assert "local_files_only=True" in code and "require_verified(" in code
    assert re.search(
        r"WhisperModel\(\s*str\(self\._dir\)", code
    )  # a path, never a Hub id
    assert '"tiny' not in code and ".en" not in code


def test_providers_never_log_or_return_raw_audio_or_embeddings_in_repr() -> None:
    from sam.voice_local.speaker import SpeechBrainEcapaEmbeddingProvider
    from sam.voice_local.stt import LocalWhisperTranscriptionProvider

    text = repr(SpeechBrainEcapaEmbeddingProvider())
    assert "[" not in text and not re.search(r"\d\.\d{4}", text)  # no vectors/values
    assert "small" in repr(LocalWhisperTranscriptionProvider("small"))


# ----------------------------------------------- opt-in real-model integration

LOCAL = os.environ.get("SAM_RUN_LOCAL_VOICE") == "1" and shutil.which("say") is not None


@pytest.mark.skipif(
    not LOCAL, reason="set SAM_RUN_LOCAL_VOICE=1 with the voice-local group and models"
)
def test_real_local_models_end_to_end(tmp_path: Path) -> None:  # pragma: no cover
    from sam.voice_identity.providers import cosine_similarity
    from sam.voice_local.speaker import SpeechBrainEcapaEmbeddingProvider
    from sam.voice_local.stt import LocalWhisperTranscriptionProvider

    def speak(voice: str, text: str) -> Any:
        path = tmp_path / f"{voice}-{abs(hash(text))}.wav"
        subprocess.run(
            ["say", "-v", voice, "-o", str(path), "--data-format=LEI16@16000", text],
            check=True,
        )
        return validate_audio(
            AudioInput(content=path.read_bytes(), declared_format=AudioFormat.WAV_PCM16)
        )

    speaker = SpeechBrainEcapaEmbeddingProvider()
    a1 = speaker.embed(
        speak("Samantha", "The quarterly report is ready for review"),
        timeout_seconds=60,
    )
    a2 = speaker.embed(
        speak("Samantha", "Please schedule a meeting for tomorrow"), timeout_seconds=60
    )
    d1 = speaker.embed(
        speak("Daniel", "The quarterly report is ready for review"), timeout_seconds=60
    )
    assert len(a1) == 192
    assert cosine_similarity(a1, a2) > 0.5 > cosine_similarity(a1, d1)
    stt = LocalWhisperTranscriptionProvider("small")
    result: Any = stt.transcribe(
        TranscriptionRequest(
            session_id="s",
            utterance_id="u",
            audio=speak("Samantha", "The quarterly report is ready for review"),
        ),
        timeout_seconds=120,
    )
    assert "quarterly report" in result["text"].lower()
