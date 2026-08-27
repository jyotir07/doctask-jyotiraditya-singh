"""Generate the PDF and DOCX members of each demo corpus.

The declared format list includes PDF and DOCX, so the demo corpora contain
real ones rather than a note promising they would work. Generated rather than
hand-committed so a reviewer can see exactly what is inside them.

    python corpora/build_binaries.py
"""

from __future__ import annotations

from pathlib import Path

from docx import Document as Docx
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = Path(__file__).parent

APPENDIX = [
    "APPENDIX A TO MASTER SERVICES AGREEMENT",
    "",
    "Supplier: Acme Industrial Supply Ltd",
    "Customer: Northwind Trading Co",
    "",
    "A.1 INSURANCE",
    "The Supplier shall maintain public liability cover of not less than",
    "GBP 5,000,000 for the duration of the Agreement.",
    "",
    "A.2 RATES",
    "The agreed rate for principal consultants is USD 145 per hour.",
]

AMENDMENT_2 = [
    "AMENDMENT NO. 2 TO MASTER SERVICES AGREEMENT",
    "",
    "Parties: Acme Industrial Supply Ltd and Northwind Trading Co",
    "Effective date: 1 January 2025",
    "",
    "1. LIABILITY CAP",
    "The liability cap in Section 5 is raised to GBP 250,000.",
    "",
    "2. PAYMENT TERMS",
    "Payment terms are unchanged and remain Net 45 days from the invoice date.",
]

GLOBEX_SCHEDULE = [
    "SCHEDULE 1 TO LOGISTICS SERVICES AGREEMENT",
    "",
    "Supplier: Globex Logistics Limited",
    "Customer: Initech Retail Co",
    "",
    "S1.1 RATES",
    "The agreed rate for senior logistics planners is USD 210 per hour.",
    "",
    "S1.2 PAYMENT",
    "Payment terms are Net 60 days from the invoice date.",
]


def write_pdf(path: Path, lines: list[str]) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    text = c.beginText(60, 780)
    for line in lines:
        text.textLine(line)
    c.drawText(text)
    c.showPage()
    c.save()


def write_docx(path: Path, lines: list[str]) -> None:
    doc = Docx()
    for line in lines:
        doc.add_paragraph(line)
    doc.save(str(path))


def main() -> None:
    write_pdf(HERE / "acme-v1" / "docs" / "appendix-a.pdf", APPENDIX)
    write_docx(HERE / "acme-v1" / "docs" / "amendment-02.docx", AMENDMENT_2)
    write_pdf(HERE / "globex-v1" / "docs" / "schedule-1.pdf", GLOBEX_SCHEDULE)
    print("wrote appendix-a.pdf, amendment-02.docx, schedule-1.pdf")


if __name__ == "__main__":
    main()
