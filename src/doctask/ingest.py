"""Turning files into text the rest of the system can cite into.

Every parser returns the same shape: normalised text plus the page boundaries
inside it. Citations are character offsets into that text, so the offsets a
model produces stay meaningful no matter which format they came from.

A format this module does not know is an error, never a skip. A silently
omitted document makes the deliverable quietly incomplete, and "quietly
incomplete" is the failure this whole system exists to prevent.
"""

from __future__ import annotations

import csv
import email
import hashlib
import io
from dataclasses import dataclass
from email import policy
from pathlib import Path

from doctask.errors import UnsupportedFormat

MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".eml": "message/rfc822",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(frozen=True)
class ParsedFile:
    text: str
    media_type: str
    page_starts: tuple[int, ...]


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _parse_plain(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _parse_csv(raw: bytes) -> str:
    """Rendered row by row so a cell keeps a citable character span.

    A spreadsheet is legitimate as a *source*; this system never produces one.
    """
    text = raw.decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)


def _parse_eml(raw: bytes) -> str:
    message = email.message_from_bytes(raw, policy=policy.default)
    headers = [
        f"{name}: {message[name]}"
        for name in ("From", "To", "Date", "Subject")
        if message[name]
    ]
    body = message.get_body(preferencelist=("plain",))
    content = body.get_content() if body is not None else ""
    return "\n".join(headers) + "\n\n" + content


def _parse_docx(raw: bytes) -> str:
    """Paragraphs joined by newlines.

    Kept separate rather than run together so a citation span can never
    straddle two clauses that were not adjacent in the document.
    """
    from docx import Document as DocxDocument

    document = DocxDocument(io.BytesIO(raw))
    return "\n".join(p.text for p in document.paragraphs)


def _parse_pdf(raw: bytes) -> tuple[str, tuple[int, ...]]:
    """Page text plus the offset each page begins at.

    The offsets are what turn a character span into "page 2, line 4" for a
    human checking a citation, so they are computed alongside the text rather
    than guessed from it afterwards.
    """
    import pdfplumber

    pages: list[str] = []
    starts: list[int] = []
    offset = 0
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            starts.append(offset)
            text = page.extract_text() or ""
            pages.append(text)
            offset += len(text) + 1  # +1 for the newline that joins pages

    return "\n".join(pages), tuple(starts)


def parse_bytes(raw: bytes, suffix: str) -> ParsedFile:
    """Parse `raw` according to `suffix`, or refuse to."""
    suffix = suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise UnsupportedFormat(f"no parser for {suffix!r}")

    page_starts: tuple[int, ...] = (0,)
    if suffix in (".txt", ".md"):
        text = _parse_plain(raw)
    elif suffix == ".csv":
        text = _parse_csv(raw)
    elif suffix == ".eml":
        text = _parse_eml(raw)
    elif suffix == ".docx":
        text = _parse_docx(raw)
    elif suffix == ".pdf":
        text, page_starts = _parse_pdf(raw)
    else:  # pragma: no cover - MEDIA_TYPES and this branch stay in step
        raise UnsupportedFormat(f"no parser for {suffix!r}")

    return ParsedFile(text=text, media_type=MEDIA_TYPES[suffix], page_starts=page_starts)


def parse_file(path: Path) -> ParsedFile:
    suffix = path.suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise UnsupportedFormat(f"cannot read {path.name}: no parser for {suffix!r}")
    return parse_bytes(path.read_bytes(), suffix)
