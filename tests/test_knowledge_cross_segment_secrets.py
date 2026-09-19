"""Secrets split across parsed segments (PDF pages, sections, rows) must
still be rejected before anything is persisted.

Every fixture below is synthetic. The production detector
(``sam.memory.sanitization.looks_like_secret``) is untouched; these tests
only prove the ingestion scan also looks *across* segment boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sam.knowledge.chunker import ChunkerConfig
from sam.knowledge.ingestion import (
    IngestionOutcome,
    _contains_secret,
    run_ingestion_pipeline,
)
from sam.knowledge.models import (
    IngestResourceRequest,
    KnowledgeErrorCategory,
    ResourceSourceKind,
    ResourceType,
)
from sam.knowledge.parser import default_parsers
from sam.memory.sanitization import looks_like_secret
from sam.permissions.models import Principal, PrincipalKind
from tests.test_knowledge_parser import _build_pdf

NOW = datetime.now(UTC)
PRINCIPAL = Principal(kind=PrincipalKind.USER, id="alice")

# (label, minimal secret-shaped string the detector accepts)
MINIMAL_SECRETS = [
    ("anthropic", "sk-ant-abcdefghij"),  # prefix + exactly 10 chars
    ("openai-style", "sk-abcdefghijklmnop"),  # prefix + exactly 16 chars
    ("aws", "AKIAABCDEFGHIJKLMNOP"),
    ("github", "ghp_abcdefghijklmnopqrst"),  # prefix + exactly 20 chars
    ("slack", "xoxb-abcdefghij"),
    ("bearer", "Bearer abcdefghij"),
    ("pem", "-----BEGIN RSA PRIVATE KEY-----"),
    ("jwt", "eyJhbGci.eyJzdWIi.c2ln"),
    ("password", "password is hunter2"),
    ("api-key-phrase", "api key: abcdefgh"),
]


def _request(**overrides: Any) -> IngestResourceRequest:
    fields: dict[str, Any] = dict(
        principal=PRINCIPAL,
        collection_id="docs",
        name="doc.txt",
        declared_resource_type=ResourceType.TXT,
        source_kind=ResourceSourceKind.UPLOAD,
        source_label="doc.txt",
        content=b"placeholder",
    )
    fields.update(overrides)
    return IngestResourceRequest(**fields)


def _run(request: IngestResourceRequest) -> IngestionOutcome:
    return run_ingestion_pipeline(
        request,
        parsers=default_parsers(),
        chunker_config=ChunkerConfig(),
        now=NOW,
    )


# --------------------------------------------------------------------- #
# Unit: the scan itself
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(("label", "secret"), MINIMAL_SECRETS)
def test_fixtures_are_detected_whole_by_the_unchanged_detector(
    label: str, secret: str
) -> None:
    assert looks_like_secret(secret), label


@pytest.mark.parametrize(("label", "secret"), MINIMAL_SECRETS)
def test_secret_split_at_every_position_across_two_segments_is_detected(
    label: str, secret: str
) -> None:
    for i in range(1, len(secret)):
        left, right = secret[:i], secret[i:]
        # Precondition: neither half is a secret on its own, so only the
        # cross-boundary scan can catch this.
        if looks_like_secret(left) or looks_like_secret(right):
            continue
        assert _contains_secret((left, right)), (label, i)


@pytest.mark.parametrize(("label", "secret"), MINIMAL_SECRETS)
def test_secret_split_across_three_segments_is_detected(
    label: str, secret: str
) -> None:
    third = len(secret) // 3
    parts = (secret[:third], secret[third : 2 * third], secret[2 * third :])
    assert not any(looks_like_secret(p) for p in parts)
    assert _contains_secret(parts), label


def test_secret_surrounded_by_ordinary_text_split_across_segments() -> None:
    parts = ("Intro paragraph. The token is sk-ant-abcde", "fghijklm and more text.")
    assert not any(looks_like_secret(p) for p in parts)
    assert _contains_secret(parts)


def test_whitespace_separated_phrase_split_across_segments_is_detected() -> None:
    # "password is <value>" broken at a page/row boundary.
    assert _contains_secret(("the admin password is", "hunter2plus"))
    assert _contains_secret(("the admin password", "is hunter2plus"))


# --------------------------------------------------------------------- #
# Boundary immediately around the detector's minimum pattern length
# --------------------------------------------------------------------- #


def test_anthropic_key_one_char_below_minimum_is_not_detected_when_split() -> None:
    # "sk-ant-" + 9 chars: below the detector's 10-char minimum.
    secret = "sk-ant-" + "abcdefghi"
    assert not looks_like_secret(secret)
    for i in range(1, len(secret)):
        assert not _contains_secret((secret[:i], secret[i:])), i


def test_anthropic_key_exactly_at_minimum_is_detected_when_split_at_prefix() -> None:
    secret = "sk-ant-" + "abcdefghij"  # exactly 10
    assert looks_like_secret(secret)
    assert _contains_secret(("sk-ant-", "abcdefghij"))
    assert _contains_secret(("sk-ant", "-abcdefghij"))
    assert _contains_secret(("sk-ant-abcdefghi", "j"))


def test_github_token_boundary_around_minimum_length() -> None:
    below = "ghp_" + "a" * 19
    at = "ghp_" + "a" * 20
    assert not looks_like_secret(below)
    assert looks_like_secret(at)
    assert not _contains_secret(("ghp_", "a" * 19))
    assert _contains_secret(("ghp_", "a" * 20))
    assert _contains_secret(("ghp_" + "a" * 19, "a"))


def test_aws_key_boundary_around_exact_length() -> None:
    assert not _contains_secret(("AKIA", "A" * 15))
    assert _contains_secret(("AKIA", "A" * 16))


# --------------------------------------------------------------------- #
# No false positives from ordinary adjacent text
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "parts",
    [
        ("The quick brown fox", "jumps over the lazy dog."),
        ("Chapter 1 ends here.", "Chapter 2 begins with graphs."),
        ("Learning sk-", "learning is a fun skill to practise."),
        ("Reset your password", "using the settings page."),
        ("Bearer", "of good news arrived today."),
        ("row one,1,2,3", "row two,4,5,6"),
        ("", ""),
        ("Only one segment of ordinary text",),
    ],
)
def test_ordinary_adjacent_segments_are_not_flagged(parts: tuple[str, ...]) -> None:
    assert _contains_secret(parts) is False


def test_single_segment_secret_still_detected() -> None:
    assert _contains_secret(("my key is sk-ant-abcdefghijklmnop",))


def test_no_segments_is_not_a_secret() -> None:
    assert _contains_secret(()) is False


# --------------------------------------------------------------------- #
# End to end through the real parsers
# --------------------------------------------------------------------- #


def test_secret_split_across_two_pdf_pages_is_rejected() -> None:
    pdf = _build_pdf(["Findings. The key is sk-ant-abcde", "fghijklm continues here."])
    outcome = _run(
        _request(
            content=pdf,
            declared_resource_type=ResourceType.PDF,
            name="paper.pdf",
            source_label="paper.pdf",
        )
    )
    assert outcome.accepted is False
    assert outcome.error_category is KnowledgeErrorCategory.SECRET_DETECTED
    assert outcome.resource is None
    assert outcome.chunks == ()


def test_pdf_pages_with_ordinary_text_are_still_accepted() -> None:
    pdf = _build_pdf(["Graph neural networks.", "Attention over neighbours."])
    outcome = _run(
        _request(
            content=pdf,
            declared_resource_type=ResourceType.PDF,
            name="paper.pdf",
            source_label="paper.pdf",
        )
    )
    assert outcome.accepted is True
    assert {c.location.page_number for c in outcome.chunks} == {1, 2}


def test_secret_split_across_markdown_sections_is_rejected() -> None:
    md = b"# Setup\nthe key is sk-ant-abcde\n\n# Notes\nfghijklm and text\n"
    outcome = _run(
        _request(
            content=md,
            declared_resource_type=ResourceType.MARKDOWN,
            name="notes.md",
            source_label="notes.md",
        )
    )
    # Sections are separate segments; the joined scan must catch this.
    assert outcome.accepted is False
    assert outcome.error_category is KnowledgeErrorCategory.SECRET_DETECTED


def test_distinct_json_values_are_not_treated_as_one_split_secret() -> None:
    """Documented limit, not a bypass: JSON elements (and CSV rows) are
    emitted with their own structural delimiters (quotes, ``key: ``), so
    two distinct values are two values in the source too - there is no
    contiguous secret to find. Only text-flow splits (PDF pages, Markdown
    sections) can cut a single token in two. Deliberately obfuscating a
    secret with inserted separators is out of scope for a pattern-based
    detector."""

    js = b'["the key is sk-ant-abcde", "fghijklm"]'
    outcome = _run(
        _request(
            content=js,
            declared_resource_type=ResourceType.JSON,
            name="data.json",
            source_label="data.json",
        )
    )
    assert outcome.accepted is True


def test_whole_secret_inside_one_json_value_is_still_rejected() -> None:
    js = b'["the key is sk-ant-abcdefghijklmnop", "other"]'
    outcome = _run(
        _request(
            content=js,
            declared_resource_type=ResourceType.JSON,
            name="data.json",
            source_label="data.json",
        )
    )
    assert outcome.accepted is False
    assert outcome.error_category is KnowledgeErrorCategory.SECRET_DETECTED


def test_rejection_never_echoes_the_secret() -> None:
    pdf = _build_pdf(["prefix sk-ant-abcde", "fghijklm suffix"])
    outcome = _run(
        _request(
            content=pdf,
            declared_resource_type=ResourceType.PDF,
            name="paper.pdf",
            source_label="paper.pdf",
        )
    )
    dumped = repr(outcome)
    for fragment in ("sk-ant", "abcde", "fghijklm"):
        assert fragment not in dumped
