from __future__ import annotations

import io

import pytest
from docx import Document
from fpdf import FPDF

from assay.ingest import IngestError, chunk_text, extract_text
from assay.settings import Settings
from tests.conftest import running_app

PARAGRAPH = ("Canberra is the capital city of Australia and was chosen as a compromise between Sydney and "
             "Melbourne. ") * 6


def pdf_bytes(pages: int) -> bytes:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=11)
    for page in range(pages):
        pdf.add_page()
        pdf.multi_cell(0, 6, f"Page {page + 1}. {PARAGRAPH}")
    return bytes(pdf.output())


def docx_bytes() -> bytes:
    document = Document()
    document.add_heading("Capital cities", level=1)
    document.add_paragraph(PARAGRAPH)
    document.add_heading("Design", level=2)
    document.add_paragraph("The city was designed by Walter Burley Griffin. " * 10)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


CSV = b"city,country,population\nCanberra,Australia,467000\nOttawa,Canada,1017000\n"
MARKDOWN = ("# Canberra\n\n" + PARAGRAPH + "\n\n## Design\n\n" + "Griffin designed it. " * 40).encode()


@pytest.mark.parametrize(
    ("filename", "payload", "expected"),
    [
        ("capital.pdf", pdf_bytes(3), "Canberra"),
        ("capital.docx", docx_bytes(), "Griffin"),
        ("cities.csv", CSV, "country: Canada"),
        ("capital.md", MARKDOWN, "Griffin designed it"),
    ],
)
def test_formats_produce_chunks_with_exact_offsets(settings: Settings, filename: str, payload: bytes,
                                                   expected: str) -> None:
    text = extract_text(filename, payload, settings)
    chunks = chunk_text(7, text, chunk_tokens=60, overlap=10)
    assert chunks, filename
    assert any(expected in c.text for c in chunks)
    for n, chunk in enumerate(chunks):
        assert chunk.id == f"s7:c{n}"
        assert text[chunk.start:chunk.end] == chunk.text
        assert len(chunk.text.split()) <= 60


def test_chunks_overlap_and_track_headings(settings: Settings) -> None:
    text = extract_text("capital.md", MARKDOWN, settings)
    chunks = chunk_text(1, text, chunk_tokens=60, overlap=10)
    assert len(chunks) > 2
    for before, after in zip(chunks, chunks[1:], strict=False):
        assert after.start < before.end  # overlapping windows
    assert chunks[0].heading == "Canberra"
    assert chunks[-1].heading == "Design"


def test_oversized_upload_is_rejected_naming_the_limit(settings: Settings) -> None:
    small = settings.model_copy(update={"max_upload_bytes": 100})
    with pytest.raises(IngestError, match="upload limit is 100 bytes"):
        extract_text("big.txt", b"x" * 101, small)


def test_pdf_page_cap_names_the_limit(settings: Settings) -> None:
    capped = settings.model_copy(update={"max_pdf_pages": 2})
    with pytest.raises(IngestError, match="PDF limit is 2 pages"):
        extract_text("long.pdf", pdf_bytes(3), capped)


def test_unsupported_extension_names_supported_ones(settings: Settings) -> None:
    with pytest.raises(IngestError, match="supported"):
        extract_text("tool.exe", b"MZ", settings)


async def test_upload_endpoint_rejects_oversized_file_with_413(settings: Settings) -> None:
    small = settings.model_copy(update={"max_upload_bytes": 1024})
    async with running_app(small) as (client, _):
        too_big = await client.post("/documents", data={"session_id": "s1"},
                                    files={"file": ("big.txt", b"a" * 2048, "text/plain")})
        fine = await client.post("/documents", data={"session_id": "s1"},
                                 files={"file": ("notes.md", b"# Notes\n\nCanberra is the capital.", "text/markdown")})
    assert too_big.status_code == 413
    assert "upload limit of 1024 bytes" in too_big.json()["detail"]
    assert fine.status_code == 201 and fine.json()["chars"] > 0
