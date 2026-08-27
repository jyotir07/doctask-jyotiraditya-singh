"""Every format must produce text whose character offsets are citable.

This is the property that ties the parsers to N1. A model reads the parsed
text and returns a span into it; the verifier re-reads that span from the same
text. If a parser's output is not stable and self-consistent, honest citations
start failing verification and the system silently loses facts it should have
kept.

So each parser is tested the same way: parse a real file of that format, find
a phrase in the result, and prove a citation to it verifies.
"""

from __future__ import annotations

import pytest

from doctask.domain import Citation, CitationStatus
from doctask.ingest import parse_bytes, parse_file
from doctask.errors import UnsupportedFormat
from doctask.provenance import verify_citation
from tests.support import span_of

CONTRACT_LINE = "Customer shall pay all undisputed invoices Net 30 days."


def assert_citable(text: str, needle: str, doc_id: str = "d1") -> None:
    """A span located in `text` must verify against `text`."""
    start, end = span_of(text, needle)
    citation = Citation(doc_id=doc_id, char_start=start, char_end=end, quoted_text=needle)
    assert verify_citation(text, citation) is CitationStatus.VERIFIED


# --- text formats ----------------------------------------------------------


def test_plain_text_is_citable():
    parsed = parse_bytes(CONTRACT_LINE.encode("utf-8"), ".txt")

    assert_citable(parsed.text, "Net 30 days")


def test_markdown_keeps_its_prose_citable():
    raw = f"# Payment\n\n{CONTRACT_LINE}\n".encode("utf-8")

    parsed = parse_bytes(raw, ".md")

    assert_citable(parsed.text, "Net 30 days")


def test_csv_cells_are_citable():
    """A spreadsheet is legitimate as a source. Each cell keeps a span."""
    raw = b"invoice,terms,amount\nINV-2024-0311,Net 30,5800.00\n"

    parsed = parse_bytes(raw, ".csv")

    assert_citable(parsed.text, "INV-2024-0311")
    assert_citable(parsed.text, "Net 30")


def test_email_headers_and_body_are_citable():
    raw = (
        b"From: ap@northwind.example\r\n"
        b"To: billing@acme.example\r\n"
        b"Subject: Invoice INV-2024-0311\r\n"
        b"\r\n"
        b"Please note our terms are Net 45 days per the amendment.\r\n"
    )

    parsed = parse_bytes(raw, ".eml")

    assert_citable(parsed.text, "Invoice INV-2024-0311")
    assert_citable(parsed.text, "Net 45 days")


def test_media_type_is_recorded():
    assert parse_bytes(b"x", ".txt").media_type == "text/plain"
    assert parse_bytes(b"a,b", ".csv").media_type == "text/csv"


# --- binary formats --------------------------------------------------------


def _write_docx(path, paragraphs):
    from docx import Document as DocxDocument

    doc = DocxDocument()
    for para in paragraphs:
        doc.add_paragraph(para)
    doc.save(str(path))
    return path


def _write_pdf(path, lines):
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=LETTER)
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    c.save()
    return path


def test_docx_paragraphs_are_citable(tmp_path):
    path = _write_docx(tmp_path / "msa.docx", ["MASTER SERVICES AGREEMENT", CONTRACT_LINE])

    parsed = parse_file(path)

    assert_citable(parsed.text, "Net 30 days")


def test_docx_keeps_paragraphs_separate(tmp_path):
    """Paragraphs must not run together, or a span can straddle two clauses
    that were never adjacent in the document."""
    path = _write_docx(tmp_path / "msa.docx", ["First clause.", "Second clause."])

    parsed = parse_file(path)

    assert "First clause.\nSecond clause." in parsed.text


def test_pdf_text_is_citable(tmp_path):
    path = _write_pdf(tmp_path / "msa.pdf", ["MASTER SERVICES AGREEMENT", CONTRACT_LINE])

    parsed = parse_file(path)

    assert_citable(parsed.text, "Net 30 days")


def test_pdf_records_where_each_page_starts(tmp_path):
    """Page boundaries are what turn a character offset into 'page 2, line 4'
    for a human reading the citation."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    path = tmp_path / "three-page.pdf"
    c = canvas.Canvas(str(path), pagesize=LETTER)
    for line in ["Page one clause.", "Page two clause.", "Page three clause."]:
        c.drawString(72, 720, line)
        c.showPage()
    c.save()

    parsed = parse_file(path)

    # Each offset must land exactly on the first character of its page. An
    # assertion that the text merely appears somewhere after the offset would
    # tolerate a drift of one per page, which compounds across a long document
    # until citations point at the wrong page entirely.
    assert len(parsed.page_starts) == 3
    assert parsed.text[parsed.page_starts[0]:].startswith("Page one clause.")
    assert parsed.text[parsed.page_starts[1]:].startswith("Page two clause.")
    assert parsed.text[parsed.page_starts[2]:].startswith("Page three clause.")


# --- refusing what it cannot read ------------------------------------------


def test_an_unknown_format_is_refused():
    with pytest.raises(UnsupportedFormat):
        parse_bytes(b"\x00\x01", ".xyz")


def test_the_refusal_names_the_format():
    with pytest.raises(UnsupportedFormat) as raised:
        parse_bytes(b"\x00\x01", ".xlsx")

    assert ".xlsx" in str(raised.value)
