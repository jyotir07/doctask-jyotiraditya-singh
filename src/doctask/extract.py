"""Turning raw model output into facts that survived verification.

Where N1 stops being a pure function and becomes a stage.

Two kinds of wrongness are handled differently on purpose. Output that does
not match the schema is *malformed* and worth retrying, because the model may
simply have produced badly shaped JSON. Output that is well formed but not
supported by the document is not retried -- it is recorded as unsupported,
with the reason, and kept.

Keeping the rejects is deliberate. A system that quietly drops what it could
not verify looks identical, from the outside, to one that never hallucinated.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from doctask.domain import Citation, CitationStatus, Document, Fact, UnsupportedFact
from doctask.errors import ExtractionMalformed
from doctask.provenance import verify_citation

# The schema handed to the provider. Constraining the output shape is the
# structural half of N2: a document can influence what facts are proposed, but
# it has no way to reach control flow, because the only thing the model is
# permitted to emit is this.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                    "char_start": {"type": "integer"},
                    "char_end": {"type": "integer"},
                    "quoted_text": {"type": "string"},
                },
                "required": ["field", "value", "char_start", "char_end", "quoted_text"],
            },
        }
    },
    "required": ["facts"],
}

REQUIRED_KEYS = ("field", "value", "char_start", "char_end", "quoted_text")


@dataclass(frozen=True)
class ExtractionResult:
    facts: tuple[Fact, ...]
    unsupported: tuple[UnsupportedFact, ...]


def fact_id_for(doc_id: str, field: str, value: str, start: int, end: int) -> str:
    """A stable identity for a fact.

    Derived from content so that re-extracting the same document produces the
    same ids. An id that moved between runs would make every re-run look like
    a change, and non-modification would stop being provable.
    """
    material = f"{doc_id}|{field}|{value}|{start}|{end}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _require_shape(payload: dict) -> list[dict]:
    if not isinstance(payload, dict) or "facts" not in payload:
        raise ExtractionMalformed("extraction output has no 'facts' key")

    raw_facts = payload["facts"]
    if not isinstance(raw_facts, list):
        raise ExtractionMalformed("'facts' is not a list")

    for entry in raw_facts:
        if not isinstance(entry, dict):
            raise ExtractionMalformed("a fact is not an object")
        missing = [k for k in REQUIRED_KEYS if k not in entry]
        if missing:
            raise ExtractionMalformed(f"fact is missing {', '.join(missing)}")
        if not isinstance(entry["char_start"], int) or not isinstance(entry["char_end"], int):
            raise ExtractionMalformed("character offsets must be integers")

    return raw_facts


def parse_extraction(payload: dict, document: Document) -> ExtractionResult:
    """Parse model output into verified facts and honest rejects."""
    raw_facts = _require_shape(payload)

    facts: list[Fact] = []
    unsupported: list[UnsupportedFact] = []

    for entry in raw_facts:
        citation = Citation(
            doc_id=document.doc_id,
            char_start=entry["char_start"],
            char_end=entry["char_end"],
            quoted_text=entry["quoted_text"],
        )
        status = verify_citation(document.text, citation)

        if status is CitationStatus.VERIFIED:
            facts.append(
                Fact(
                    fact_id=fact_id_for(
                        document.doc_id,
                        entry["field"],
                        entry["value"],
                        citation.char_start,
                        citation.char_end,
                    ),
                    doc_id=document.doc_id,
                    field=entry["field"],
                    value=entry["value"],
                    citation=citation,
                )
            )
        else:
            unsupported.append(
                UnsupportedFact(
                    doc_id=document.doc_id,
                    field=entry["field"],
                    value=entry["value"],
                    citation=citation,
                    status=status,
                )
            )

    return ExtractionResult(facts=tuple(facts), unsupported=tuple(unsupported))
