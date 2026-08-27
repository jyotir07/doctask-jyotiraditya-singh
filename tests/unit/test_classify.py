"""Classification is the first place the system's own judgement changes what
happens next, rather than logging a label and carrying on regardless.

Three outcomes, three different paths: a confident recognition proceeds to
extraction, an unconfident one escalates to a person, and a document this
system does not handle is skipped with a reason recorded. A stage with only
one possible outcome is a labelled script, not a decision.
"""

from __future__ import annotations

import pytest

from doctask.classify import classify_document
from doctask.domain import ClassificationOutcome, DocType
from doctask.errors import ClassificationMalformed

FLOOR = 0.6


def payload(doc_type, confidence):
    return {"doc_type": doc_type, "confidence": confidence}


def test_a_confident_recognition_proceeds():
    result = classify_document(payload("MSA", 0.97), confidence_floor=FLOOR)

    assert result.outcome is ClassificationOutcome.ACCEPTED
    assert result.doc_type is DocType.MSA


def test_every_declared_document_type_is_recognised():
    """The domains this system claims to handle, held to that claim."""
    for name in ["MSA", "SOW", "AMENDMENT", "INVOICE", "PURCHASE_ORDER",
                 "CHANGE_ORDER", "CORRESPONDENCE"]:
        result = classify_document(payload(name, 0.95), confidence_floor=FLOOR)

        assert result.outcome is ClassificationOutcome.ACCEPTED, name
        assert result.doc_type.value == name


def test_an_unconfident_recognition_escalates_to_a_person():
    result = classify_document(payload("MSA", 0.4), confidence_floor=FLOOR)

    assert result.outcome is ClassificationOutcome.ESCALATE


def test_the_escalation_says_why():
    """A person picking this up needs to know what the system was unsure of."""
    result = classify_document(payload("MSA", 0.4), confidence_floor=FLOOR)

    assert "0.4" in result.reason
    assert "MSA" in result.reason


def test_confidence_exactly_at_the_floor_is_accepted():
    result = classify_document(payload("MSA", FLOOR), confidence_floor=FLOOR)

    assert result.outcome is ClassificationOutcome.ACCEPTED


def test_confidence_just_below_the_floor_escalates():
    result = classify_document(payload("MSA", FLOOR - 0.01), confidence_floor=FLOOR)

    assert result.outcome is ClassificationOutcome.ESCALATE


def test_a_document_type_outside_the_declared_set_is_skipped():
    """Skipped rather than guessed. Extracting contract fields from a payslip
    would produce confident nonsense."""
    result = classify_document(payload("PAYSLIP", 0.99), confidence_floor=FLOOR)

    assert result.outcome is ClassificationOutcome.SKIP
    assert result.doc_type is DocType.UNKNOWN


def test_the_skip_records_what_was_skipped_and_why():
    result = classify_document(payload("PAYSLIP", 0.99), confidence_floor=FLOOR)

    assert "PAYSLIP" in result.reason


def test_an_unrecognised_type_is_skipped_even_when_confident():
    """High confidence in a type we do not handle is still not a reason to
    proceed. Confidence is about recognition, not about competence."""
    confident = classify_document(payload("PAYSLIP", 1.0), confidence_floor=FLOOR)

    assert confident.outcome is ClassificationOutcome.SKIP


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"doc_type": "MSA"},
        {"confidence": 0.9},
        {"doc_type": "MSA", "confidence": "high"},
        {"doc_type": 7, "confidence": 0.9},
        {"doc_type": "MSA", "confidence": 1.5},
        {"doc_type": "MSA", "confidence": -0.2},
    ],
)
def test_malformed_classification_output_is_rejected(bad):
    with pytest.raises(ClassificationMalformed):
        classify_document(bad, confidence_floor=FLOOR)
