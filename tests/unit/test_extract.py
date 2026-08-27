"""Turning raw model output into facts that survived verification.

This is where N1 stops being a pure function and becomes a stage. Every fact
the model proposes is checked against the document bytes before it is allowed
to become a fact at all, and what fails is kept -- as an unsupported fact with
a reason -- rather than discarded silently.

Keeping the rejects matters. A system that quietly drops what it could not
verify looks identical, from the outside, to one that never hallucinated.
"""

from __future__ import annotations

import pytest

from doctask.domain import CitationStatus
from doctask.errors import ExtractionMalformed
from doctask.extract import parse_extraction
from tests.support import MSA_TEXT, fabricated_fact, fact, span_of, text_doc

DOC = text_doc("msa-001", MSA_TEXT)


def payload(*facts):
    return {"facts": list(facts)}


def test_a_verified_fact_becomes_a_fact():
    result = parse_extraction(payload(fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")), DOC)

    assert [(f.field, f.value) for f in result.facts] == [("payment_terms", "Net 30")]
    assert result.unsupported == ()


def test_the_fact_carries_the_span_it_was_verified_against():
    result = parse_extraction(payload(fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")), DOC)

    citation = result.facts[0].citation
    assert (citation.char_start, citation.char_end) == span_of(MSA_TEXT, "Net 30")
    assert citation.doc_id == "msa-001"


def test_a_fabricated_fact_is_rejected():
    invented = fabricated_fact("payment_terms", "Net 90", MSA_TEXT, decoy="90 days written notice")

    result = parse_extraction(payload(invented), DOC)

    assert result.facts == ()
    assert [u.status for u in result.unsupported] == [CitationStatus.SPAN_MISMATCH]


def test_a_rejected_fact_keeps_its_value_and_reason():
    """Kept, not discarded: an honest report of what could not be supported."""
    invented = fabricated_fact("late_fee", "12% per annum", MSA_TEXT, decoy="TERMINATION")

    result = parse_extraction(payload(invented), DOC)

    dropped = result.unsupported[0]
    assert dropped.field == "late_fee"
    assert dropped.value == "12% per annum"
    assert dropped.doc_id == "msa-001"


def test_a_span_past_the_end_of_the_document_is_rejected():
    beyond = {
        "field": "payment_terms",
        "value": "Net 30",
        "char_start": len(MSA_TEXT) + 10,
        "char_end": len(MSA_TEXT) + 20,
        "quoted_text": "Net 30",
    }

    result = parse_extraction(payload(beyond), DOC)

    assert [u.status for u in result.unsupported] == [CitationStatus.OUT_OF_BOUNDS]


def test_good_and_bad_facts_are_partitioned():
    truthful = fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")
    invented = fabricated_fact("late_fee", "12%", MSA_TEXT, decoy="TERMINATION")

    result = parse_extraction(payload(truthful, invented), DOC)

    assert [f.field for f in result.facts] == ["payment_terms"]
    assert [u.field for u in result.unsupported] == ["late_fee"]


def test_a_document_with_no_facts_is_not_an_error():
    """Correspondence often carries nothing extractable. That is a real
    outcome, not a failure."""
    result = parse_extraction(payload(), DOC)

    assert result.facts == ()
    assert result.unsupported == ()


# --- malformed model output ------------------------------------------------


@pytest.mark.parametrize(
    "bad,label",
    [
        ({}, "no facts key"),
        ({"facts": "Net 30"}, "facts is not a list"),
        ({"facts": [{"value": "Net 30"}]}, "fact missing field"),
        ({"facts": [{"field": "payment_terms"}]}, "fact missing value"),
        ({"facts": [{"field": "f", "value": "v", "char_start": "x", "char_end": 3,
                     "quoted_text": "q"}]}, "offset is not an integer"),
        ({"facts": [{"field": "f", "value": "v", "quoted_text": "q"}]}, "no span at all"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_malformed_output_is_rejected_loudly(bad, label):
    """The repair loop needs to know the difference between a model that
    produced a wrong answer and one that produced no answer at all."""
    with pytest.raises(ExtractionMalformed):
        parse_extraction(bad, DOC)


# --- identity --------------------------------------------------------------


def test_the_same_fact_gets_the_same_id_every_time():
    """Entry content hashes are built from facts. An id that moved between
    runs would make every re-run look like a change."""
    first = parse_extraction(payload(fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")), DOC)
    second = parse_extraction(payload(fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")), DOC)

    assert first.facts[0].fact_id == second.facts[0].fact_id


def test_different_facts_get_different_ids():
    result = parse_extraction(
        payload(
            fact("payment_terms", "Net 30", MSA_TEXT, "Net 30"),
            fact("hourly_rate", "USD 145", MSA_TEXT, "USD 145 per hour"),
        ),
        DOC,
    )

    assert result.facts[0].fact_id != result.facts[1].fact_id


def test_two_values_for_the_same_field_get_different_ids():
    """The hard case. An id built only from document and field would collide
    here, and two genuinely different claims would become one entry."""
    result = parse_extraction(
        payload(
            fact("payment_terms", "Net 30", MSA_TEXT, "Net 30"),
            fact("payment_terms", "90 days", MSA_TEXT, "90 days written notice"),
        ),
        DOC,
    )

    assert result.facts[0].fact_id != result.facts[1].fact_id
