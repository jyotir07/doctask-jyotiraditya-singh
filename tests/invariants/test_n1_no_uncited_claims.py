"""N1: the system must never publish a claim whose citation does not
re-verify against the source bytes.

This is the anti-hallucination mechanism, and it is mechanical rather than
prompted. Every fact carries (doc_id, char_start, char_end, quoted_text). The
verifier re-reads source[char_start:char_end] and compares. A model that
invents a payment term produces a span that does not hold the quote, and the
invention dies before it reaches the register.

Breaks this catches: deleting the verifier call from the extract stage;
verifying with a substring/fuzzy match loose enough to accept an invention;
publishing an entry whose supporting facts were all dropped.
"""

from __future__ import annotations

import pytest

from doctask.corpus import Corpus
from doctask.domain import Citation, CitationStatus, EntryState
from doctask.llm import FakeProvider
from doctask.provenance import verify_citation
from tests.support import MSA_TEXT, classify, fabricated_fact, fact, span_of, text_doc

# --- The mechanism itself, with hand-derived literals ----------------------

VERIFY_CASES = [
    # (label, source, start, end, quote, expected status)
    ("exact span", "Payment terms are Net 30 days.", 18, 24, "Net 30", CitationStatus.VERIFIED),
    ("quote not at span", "Payment terms are Net 30 days.", 18, 24, "Net 45", CitationStatus.SPAN_MISMATCH),
    ("span past end", "Net 30", 0, 99, "Net 30", CitationStatus.OUT_OF_BOUNDS),
    ("negative start", "Net 30", -1, 6, "Net 30", CitationStatus.OUT_OF_BOUNDS),
    ("inverted span", "Net 30", 4, 2, "Net 30", CitationStatus.OUT_OF_BOUNDS),
    ("empty span", "Net 30", 3, 3, "", CitationStatus.SPAN_MISMATCH),
    # Whitespace normalisation is allowed; changing the words is not.
    ("line break inside quote", "pay Net\n30 days", 4, 11, "Net 30", CitationStatus.VERIFIED),
    ("substring is not enough", "Payment terms are Net 30 days.", 18, 30, "Net 30", CitationStatus.SPAN_MISMATCH),
]


@pytest.mark.parametrize(
    "label,source,start,end,quote,expected",
    VERIFY_CASES,
    ids=[c[0] for c in VERIFY_CASES],
)
def test_verify_citation_holds_the_span_to_the_quote(label, source, start, end, quote, expected):
    citation = Citation(doc_id="d1", char_start=start, char_end=end, quoted_text=quote)

    assert verify_citation(source, citation) is expected


# --- The mechanism wired into a run ---------------------------------------


@pytest.fixture
def msa_corpus():
    return Corpus.from_documents([text_doc("msa-001", MSA_TEXT)])


def _script(*facts):
    return {
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": list(facts)},
    }


@pytest.mark.requires_db
def test_truthful_fact_reaches_the_register(make_engine, msa_corpus):
    """The verifier must not be so strict that it rejects honest extractions."""
    truthful = fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")
    engine = make_engine(FakeProvider(_script(truthful)))

    run = engine.start_run(msa_corpus)

    values = [e.value for e in run.register.entries if e.field == "payment_terms"]
    assert values == ["Net 30"]


@pytest.mark.requires_db
def test_fabricated_fact_never_reaches_the_register(make_engine, msa_corpus):
    invented = fabricated_fact("payment_terms", "Net 90", MSA_TEXT, decoy="90 days written notice")
    engine = make_engine(FakeProvider(_script(invented)))

    run = engine.start_run(msa_corpus)

    assert [e.value for e in run.register.entries if e.field == "payment_terms"] == []


@pytest.mark.requires_db
def test_fabricated_fact_is_recorded_as_unsupported_with_its_reason(make_engine, msa_corpus):
    invented = fabricated_fact("payment_terms", "Net 90", MSA_TEXT, decoy="90 days written notice")
    engine = make_engine(FakeProvider(_script(invented)))

    run = engine.start_run(msa_corpus)

    assert len(run.unsupported_facts) == 1
    dropped = run.unsupported_facts[0]
    assert dropped.field == "payment_terms"
    assert dropped.status is CitationStatus.SPAN_MISMATCH


@pytest.mark.requires_db
def test_an_entry_keeps_only_its_verified_citations(make_engine, msa_corpus):
    """One good fact and one invention about the same field.

    The entry survives on the strength of the verified citation, and the
    invented citation is not carried along with it.
    """
    truthful = fact("hourly_rate", "USD 145", MSA_TEXT, "USD 145 per hour")
    invented = fabricated_fact("hourly_rate", "USD 145", MSA_TEXT, decoy="Either party")
    engine = make_engine(FakeProvider(_script(truthful, invented)))

    run = engine.start_run(msa_corpus)

    entry = next(e for e in run.register.entries if e.field == "hourly_rate")
    assert len(entry.citations) == 1
    verified_span = span_of(MSA_TEXT, "USD 145 per hour")
    assert (entry.citations[0].char_start, entry.citations[0].char_end) == verified_span


@pytest.mark.requires_db
def test_entry_with_no_surviving_citation_is_not_published(make_engine, msa_corpus):
    """Every claim in the deliverable traces to a place in the sources.

    An entry whose only support was dropped must be absent, not present with
    an empty citation list and not present in a degraded state.
    """
    invented = fabricated_fact("termination_notice", "30 days", MSA_TEXT, decoy="Net 30")
    engine = make_engine(FakeProvider(_script(invented)))

    run = engine.start_run(msa_corpus)

    assert all(e.field != "termination_notice" for e in run.register.entries)
    assert all(e.citations for e in run.register.entries)
    assert all(e.state is not EntryState.UNSUPPORTED for e in run.register.entries)


@pytest.mark.requires_db
def test_rendered_deliverable_contains_no_unverified_value(make_engine, msa_corpus):
    """The rendered output is the artefact a human reads. The invented value
    must not appear anywhere in it, including in prose or a footnote."""
    truthful = fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")
    invented = fabricated_fact("late_fee", "12% per annum", MSA_TEXT, decoy="TERMINATION")
    engine = make_engine(FakeProvider(_script(truthful, invented)))

    run = engine.start_run(msa_corpus)

    assert "12% per annum" not in run.register.render()
    assert "Net 30" in run.register.render()
