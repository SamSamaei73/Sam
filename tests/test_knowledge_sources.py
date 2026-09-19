"""Tests for sam.knowledge.sources — declared-vs-actual type validation."""

from __future__ import annotations

import pytest

from sam.knowledge.errors import UnsupportedResourceError
from sam.knowledge.models import ResourceType
from sam.knowledge.sources import sniff_conflicting_type, validate_declared_type


class TestSniffConflictingType:
    def test_pdf_declared_as_txt_flagged(self) -> None:
        content = b"%PDF-1.4\nsome bytes"
        assert sniff_conflicting_type(content, ResourceType.TXT) is ResourceType.PDF

    def test_plain_text_declared_as_txt_no_conflict(self) -> None:
        assert sniff_conflicting_type(b"hello world", ResourceType.TXT) is None

    def test_declared_pdf_without_signature_raises(self) -> None:
        with pytest.raises(UnsupportedResourceError):
            sniff_conflicting_type(b"not a pdf", ResourceType.PDF)

    def test_declared_pdf_with_signature_ok(self) -> None:
        assert sniff_conflicting_type(b"%PDF-1.4\n...", ResourceType.PDF) is None

    def test_leading_whitespace_before_signature_tolerated(self) -> None:
        assert sniff_conflicting_type(b"   %PDF-1.4\n...", ResourceType.PDF) is None


class TestValidateDeclaredType:
    def test_pdf_mismatch_rejected(self) -> None:
        with pytest.raises(UnsupportedResourceError):
            validate_declared_type(b"%PDF-1.4\n...", ResourceType.TXT)

    def test_matching_pdf_ok(self) -> None:
        validate_declared_type(b"%PDF-1.4\n...", ResourceType.PDF)

    def test_matching_txt_ok(self) -> None:
        validate_declared_type(b"hello world", ResourceType.TXT)

    def test_null_byte_in_declared_text_rejected(self) -> None:
        with pytest.raises(UnsupportedResourceError):
            validate_declared_type(b"hello\x00world", ResourceType.TXT)

    def test_null_byte_in_declared_markdown_rejected(self) -> None:
        with pytest.raises(UnsupportedResourceError):
            validate_declared_type(b"# hi\x00", ResourceType.MARKDOWN)

    def test_null_byte_in_declared_csv_rejected(self) -> None:
        with pytest.raises(UnsupportedResourceError):
            validate_declared_type(b"a,b\x00\n1,2\n", ResourceType.CSV)

    def test_null_byte_in_json_not_specially_checked_here(self) -> None:
        # JSON's own decode/parse layer catches structural problems;
        # this module only special-cases the plain-text formats.
        validate_declared_type(b'{"a": 1}', ResourceType.JSON)
