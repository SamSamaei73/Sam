"""Dedicated secret-detection regression tests.

These patterns are a best-effort heuristic, not comprehensive secret
detection — see ``sam.memory.sanitization``'s module docstring. This file
exists specifically so a regression in the pattern list is caught early.
"""

import pytest

from sam.memory.sanitization import looks_like_secret, redact


@pytest.mark.parametrize(
    "text",
    [
        "my API key is sk-ant-abcdefghijklmnop1234567890",
        "here's my api key: sk-1234567890abcdef1234567890abcdef",
        "Authorization: Bearer abcdEFGH12345.token-value_here",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK...",
        "AKIAABCDEFGHIJKLMNOP is my access key id",
        "token: ghp_abcdefghijklmnopqrstuvwxyz012345",
        # Synthetic value, split so secret scanners do not flag it.
        "slack token " + "xox" + "b-EXAMPLE-NOT-A-REAL-TOKEN",
        "my password is hunter2plus",
        "the passwd=SuperSecret123!",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_secret_like_content_is_detected(text: str) -> None:
    assert looks_like_secret(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "User prefers Farsi technical explanations.",
        "Sam uses FastAPI.",
        "The meeting is scheduled for 3pm tomorrow.",
        "Phase 3 Permission Engine was approved.",
        "This project targets Python 3.12.",
    ],
)
def test_ordinary_content_is_not_flagged(text: str) -> None:
    assert looks_like_secret(text) is False


def test_redact_removes_the_secret_span() -> None:
    text = "my API key is sk-ant-abcdefghijklmnop1234567890, please remember it"
    redacted = redact(text)
    assert "sk-ant-abcdefghijklmnop1234567890" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_preserves_surrounding_safe_text() -> None:
    text = "the password is hunter2plus and that's all"
    redacted = redact(text)
    assert redacted.startswith("the ") or "the" in redacted
    assert "hunter2plus" not in redacted


def test_redact_is_idempotent_on_clean_text() -> None:
    text = "Sam uses FastAPI."
    assert redact(text) == text
