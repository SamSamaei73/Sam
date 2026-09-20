"""Explicit, one-time model acquisition. Never called during a request.

    python -m sam.voice_local.setup --speaker --stt small

Downloads ONLY the pinned files of the trusted registry from the fixed
Hugging Face endpoint at the pinned commit, verifies each SHA-256 (a mismatch
deletes the file and fails), and atomically moves the verified set into place.
No remote code is executed and nothing else is fetched.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from sam.voice_identity.errors import ModelSetupError
from sam.voice_local.registry import (
    HF_ENDPOINT,
    SPEAKER_MODEL,
    STT_MODELS,
    TrustedModel,
    default_model_root,
    model_dir,
    sha256_file,
    verify_model,
)


def acquire(model: TrustedModel, root: Path) -> Path:
    """Download + verify ``model`` into ``root``; returns its directory."""

    target = model_dir(root, model)
    if verify_model(target, model):
        return target
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise ModelSetupError(
            "install the voice-local dependency group first"
        ) from None
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target.parent))
    try:
        for spec in model.files:
            hf_hub_download(
                repo_id=model.repo_id,
                filename=spec.name,
                revision=model.revision,  # a commit sha, not a movable tag
                local_dir=staging,
                endpoint=HF_ENDPOINT,
            )
            if sha256_file(staging / spec.name) != spec.sha256:
                raise ModelSetupError("a downloaded model file failed verification")
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
    except ModelSetupError:
        raise
    except Exception:
        raise ModelSetupError("model download failed") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire Sam's trusted local voice models."
    )
    parser.add_argument(
        "--speaker", action="store_true", help="speaker-verification model (~83 MB)"
    )
    parser.add_argument(
        "--stt", choices=sorted(STT_MODELS), help="speech-to-text model size"
    )
    parser.add_argument("--root", type=Path, default=default_model_root())
    args = parser.parse_args(argv)
    wanted = ([SPEAKER_MODEL] if args.speaker else []) + (
        [STT_MODELS[args.stt]] if args.stt else []
    )
    if not wanted:
        parser.error("choose --speaker and/or --stt")
    for model in wanted:
        size_mb = model.total_bytes / 1e6
        label = f"{model.repo_id}@{model.revision[:12]}"
        print(f"{label} ({size_mb:.0f} MB, {model.license})")
        print("  ->", acquire(model, args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
