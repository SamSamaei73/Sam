"""Trusted speaker selection cannot silently reuse another model or profile."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from sam.core.config import Settings
from sam.voice_identity.errors import ModelSetupError
from sam.voice_identity.store import InMemoryVoiceProfileStore
from sam.voice_local import factory, registry, setup
from sam.voice_local.install import required_models
from sam.voice_local.speaker import SpeechBrainEcapaEmbeddingProvider
from sam.voice_local.speaker_wespeaker import WeSpeakerEmbeddingProvider


def test_default_speaker_and_threshold_stay_backward_compatible() -> None:
    settings = Settings()
    assert settings.local_speaker_model == "ecapa"
    assert settings.speaker_verification_threshold == 0.5
    assert required_models(settings)[0] is registry.SPEAKER_MODEL


@pytest.mark.parametrize("name", ["", "other", "org/custom", "../model", "WESPEAKER"])
def test_untrusted_speaker_selection_fails_closed(name: str) -> None:
    with pytest.raises(ValueError):
        Settings(local_speaker_model=name)  # type: ignore[arg-type]
    with pytest.raises(ModelSetupError, match="^unsupported speaker model$"):
        registry.speaker_model(name)


def test_environment_selects_only_the_pinned_wespeaker_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_SPEAKER_MODEL", "wespeaker")
    settings = Settings()
    assert settings.local_speaker_model == "wespeaker"
    assert required_models(settings) == (
        registry.WESPEAKER_MODEL,
        registry.STT_MODELS["small"],
    )
    assert registry.WESPEAKER_MODEL.files[0].name == "voxceleb_resnet34_LM.onnx"
    assert registry.WESPEAKER_MODEL.total_bytes == 26_530_309
    assert settings.speaker_verification_threshold == 0.5


@pytest.mark.parametrize("selection", ["ecapa", "wespeaker"])
def test_factory_and_readiness_use_only_the_selected_model(
    selection: Literal["ecapa", "wespeaker"],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sam.voice_local.factory.sys.platform", "darwin")
    seen: list[registry.TrustedModel] = []

    def verify(_path: Path, model: registry.TrustedModel) -> bool:
        seen.append(model)
        return True

    monkeypatch.setattr(
        factory,
        "verify_model",
        verify,
    )
    monkeypatch.setattr(
        factory, "MacOSKeychainVoiceProfileStore", InMemoryVoiceProfileStore
    )
    settings = Settings(voice_identity_enabled=True, local_speaker_model=selection)
    stack = factory.local_voice_from_settings(settings, tmp_path)
    assert stack is not None
    expected_class = (
        WeSpeakerEmbeddingProvider
        if selection == "wespeaker"
        else SpeechBrainEcapaEmbeddingProvider
    )
    assert isinstance(stack.embedder, expected_class)
    assert seen == list(required_models(settings))
    seen.clear()
    assert factory.local_voice_readiness(settings, tmp_path) == "ready"
    assert seen == list(required_models(settings))


def test_missing_selected_model_never_falls_back_to_ecapa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sam.voice_local.factory.sys.platform", "darwin")
    monkeypatch.setattr(
        factory,
        "verify_model",
        lambda _path, model: model is not registry.WESPEAKER_MODEL,
    )
    settings = Settings(voice_identity_enabled=True, local_speaker_model="wespeaker")
    assert factory.local_voice_from_settings(settings, tmp_path) is None
    assert factory.local_voice_readiness(settings, tmp_path) == "models_missing"


def test_setup_uses_environment_selection_and_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models: list[registry.TrustedModel] = []

    def acquire(model: registry.TrustedModel, root: Path) -> Path:
        models.append(model)
        assert root == tmp_path
        return root

    monkeypatch.setattr(setup, "acquire", acquire)
    monkeypatch.setenv("LOCAL_SPEAKER_MODEL", "wespeaker")
    assert setup.main(["--speaker", "--root", str(tmp_path)]) == 0
    assert models == [registry.WESPEAKER_MODEL]
    models.clear()
    assert (
        setup.main(["--speaker", "--speaker-model", "ecapa", "--root", str(tmp_path)])
        == 0
    )
    assert models == [registry.SPEAKER_MODEL]


def test_setup_rejects_selector_without_speaker_install() -> None:
    with pytest.raises(SystemExit) as raised:
        setup.main(["--speaker-model", "wespeaker"])
    assert raised.value.code == 2
