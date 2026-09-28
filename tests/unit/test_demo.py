"""The demo corpora load, and their scripts mean what they claim to."""

from __future__ import annotations

import pytest

from doctask.corpus import Corpus
from doctask.demo import ScriptError, available_corpora, build_script, load_demo
from doctask.domain import Citation, CitationStatus
from doctask.provenance import verify_citation
from tests.support import MSA_TEXT, text_doc


def _corpus():
    return Corpus.from_documents([text_doc("msa", MSA_TEXT)], corpus_id="c")


def test_both_shipped_corpora_are_discovered():
    assert {"acme-v1", "globex-v1"} <= set(available_corpora())


@pytest.mark.parametrize("name", ["acme-v1", "globex-v1"])
def test_every_shipped_script_resolves_against_its_documents(name):
    corpus, script = load_demo(name)
    for doc_ in corpus.documents:
        assert ("classify", doc_.doc_id) in script


def test_an_honest_fact_resolves_to_a_span_that_verifies():
    spec = {"documents": {"msa": {"doc_type": "MSA", "facts": [
        {"field": "payment_terms", "value": "Net 30", "quote": "Net 30 days"}]}}}
    fact = build_script(_corpus(), spec)[("extract", "msa")]["facts"][0]
    citation = Citation("msa", fact["char_start"], fact["char_end"], fact["quoted_text"])
    assert verify_citation(MSA_TEXT, citation) is CitationStatus.VERIFIED


def test_a_fabricated_fact_points_at_its_anchor_and_fails_verification():
    spec = {"documents": {"msa": {"doc_type": "MSA", "fabricated": [
        {"field": "payment_terms", "value": "Net 10",
         "anchor": "Net 30 days", "quote": "Net 10 days"}]}}}
    fact = build_script(_corpus(), spec)[("extract", "msa")]["facts"][0]
    citation = Citation("msa", fact["char_start"], fact["char_end"], fact["quoted_text"])
    assert MSA_TEXT[fact["char_start"]:fact["char_end"]] == "Net 30 days"
    assert verify_citation(MSA_TEXT, citation) is CitationStatus.SPAN_MISMATCH


def test_a_fabricated_fact_whose_quote_is_really_there_is_refused():
    spec = {"documents": {"msa": {"doc_type": "MSA", "fabricated": [
        {"field": "payment_terms", "value": "Net 30",
         "anchor": "Net 30 days", "quote": "Net 30 days"}]}}}
    with pytest.raises(ScriptError):
        build_script(_corpus(), spec)


def test_a_quote_missing_from_its_document_is_refused():
    spec = {"documents": {"msa": {"doc_type": "MSA", "facts": [
        {"field": "payment_terms", "value": "Net 99", "quote": "Net 99 days"}]}}}
    with pytest.raises(ScriptError):
        build_script(_corpus(), spec)
