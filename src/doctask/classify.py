"""Working out what each document is, and deciding what that means.

The first stage whose verdict changes the path. Three outcomes: proceed,
escalate to a person, or skip with a recorded reason. A stage with one
possible outcome is a labelled script, not a decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from doctask.domain import ClassificationOutcome, DocType
from doctask.errors import ClassificationMalformed

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": [t.value for t in DocType]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["doc_type", "confidence"],
}


@dataclass(frozen=True)
class Classification:
    doc_type: DocType
    confidence: float
    outcome: ClassificationOutcome
    reason: str | None = None


def classify_document(payload: dict, *, confidence_floor: float) -> Classification:
    """Decide what a document is and what to do about it."""
    if not isinstance(payload, dict):
        raise ClassificationMalformed("classifier output is not an object")

    if "doc_type" not in payload or "confidence" not in payload:
        raise ClassificationMalformed("classifier output needs doc_type and confidence")

    raw_type = payload["doc_type"]
    confidence = payload["confidence"]

    if not isinstance(raw_type, str):
        raise ClassificationMalformed("doc_type must be a string")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ClassificationMalformed("confidence must be a number")
    if not 0.0 <= confidence <= 1.0:
        raise ClassificationMalformed(f"confidence {confidence} is outside 0.0-1.0")

    try:
        doc_type = DocType(raw_type)
    except ValueError:
        return Classification(
            doc_type=DocType.UNKNOWN,
            confidence=float(confidence),
            outcome=ClassificationOutcome.SKIP,
            reason=f"{raw_type} is outside the declared document set",
        )

    if doc_type is DocType.UNKNOWN:
        return Classification(
            doc_type=DocType.UNKNOWN,
            confidence=float(confidence),
            outcome=ClassificationOutcome.SKIP,
            reason="document type could not be determined",
        )

    # Confidence is about recognition, not competence: being certain a
    # document is a payslip is still not a reason to extract contract terms
    # from it, which is why the unknown-type check comes first.
    if confidence < confidence_floor:
        return Classification(
            doc_type=doc_type,
            confidence=float(confidence),
            outcome=ClassificationOutcome.ESCALATE,
            reason=(
                f"classified as {doc_type.value} with confidence {confidence}, "
                f"below the floor of {confidence_floor}"
            ),
        )

    return Classification(
        doc_type=doc_type,
        confidence=float(confidence),
        outcome=ClassificationOutcome.ACCEPTED,
    )
