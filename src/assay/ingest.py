"""Turn uploads and fetched pages into text, then into chunks that keep exact char offsets.

Chunk text is always `source_text[start:end]`, so a citation can point at the exact span.
Token counts are whitespace words, a close enough proxy for the ~350 token target.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from bisect import bisect_right
from pathlib import PurePath

import trafilatura
from docx import Document
from pypdf import PdfReader

from assay.domain import Chunk
from assay.settings import Settings

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".html", ".htm", ".md", ".markdown", ".txt", ".csv"}
MAX_CSV_ROWS = 50_000

_WORD = re.compile(r"\S+")
_BLANK_RUN = re.compile(r"\n[ \t]*\n\s*")
_HEADING = re.compile(r"^(#{1,6}[ \t]+\S.*|={2,}[ \t]*\S.*?={2,})[ \t]*$", re.M)


class IngestError(ValueError):
    """An upload this service will not take; the message names the field and the limit."""


def extract_text(filename: str, payload: bytes, settings: Settings) -> str:
    if len(payload) > settings.max_upload_bytes:
        raise IngestError(
            f"file {filename!r} is {len(payload)} bytes; the upload limit is {settings.max_upload_bytes} bytes "
            f"({settings.max_upload_bytes // (1024 * 1024)} MB)"
        )
    extension = PurePath(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise IngestError(
            f"file {filename!r} has extension {extension or '(none)'}; supported: {sorted(SUPPORTED_EXTENSIONS)}"
        )
    if not payload:
        raise IngestError(f"file {filename!r} is empty")
    if extension == ".pdf":
        text = _pdf_text(filename, payload, settings.max_pdf_pages)
    elif extension == ".docx":
        text = _docx_text(filename, payload)
    elif extension in (".html", ".htm"):
        text = trafilatura.extract(payload.decode("utf-8", errors="replace"), include_tables=True) or ""
    elif extension == ".csv":
        text = _csv_text(filename, payload)
    else:
        text = payload.decode("utf-8", errors="replace")
    if not text.strip():
        raise IngestError(f"file {filename!r} contains no extractable text")
    return text


def _pdf_text(filename: str, payload: bytes, max_pages: int) -> str:
    try:
        reader = PdfReader(io.BytesIO(payload))
        page_count = len(reader.pages)
    except Exception as exc:
        raise IngestError(f"file {filename!r} is not a readable PDF: {exc}") from exc
    if page_count > max_pages:
        raise IngestError(f"file {filename!r} has {page_count} pages; the PDF limit is {max_pages} pages")
    # Each page becomes its own block so chunk boundaries prefer page breaks.
    return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)


def _docx_text(filename: str, payload: bytes) -> str:
    try:
        document = Document(io.BytesIO(payload))
    except Exception as exc:
        raise IngestError(f"file {filename!r} is not a readable DOCX: {exc}") from exc
    blocks = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        if style.lower().startswith("heading"):
            level = style.split()[-1] if style.split()[-1].isdigit() else "1"
            blocks.append("#" * min(int(level), 6) + " " + text)
        else:
            blocks.append(text)
    return "\n\n".join(blocks)


def _csv_text(filename: str, payload: bytes) -> str:
    reader = csv.reader(io.StringIO(payload.decode("utf-8-sig", errors="replace")))
    try:
        header = next(reader)
    except StopIteration:
        return ""
    rows = []
    for count, row in enumerate(reader, start=1):
        if count > MAX_CSV_ROWS:
            raise IngestError(f"file {filename!r} has more than {MAX_CSV_ROWS} rows; the CSV limit is {MAX_CSV_ROWS}")
        cells = [f"{name.strip()}: {value.strip()}" for name, value in zip(header, row, strict=False) if value.strip()]
        if cells:
            rows.append("; ".join(cells))
    return "\n\n".join(rows)


def _block_starts(text: str) -> tuple[list[int], list[tuple[int, str]]]:
    """Char positions where a paragraph or heading begins, and the headings with their positions."""
    starts = {0}
    starts.update(m.end() for m in _BLANK_RUN.finditer(text))
    headings = [(m.start(), m.group(0).strip("#= \t")) for m in _HEADING.finditer(text)]
    starts.update(pos for pos, _ in headings)
    return sorted(starts), headings


def chunk_text(source_id: int, text: str, *, chunk_tokens: int, overlap: int) -> list[Chunk]:
    if overlap >= chunk_tokens:
        raise ValueError(f"chunk overlap ({overlap}) must be smaller than chunk size ({chunk_tokens})")
    words = [(m.start(), m.end()) for m in _WORD.finditer(text)]
    if not words:
        return []
    starts, headings = _block_starts(text)
    heading_positions = [pos for pos, _ in headings]
    # A word opens a block when a block start falls between the previous word and this one.
    opens_block = []
    previous_end = -1
    for start, end in words:
        k = bisect_right(starts, start)
        opens_block.append(k > 0 and starts[k - 1] > previous_end)
        previous_end = end
    boundary_words = [i for i, opens in enumerate(opens_block) if opens]

    chunks: list[Chunk] = []
    i = 0
    n = len(words)
    while i < n:
        limit = min(i + chunk_tokens, n)
        j = limit
        if limit < n:
            # Prefer to end at a paragraph or heading, if one falls in the back half of the window.
            k = bisect_right(boundary_words, limit) - 1
            if k >= 0 and boundary_words[k] > i + chunk_tokens // 2:
                j = boundary_words[k]
        start_char, end_char = words[i][0], words[j - 1][1]
        h = bisect_right(heading_positions, start_char) - 1
        chunks.append(
            Chunk(
                id=f"s{source_id}:c{len(chunks)}",
                source_id=source_id,
                start=start_char,
                end=end_char,
                heading=headings[h][1] if h >= 0 else "",
                text=text[start_char:end_char],
            )
        )
        if j >= n:
            break
        i = max(j - overlap, i + 1)
    return chunks
