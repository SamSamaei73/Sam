"""Trusted model registry: the ONLY models Sam will load.

Model identity comes from this file (Sam configuration), never from a user, an
LLM, a document, an MCP server, or a request. Every file is pinned to an exact
Hugging Face commit revision AND a SHA-256, so a mirror, a moved tag, or a
tampered file cannot substitute different weights: verification fails closed.

Provenance (checked 2026-09):
- speechbrain/spkrec-ecapa-voxceleb -- Apache-2.0; ECAPA-TDNN trained on
  VoxCeleb 1+2. Only ``embedding_model.ckpt`` is used; the repo's
  ``hyperparams.yaml`` (HyperPyYAML ``!new:`` tags = object construction) and
  any ``custom.py`` are NEVER loaded. The network is rebuilt in Sam code.
- Systran/faster-whisper-{small,medium,large-v3} -- MIT; CTranslate2
  conversions of OpenAI's multilingual Whisper (Persian ``fa`` supported). No
  English-only ``.en`` model is allowed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sam.voice_identity.errors import ModelSetupError

HF_ENDPOINT = "https://huggingface.co"  # fixed; never taken from the environment
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ModelFile:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class TrustedModel:
    key: str
    repo_id: str
    revision: str
    license: str
    files: tuple[ModelFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


SPEAKER_MODEL = TrustedModel(
    key="speaker-ecapa",
    repo_id="speechbrain/spkrec-ecapa-voxceleb",
    revision="0f99f2d0ebe89ac095bcc5903c4dd8f72b367286",
    license="Apache-2.0",
    files=(
        ModelFile(
            "embedding_model.ckpt",
            "0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2",
            83_316_686,
        ),
    ),
)

_WHISPER_TOK = ModelFile(
    "tokenizer.json",
    "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
    2_203_239,
)
_WHISPER_VOCAB = ModelFile(
    "vocabulary.txt",
    "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
    459_861,
)

STT_MODELS: dict[str, TrustedModel] = {
    "small": TrustedModel(
        "stt-small",
        "Systran/faster-whisper-small",
        "536b0662742c02347bc0e980a01041f333bce120",
        "MIT",
        (
            ModelFile(
                "config.json",
                "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
                2_370,
            ),
            ModelFile(
                "model.bin",
                "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
                483_546_902,
            ),
            _WHISPER_TOK,
            _WHISPER_VOCAB,
        ),
    ),
    "medium": TrustedModel(
        "stt-medium",
        "Systran/faster-whisper-medium",
        "08e178d48790749d25932bbc082711ddcfdfbc4f",
        "MIT",
        (
            ModelFile(
                "config.json",
                "3622a2ddc41ec0e0fd4e68c13c6830f03b90c38d89aaad184de02c8c642cf807",
                2_257,
            ),
            ModelFile(
                "model.bin",
                "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
                1_527_906_378,
            ),
            _WHISPER_TOK,
            _WHISPER_VOCAB,
        ),
    ),
    "large-v3": TrustedModel(
        "stt-large-v3",
        "Systran/faster-whisper-large-v3",
        "edaa852ec7e145841d8ffdb056a99866b5f0a478",
        "MIT",
        (
            ModelFile(
                "config.json",
                "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9",
                2_394,
            ),
            ModelFile(
                "model.bin",
                "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
                3_087_284_237,
            ),
            ModelFile(
                "preprocessor_config.json",
                "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
                340,
            ),
            ModelFile(
                "tokenizer.json",
                "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca",
                2_480_617,
            ),
            ModelFile(
                "vocabulary.json",
                "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
                1_068_114,
            ),
        ),
    ),
}


def default_model_root() -> Path:
    """Bounded, user-owned cache: ``~/Library/Application Support/Sam/models``."""

    return Path.home() / "Library" / "Application Support" / "Sam" / "models"


def model_dir(root: Path, model: TrustedModel) -> Path:
    return root / model.key / model.revision[:12]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(directory: Path, model: TrustedModel) -> bool:
    """True only if every pinned file exists with the pinned size AND hash."""

    for spec in model.files:
        path = directory / spec.name
        try:
            if not path.is_file() or path.stat().st_size != spec.size:
                return False
            if sha256_file(path) != spec.sha256:
                return False
        except OSError:
            return False
    return True


def require_verified(directory: Path, model: TrustedModel) -> Path:
    if not verify_model(directory, model):
        raise ModelSetupError("the trusted model is missing or failed verification")
    return directory


def stt_model(name: str) -> TrustedModel:
    try:
        return STT_MODELS[name]
    except KeyError:
        raise ModelSetupError("unsupported speech model") from None


__all__ = [
    "HF_ENDPOINT",
    "SPEAKER_MODEL",
    "STT_MODELS",
    "ModelFile",
    "TrustedModel",
    "default_model_root",
    "model_dir",
    "require_verified",
    "sha256_file",
    "stt_model",
    "verify_model",
]
