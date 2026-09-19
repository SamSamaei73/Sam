"""Tests for sam.knowledge.parser: TextParser, MarkdownParser, JSONParser,
CSVParser, PDFParser, and the parser-selection helpers."""

from __future__ import annotations

import json
import zlib

import pytest

from sam.knowledge.errors import ExtractionError, ParsingError, ResourceTooLargeError
from sam.knowledge.models import MAX_CHUNKS_PER_RESOURCE, DocumentMetadata, ResourceType
from sam.knowledge.parser import (
    MAX_JSON_NESTING_DEPTH,
    CSVParser,
    JSONParser,
    MarkdownParser,
    PDFParser,
    TextParser,
    default_parsers,
    select_parser,
)

META = DocumentMetadata()


def _build_pdf(
    pages: list[str], *, compress: bool = True, font_obj: int = 100
) -> bytes:
    """Construct a minimal, structurally valid multi-page PDF with one
    Tj text-showing operator per page — used to exercise PDFParser
    without any external PDF-generation library."""

    body: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
    }
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    body[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    for i, text in enumerate(pages):
        page_num = 3 + 2 * i
        content_num = page_num + 1
        body[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /Contents {content_num} 0 R "
            f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
            f"/MediaBox [0 0 612 792] >>"
        ).encode()
        stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode()
        if compress:
            compressed = zlib.compress(stream)
            body[content_num] = (
                b"<< /Filter /FlateDecode /Length "
                + str(len(compressed)).encode()
                + b" >>\nstream\n"
                + compressed
                + b"\nendstream"
            )
        else:
            body[content_num] = (
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
    body[font_obj] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    for num in sorted(body):
        out += f"{num} 0 obj\n".encode() + body[num] + b"\nendobj\n"
    out += b"trailer << /Root 1 0 R >>\n%%EOF"
    return bytes(out)


class TestDefaultParsersAndSelection:
    def test_default_parsers_cover_every_resource_type(self) -> None:
        parsers = default_parsers()
        for resource_type in ResourceType:
            select_parser(resource_type, parsers)  # must not raise

    def test_select_parser_returns_matching_instance(self) -> None:
        parser = select_parser(ResourceType.TXT, default_parsers())
        assert isinstance(parser, TextParser)


class TestTextParser:
    def test_supports_only_txt(self) -> None:
        parser = TextParser()
        assert parser.supports(ResourceType.TXT)
        assert not parser.supports(ResourceType.MARKDOWN)

    def test_whole_document_one_segment(self) -> None:
        parsed = TextParser().parse(b"hello world", META)
        assert len(parsed.segments) == 1
        assert parsed.segments[0].text == "hello world"
        assert parsed.segments[0].page_number is None
        assert parsed.segments[0].section_title is None

    def test_empty_text_no_segments(self) -> None:
        parsed = TextParser().parse(b"   \n\t  ", META)
        assert parsed.segments == ()
        assert parsed.has_extractable_text is False

    def test_invalid_utf8_raises(self) -> None:
        with pytest.raises(ParsingError):
            TextParser().parse(b"\xff\xfe\x00\x01", META)


class TestMarkdownParser:
    def test_supports_only_markdown(self) -> None:
        parser = MarkdownParser()
        assert parser.supports(ResourceType.MARKDOWN)
        assert not parser.supports(ResourceType.TXT)

    def test_sections_split_on_headings(self) -> None:
        text = "# Intro\nHello.\n\n## Graph Attention Networks\nGAT details.\n"
        parsed = MarkdownParser().parse(text.encode(), META)
        titles = [s.section_title for s in parsed.segments]
        assert titles == ["Intro", "Graph Attention Networks"]

    def test_content_before_first_heading_has_none_title(self) -> None:
        text = "preamble text\n# Heading\nbody\n"
        parsed = MarkdownParser().parse(text.encode(), META)
        assert parsed.segments[0].section_title is None
        assert parsed.segments[0].text == "preamble text"

    def test_heading_levels_all_reset_title(self) -> None:
        text = "# One\na\n### Two\nb\n"
        parsed = MarkdownParser().parse(text.encode(), META)
        assert [s.section_title for s in parsed.segments] == ["One", "Two"]

    def test_empty_document_no_segments(self) -> None:
        parsed = MarkdownParser().parse(b"", META)
        assert parsed.segments == ()

    def test_heading_with_no_body_produces_no_segment(self) -> None:
        parsed = MarkdownParser().parse(b"# Just a heading\n", META)
        assert parsed.segments == ()


class TestJSONParser:
    def test_supports_only_json(self) -> None:
        parser = JSONParser()
        assert parser.supports(ResourceType.JSON)
        assert not parser.supports(ResourceType.CSV)

    def test_array_becomes_one_segment_per_element(self) -> None:
        data = [{"a": 1}, {"b": 2}, {"c": 3}]
        parsed = JSONParser().parse(json.dumps(data).encode(), META)
        assert len(parsed.segments) == 3
        assert [s.paragraph_index for s in parsed.segments] == [0, 1, 2]

    def test_object_becomes_single_segment(self) -> None:
        data = {"a": 1, "b": [1, 2, 3]}
        parsed = JSONParser().parse(json.dumps(data).encode(), META)
        assert len(parsed.segments) == 1
        assert parsed.segments[0].paragraph_index == 0

    def test_scalar_becomes_single_segment(self) -> None:
        parsed = JSONParser().parse(b"42", META)
        assert len(parsed.segments) == 1

    def test_empty_array_no_segments(self) -> None:
        parsed = JSONParser().parse(b"[]", META)
        assert parsed.segments == ()

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(ParsingError):
            JSONParser().parse(b"{not valid json", META)

    def test_deeply_nested_json_rejected(self) -> None:
        nested = (
            "[" * (MAX_JSON_NESTING_DEPTH + 5)
            + "1"
            + "]" * (MAX_JSON_NESTING_DEPTH + 5)
        )
        with pytest.raises(ParsingError):
            JSONParser().parse(nested.encode(), META)

    def test_nesting_at_exactly_the_limit_allowed(self) -> None:
        nested = "[" * MAX_JSON_NESTING_DEPTH + "1" + "]" * MAX_JSON_NESTING_DEPTH
        JSONParser().parse(nested.encode(), META)  # must not raise

    def test_oversized_array_rejected(self) -> None:
        data = list(range(MAX_CHUNKS_PER_RESOURCE + 1))
        with pytest.raises(ResourceTooLargeError):
            JSONParser().parse(json.dumps(data).encode(), META)

    def test_invalid_utf8_raises(self) -> None:
        with pytest.raises(ParsingError):
            JSONParser().parse(b"\xff\xfe", META)

    def test_null_elements_render_and_are_not_blank(self) -> None:
        parsed = JSONParser().parse(json.dumps([None, 1]).encode(), META)
        assert len(parsed.segments) == 2
        assert parsed.segments[0].text == "null"


class TestCSVParser:
    def test_supports_only_csv(self) -> None:
        parser = CSVParser()
        assert parser.supports(ResourceType.CSV)
        assert not parser.supports(ResourceType.JSON)

    def test_rows_rendered_with_column_names(self) -> None:
        csv_text = "name,age\nAlice,30\nBob,25\n"
        parsed = CSVParser().parse(csv_text.encode(), META)
        assert len(parsed.segments) == 2
        assert parsed.segments[0].text == "name: Alice | age: 30"
        assert parsed.segments[0].paragraph_index == 0
        assert parsed.segments[1].paragraph_index == 1

    def test_header_only_no_data_segments(self) -> None:
        parsed = CSVParser().parse(b"name,age\n", META)
        assert parsed.segments == ()

    def test_empty_content_no_segments(self) -> None:
        parsed = CSVParser().parse(b"", META)
        assert parsed.segments == ()

    def test_extra_columns_labeled_positionally(self) -> None:
        csv_text = "name\nAlice,extra\n"
        parsed = CSVParser().parse(csv_text.encode(), META)
        assert "column_1: extra" in parsed.segments[0].text

    def test_oversized_field_rejected(self) -> None:
        huge = "x" * 200_000
        with pytest.raises((ResourceTooLargeError, ParsingError)):
            CSVParser().parse(f"col\n{huge}\n".encode(), META)

    def test_too_many_rows_rejected(self) -> None:
        rows = "\n".join(str(i) for i in range(MAX_CHUNKS_PER_RESOURCE + 5))
        content = f"col\n{rows}\n".encode()
        with pytest.raises(ResourceTooLargeError):
            CSVParser().parse(content, META)

    def test_invalid_utf8_raises(self) -> None:
        with pytest.raises(ParsingError):
            CSVParser().parse(b"\xff\xfe", META)


class TestPDFParser:
    def test_supports_only_pdf(self) -> None:
        parser = PDFParser()
        assert parser.supports(ResourceType.PDF)
        assert not parser.supports(ResourceType.TXT)

    def test_multi_page_extraction_preserves_page_numbers(self) -> None:
        pdf = _build_pdf(["Hello World Page One", "Second Page Content Here"])
        parsed = PDFParser().parse(pdf, META)
        assert [s.page_number for s in parsed.segments] == [1, 2]
        assert parsed.segments[0].text == "Hello World Page One"
        assert parsed.segments[1].text == "Second Page Content Here"

    def test_single_page_uncompressed_stream(self) -> None:
        pdf = _build_pdf(["Uncompressed text run"], compress=False)
        parsed = PDFParser().parse(pdf, META)
        assert parsed.segments[0].text == "Uncompressed text run"
        assert parsed.segments[0].page_number == 1

    def test_pages_never_merge_across_boundary(self) -> None:
        pdf = _build_pdf(["Alpha content", "Beta content", "Gamma content"])
        parsed = PDFParser().parse(pdf, META)
        assert len(parsed.segments) == 3
        assert parsed.segments[2].page_number == 3

    def test_missing_header_rejected(self) -> None:
        with pytest.raises(ParsingError):
            PDFParser().parse(b"not a pdf at all", META)

    def test_empty_pdf_no_objects_rejected(self) -> None:
        with pytest.raises(ParsingError):
            PDFParser().parse(b"%PDF-1.4\n%%EOF", META)

    def test_no_pages_rejected(self) -> None:
        pdf = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
            b"trailer << /Root 1 0 R >>\n%%EOF"
        )
        with pytest.raises(ParsingError):
            PDFParser().parse(pdf, META)

    def test_image_only_pdf_has_no_extractable_text(self) -> None:
        # A structurally valid page with no text-showing operators at
        # all (simulating a scanned/image-only page).
        body = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R "
            b"/Resources << >> /MediaBox [0 0 612 792] >>",
            4: b"<< /Length 3 >>\nstream\nq Q\nendstream",
        }
        out = bytearray(b"%PDF-1.4\n")
        for num in sorted(body):
            out += f"{num} 0 obj\n".encode() + body[num] + b"\nendobj\n"
        out += b"trailer << /Root 1 0 R >>\n%%EOF"
        parsed = PDFParser().parse(bytes(out), META)
        assert parsed.has_extractable_text is False

    def test_fallback_page_order_without_resolvable_catalog(self) -> None:
        # No /Type /Catalog object at all — the parser must fall back to
        # ascending-object-number ordering of /Type /Page objects rather
        # than raising.
        body = {
            10: b"<< /Type /Page /Contents 11 0 R /Resources << >> "
            b"/MediaBox [0 0 612 792] >>",
            11: b"<< /Length 30 >>\nstream\nBT (Fallback text) Tj ET\nendstream",
        }
        out = bytearray(b"%PDF-1.4\n")
        for num in sorted(body):
            out += f"{num} 0 obj\n".encode() + body[num] + b"\nendobj\n"
        out += b"%%EOF"
        parsed = PDFParser().parse(bytes(out), META)
        assert parsed.segments[0].text == "Fallback text"

    def test_decompression_bomb_rejected(self) -> None:
        huge = b"0" * 30_000_000
        compressed = zlib.compress(huge, 9)
        body = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R "
            b"/Resources << >> /MediaBox [0 0 612 792] >>",
            4: b"<< /Filter /FlateDecode /Length "
            + str(len(compressed)).encode()
            + b" >>\nstream\n"
            + compressed
            + b"\nendstream",
        }
        out = bytearray(b"%PDF-1.4\n")
        for num in sorted(body):
            out += f"{num} 0 obj\n".encode() + body[num] + b"\nendobj\n"
        out += b"trailer << /Root 1 0 R >>\n%%EOF"
        with pytest.raises(ExtractionError):
            PDFParser().parse(bytes(out), META)

    def test_cyclic_kids_reference_does_not_hang(self) -> None:
        body = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [2 0 R] /Count 1 >>",
        }
        out = bytearray(b"%PDF-1.4\n")
        for num in sorted(body):
            out += f"{num} 0 obj\n".encode() + body[num] + b"\nendobj\n"
        out += b"trailer << /Root 1 0 R >>\n%%EOF"
        with pytest.raises(ParsingError):
            PDFParser().parse(bytes(out), META)

    def test_escaped_parens_in_text_decoded(self) -> None:
        stream = rb"BT (Hello \(World\)) Tj ET"
        body = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R "
            b"/Resources << >> /MediaBox [0 0 612 792] >>",
            4: b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream",
        }
        out = bytearray(b"%PDF-1.4\n")
        for num in sorted(body):
            out += f"{num} 0 obj\n".encode() + body[num] + b"\nendobj\n"
        out += b"trailer << /Root 1 0 R >>\n%%EOF"
        parsed = PDFParser().parse(bytes(out), META)
        assert parsed.segments[0].text == "Hello (World)"
