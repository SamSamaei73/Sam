"""Installed voice discovery stays bounded, local, cached and recoverable."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sam.system import local_voice as catalog


def test_fixed_voice_probe_is_cached_and_never_changes_language(monkeypatch):
    monkeypatch.setattr(catalog.sys, "platform", "darwin")
    run = Mock(return_value=SimpleNamespace(stdout=b"Samantha en_US # Hello\n"))
    monkeypatch.setattr(catalog.subprocess, "run", run)
    voices = catalog.InstalledVoiceCatalog()
    assert voices.voice("en") == "Samantha"
    assert voices.voice("fa") is None
    assert voices.voice("en") == "Samantha"
    run.assert_called_once_with(
        ["/usr/bin/say", "-v", "?"],
        stdin=catalog.subprocess.DEVNULL,
        stdout=catalog.subprocess.PIPE,
        stderr=catalog.subprocess.DEVNULL,
        timeout=10,
        check=True,
    )


def test_slow_catalog_is_unavailable_then_recovers_without_restart(monkeypatch):
    monkeypatch.setattr(catalog.sys, "platform", "darwin")
    now = 0.0
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now)
    run = Mock(
        side_effect=[
            catalog.subprocess.TimeoutExpired("say", 10),
            SimpleNamespace(stdout=b"Samantha en_US # Hello\n"),
        ]
    )
    monkeypatch.setattr(catalog.subprocess, "run", run)
    voices = catalog.InstalledVoiceCatalog()
    assert voices.voice("en") is None and voices.failed
    now = 2
    assert voices.voice("en") is None and run.call_count == 1
    now = 6
    assert voices.voice("en") == "Samantha" and not voices.failed


@pytest.mark.parametrize(
    "output",
    [b"Fred en_US # Hello\n", b"Samantha invalid/locale # Hi\n", b"x" * 262_145],
)
def test_invalid_or_missing_required_voice_never_claims_ready(monkeypatch, output):
    monkeypatch.setattr(catalog.sys, "platform", "darwin")
    monkeypatch.setattr(
        catalog.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=output))
    )
    assert catalog.InstalledVoiceCatalog().voice("en") is None
