"""The parser boundary: raw bytes in, structured ``ParsedDocument`` out.

``DocumentParser`` is a narrow ``Protocol`` — adding a new format means
adding a new class that implements it, never widening this module's
responsibility. No parser here produces embeddings, chunks, or does any
I/O beyond reading the bytes it is given; see ``sam.knowledge.chunker``
and ``sam.knowledge.index`` for what comes after parsing.

Every parser treats its input as fully untrusted: malformed, truncated,
oversized, or adversarially constructed content must raise a typed
``KnowledgeError`` (never crash the process, never hang, never silently
fabricate content) — see each parser's docstring for its specific
safeguards, and ``docs/knowledge.md``'s "Parser safety" section for the
adversarial cases this was tested against.

PDF note: no PDF library is installed and none is added for this phase
(the task explicitly prefers "implement the abstraction and a safe
unsupported-format response" over "add a large dependency unnecessarily"
when a format cannot safely be parsed with what is already installed).
``PDFParser`` is a real, bounded, standard-library-only implementation —
not a fake — but it is a best-effort text/page extractor, not a full PDF
renderer: it recovers page boundaries and literal text-showing operators
(``Tj``/``TJ``) from FlateDecode/uncompressed content streams. It does
not handle encrypted PDFs, complex CID/Type0 font encodings, or any
layout reconstruction, and it never fabricates text for scanned/
image-only pages — see the class docstring below.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zlib
from typing import Protocol

from sam.knowledge.errors import ExtractionError, ParsingError, ResourceTooLargeError
from sam.knowledge.models import (
    MAX_CHUNKS_PER_RESOURCE,
    DocumentMetadata,
    ParsedDocument,
    ParsedSegment,
    ResourceType,
)

MAX_JSON_NESTING_DEPTH = 50
_MAX_CSV_FIELD_LENGTH = 100_000
_MAX_CSV_ROWS = MAX_CHUNKS_PER_RESOURCE
_MAX_PDF_STREAM_OUTPUT_BYTES = 20_000_000
_MAX_PDF_OBJECTS = 200_000


class DocumentParser(Protocol):
    """A parser for one ``ResourceType``. Stateless — safe to share a
    single instance across every ingestion."""

    def supports(self, resource_type: ResourceType) -> bool: ...

    def parse(self, source: bytes, metadata: DocumentMetadata) -> ParsedDocument: ...


def _decode_utf8(source: bytes, *, label: str) -> str:
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParsingError(f"{label} content is not valid UTF-8") from exc


class TextParser:
    """Plain TXT: the whole document is one segment. Character offsets
    are attached later by ``sam.knowledge.chunker`` — a parser never
    invents page or section information that plain text does not have."""

    def supports(self, resource_type: ResourceType) -> bool:
        return resource_type is ResourceType.TXT

    def parse(self, source: bytes, metadata: DocumentMetadata) -> ParsedDocument:
        text = _decode_utf8(source, label="TXT")
        if not text.strip():
            return ParsedDocument(resource_type=ResourceType.TXT, segments=())
        return ParsedDocument(
            resource_type=ResourceType.TXT, segments=(ParsedSegment(text=text),)
        )


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


class MarkdownParser:
    """Markdown: split on ATX headings (``#`` .. ``######``). Each
    section's ``section_title`` is the most recently seen heading text;
    content preceding the first heading has ``section_title=None`` —
    reported honestly, never fabricated."""

    def supports(self, resource_type: ResourceType) -> bool:
        return resource_type is ResourceType.MARKDOWN

    def parse(self, source: bytes, metadata: DocumentMetadata) -> ParsedDocument:
        text = _decode_utf8(source, label="Markdown")
        segments: list[ParsedSegment] = []
        current_title: str | None = None
        buffer: list[str] = []
        for line in text.splitlines():
            match = _HEADING_RE.match(line)
            if match:
                content = "\n".join(buffer).strip()
                if content:
                    segments.append(
                        ParsedSegment(text=content, section_title=current_title)
                    )
                buffer = []
                current_title = match.group(2).strip() or None
                continue
            buffer.append(line)
        content = "\n".join(buffer).strip()
        if content:
            segments.append(ParsedSegment(text=content, section_title=current_title))
        return ParsedDocument(
            resource_type=ResourceType.MARKDOWN, segments=tuple(segments)
        )


def _max_json_depth(text: str) -> int:
    """Iterative (never recursive) bracket-depth scan, performed before
    ``json.loads`` is ever called — a pathological deeply-nested document
    is rejected without ever invoking the (recursive) JSON decoder."""

    depth = 0
    max_depth = 0
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch in "}]":
            depth = max(0, depth - 1)
    return max_depth


class JSONParser:
    """JSON: a top-level array becomes one segment per element
    (``paragraph_index`` = element index); a top-level object or scalar
    becomes a single segment. Rejects (never truncates) documents whose
    nesting or element count exceeds a safety bound."""

    def supports(self, resource_type: ResourceType) -> bool:
        return resource_type is ResourceType.JSON

    def parse(self, source: bytes, metadata: DocumentMetadata) -> ParsedDocument:
        text = _decode_utf8(source, label="JSON")
        if _max_json_depth(text) > MAX_JSON_NESTING_DEPTH:
            raise ParsingError(f"JSON nesting exceeds {MAX_JSON_NESTING_DEPTH} levels")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParsingError(f"invalid JSON: {exc}") from exc

        segments: list[ParsedSegment] = []
        if isinstance(data, list):
            if len(data) > MAX_CHUNKS_PER_RESOURCE:
                raise ResourceTooLargeError(
                    "JSON array has too many elements to ingest"
                )
            for idx, item in enumerate(data):
                rendered = json.dumps(item, ensure_ascii=False)
                if rendered.strip():
                    segments.append(ParsedSegment(text=rendered, paragraph_index=idx))
        else:
            rendered = json.dumps(data, ensure_ascii=False)
            if rendered.strip():
                segments.append(ParsedSegment(text=rendered, paragraph_index=0))
        return ParsedDocument(resource_type=ResourceType.JSON, segments=tuple(segments))


class CSVParser:
    """CSV: each data row becomes one segment (``paragraph_index`` = row
    index), rendered as ``"column: value | column: value"``. The header
    row supplies column names; a row with more fields than the header has
    the extras labeled positionally. Enforces a maximum field length and
    row count rather than trusting the input's own size."""

    def supports(self, resource_type: ResourceType) -> bool:
        return resource_type is ResourceType.CSV

    def parse(self, source: bytes, metadata: DocumentMetadata) -> ParsedDocument:
        text = _decode_utf8(source, label="CSV")
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error as exc:
            raise ParsingError(f"malformed CSV: {exc}") from exc

        if not rows:
            return ParsedDocument(resource_type=ResourceType.CSV, segments=())
        if len(rows) > _MAX_CSV_ROWS + 1:
            raise ResourceTooLargeError("CSV has too many rows to ingest")
        for row in rows:
            for field in row:
                if len(field) > _MAX_CSV_FIELD_LENGTH:
                    raise ResourceTooLargeError("CSV field exceeds maximum length")

        header, *data_rows = rows
        segments: list[ParsedSegment] = []
        for idx, row in enumerate(data_rows):
            pairs = []
            for col_idx, value in enumerate(row):
                name = header[col_idx] if col_idx < len(header) else f"column_{col_idx}"
                pairs.append(f"{name}: {value}")
            rendered = " | ".join(pairs)
            if rendered.strip():
                segments.append(ParsedSegment(text=rendered, paragraph_index=idx))
        return ParsedDocument(resource_type=ResourceType.CSV, segments=tuple(segments))


# --------------------------------------------------------------------- #
# PDF — see the module docstring for the scope/limitations of this
# implementation.
# --------------------------------------------------------------------- #

_PDF_HEADER = b"%PDF-"
_OBJ_RE = re.compile(rb"(\d+)\s+\d+\s+obj(.*?)endobj", re.DOTALL)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_REF_RE = re.compile(rb"(\d+)\s+\d+\s+R")
_TYPE_CATALOG_RE = re.compile(rb"/Type\s*/Catalog\b")
_TYPE_PAGE_RE = re.compile(rb"/Type\s*/Page(?!s)\b")
_PAGES_REF_RE = re.compile(rb"/Pages\s+(\d+)\s+\d+\s+R")
_KIDS_RE = re.compile(rb"/Kids\s*\[(.*?)\]", re.DOTALL)
_CONTENTS_REF_RE = re.compile(rb"/Contents\s+(\d+)\s+\d+\s+R")
_CONTENTS_ARRAY_RE = re.compile(rb"/Contents\s*\[(.*?)\]", re.DOTALL)
_FILTER_FLATE_RE = re.compile(
    rb"/Filter\s*(?:/FlateDecode|\[[^\]]*?/FlateDecode[^\]]*?\])"
)
_TEXT_OP_RE = re.compile(
    rb"\((?P<tj_str>(?:[^()\\]|\\.)*)\)\s*Tj"
    rb"|\[(?P<tj_arr>(?:[^\[\]\\]|\\.)*)\]\s*TJ"
)
_PAREN_STRING_RE = re.compile(rb"\((?:[^()\\]|\\.)*\)")

_ESCAPES = {
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"b": b"\b",
    b"f": b"\f",
    b"(": b"(",
    b")": b")",
    b"\\": b"\\",
}


def _safe_inflate(data: bytes, *, max_output_bytes: int) -> bytes:
    """Bounded zlib decompression — defends against a FlateDecode stream
    engineered to decompress to gigabytes from a small input (a
    "pathological PDF" / decompression-bomb attack)."""

    decompressor = zlib.decompressobj()
    output = bytearray()
    pending = data
    max_iterations = len(data) + 16
    for _ in range(max_iterations):
        if not pending:
            break
        budget = max_output_bytes - len(output) + 1
        chunk = decompressor.decompress(pending, budget)
        output.extend(chunk)
        if len(output) > max_output_bytes:
            raise ExtractionError("decompressed PDF stream exceeds safety bound")
        pending = decompressor.unconsumed_tail
        if not chunk and not pending:
            break
    output.extend(decompressor.flush())
    if len(output) > max_output_bytes:
        raise ExtractionError("decompressed PDF stream exceeds safety bound")
    return bytes(output)


def _unescape_pdf_string(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == 0x5C and i + 1 < len(raw):  # backslash
            nxt = raw[i + 1 : i + 2]
            if nxt.isdigit():
                octal = bytearray()
                for b in raw[i + 1 : i + 4]:
                    if bytes([b]).isdigit():
                        octal.append(b)
                    else:
                        break
                try:
                    out.append(int(bytes(octal), 8) & 0xFF)
                except ValueError:
                    pass
                i += 1 + len(octal)
                continue
            replacement = _ESCAPES.get(nxt)
            if replacement is not None:
                out.extend(replacement)
            else:
                out.extend(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return out.decode("latin-1")


def _extract_text_runs(content: bytes) -> list[str]:
    runs: list[str] = []
    for match in _TEXT_OP_RE.finditer(content):
        tj_str = match.group("tj_str")
        if tj_str is not None:
            text = _unescape_pdf_string(tj_str)
            if text:
                runs.append(text)
            continue
        array_body = match.group("tj_arr") or b""
        for str_match in _PAREN_STRING_RE.finditer(array_body):
            text = _unescape_pdf_string(str_match.group(0)[1:-1])
            if text:
                runs.append(text)
    return runs


def _walk_pages(
    num: int,
    objects: dict[int, bytes],
    order: list[int],
    seen: set[int],
    *,
    depth: int = 0,
) -> None:
    if num in seen or depth > 64 or num not in objects:
        return
    seen.add(num)
    body = objects[num]
    if _TYPE_PAGE_RE.search(body):
        order.append(num)
        return
    kids_match = _KIDS_RE.search(body)
    if not kids_match:
        return
    for ref_match in _REF_RE.finditer(kids_match.group(1)):
        _walk_pages(int(ref_match.group(1)), objects, order, seen, depth=depth + 1)


def _resolve_page_order(objects: dict[int, bytes]) -> list[int]:
    catalog_num = next(
        (num for num, body in objects.items() if _TYPE_CATALOG_RE.search(body)), None
    )
    pages_root = None
    if catalog_num is not None:
        match = _PAGES_REF_RE.search(objects[catalog_num])
        if match:
            pages_root = int(match.group(1))

    order: list[int] = []
    if pages_root is not None and pages_root in objects:
        _walk_pages(pages_root, objects, order, set())
    if order:
        return order
    # Fallback for a PDF whose catalog/page-tree we could not resolve:
    # every /Type /Page object in ascending object-number order. This is
    # a best-effort approximation, not a guarantee of true reading order
    # for every producer — documented in the module docstring.
    return sorted(num for num, body in objects.items() if _TYPE_PAGE_RE.search(body))


def _page_content_bytes(page_num: int, objects: dict[int, bytes]) -> bytes:
    body = objects[page_num]
    content_nums: list[int] = []
    single = _CONTENTS_REF_RE.search(body)
    if single:
        content_nums.append(int(single.group(1)))
    else:
        array = _CONTENTS_ARRAY_RE.search(body)
        if array:
            content_nums.extend(
                int(m.group(1)) for m in _REF_RE.finditer(array.group(1))
            )

    combined = bytearray()
    for num in content_nums:
        if num not in objects:
            continue
        stream_match = _STREAM_RE.search(objects[num])
        if not stream_match:
            continue
        raw = stream_match.group(1)
        if _FILTER_FLATE_RE.search(objects[num]):
            try:
                raw = _safe_inflate(raw, max_output_bytes=_MAX_PDF_STREAM_OUTPUT_BYTES)
            except zlib.error:
                continue
        combined.extend(raw)
        combined.extend(b"\n")
    return bytes(combined)


class PDFParser:
    """A minimal, bounded, standard-library-only PDF text/page extractor.

    Recovers page order by walking ``/Root`` → ``/Pages`` → ``/Kids``
    (falling back to ascending object-number order for a PDF whose
    catalog cannot be resolved), decompresses FlateDecode content
    streams with a hard output bound, and extracts literal text from
    ``Tj``/``TJ`` operators. It does **not** implement encryption,
    CID/Type0 font decoding, or layout reconstruction — text from such a
    PDF may come out incomplete or out of order rather than perfectly
    faithful. A page with genuinely no extractable text (a scanned/
    image-only page) simply contributes no segment — see
    ``ParsedDocument.has_extractable_text``, which the ingestion pipeline
    checks before ever treating a resource as successfully learned.
    """

    def supports(self, resource_type: ResourceType) -> bool:
        return resource_type is ResourceType.PDF

    def parse(self, source: bytes, metadata: DocumentMetadata) -> ParsedDocument:
        if _PDF_HEADER not in source[:1024]:
            raise ParsingError("not a valid PDF: missing %PDF- header")

        objects: dict[int, bytes] = {}
        for match in _OBJ_RE.finditer(source):
            if len(objects) >= _MAX_PDF_OBJECTS:
                raise ResourceTooLargeError(
                    "PDF contains too many objects to parse safely"
                )
            objects[int(match.group(1))] = match.group(2)
        if not objects:
            raise ParsingError("malformed PDF: no objects found")

        page_order = _resolve_page_order(objects)
        if not page_order:
            raise ParsingError("malformed PDF: no pages found")

        segments: list[ParsedSegment] = []
        for page_number, obj_num in enumerate(page_order, start=1):
            try:
                content = _page_content_bytes(obj_num, objects)
            except ExtractionError:
                raise
            except Exception:  # noqa: BLE001 - a single malformed page must not abort the document
                content = b""
            text = " ".join(_extract_text_runs(content)).strip()
            if text:
                segments.append(ParsedSegment(text=text, page_number=page_number))
        return ParsedDocument(resource_type=ResourceType.PDF, segments=tuple(segments))


def default_parsers() -> tuple[DocumentParser, ...]:
    """The parser set wired in by default — order does not matter since
    each ``ResourceType`` is supported by exactly one of these."""

    return (TextParser(), MarkdownParser(), JSONParser(), CSVParser(), PDFParser())


def select_parser(
    resource_type: ResourceType, parsers: tuple[DocumentParser, ...]
) -> DocumentParser:
    for parser in parsers:
        if parser.supports(resource_type):
            return parser
    raise ParsingError(f"no parser supports resource type {resource_type.value}")


__all__ = [
    "CSVParser",
    "DocumentParser",
    "JSONParser",
    "MarkdownParser",
    "MAX_JSON_NESTING_DEPTH",
    "PDFParser",
    "TextParser",
    "default_parsers",
    "select_parser",
]
