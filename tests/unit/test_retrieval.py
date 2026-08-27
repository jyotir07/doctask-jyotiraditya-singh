"""Finding the passages a judged rule needs to see.

Vector search earns its place here rather than in entity resolution: "does
this agreement renew automatically" is a genuinely semantic question over
prose, and a long contract cannot be handed to a model whole.

The property that matters most is the last one. A chunk carries the character
offsets it was cut from, so a citation a model produces while reading a
retrieved passage still verifies against the original document. Retrieval that
lost those offsets would quietly break N1 for every model-backed rule.
"""

from __future__ import annotations

import pytest

from doctask.domain import Citation, CitationStatus
from doctask.llm import FakeProvider, fake_embedding
from doctask.provenance import verify_citation
from doctask.retrieval import VectorIndex, chunk_text
from tests.conftest import TEST_DSN
from tests.support import MSA_TEXT, text_doc

RENEWAL_TEXT = """MASTER SERVICES AGREEMENT

1. SCOPE
The Supplier shall provide professional services as described in each SOW.

2. TERM AND RENEWAL
This agreement renews automatically for successive twelve month periods
unless either party gives written notice ninety days before expiry.

3. PAYMENT TERMS
Customer shall pay all undisputed invoices Net 30 days from the invoice date.

4. INSURANCE
The Supplier shall maintain public liability cover of not less than GBP 5m.
"""


# --- chunking ---------------------------------------------------------------


def test_a_chunk_knows_where_it_came_from():
    """The load-bearing property: chunk text is exactly the source slice."""
    chunks = chunk_text(RENEWAL_TEXT, size=200, overlap=40)

    for c in chunks:
        assert RENEWAL_TEXT[c.char_start:c.char_end] == c.text


def test_chunks_cover_the_whole_document():
    chunks = chunk_text(RENEWAL_TEXT, size=200, overlap=40)

    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(RENEWAL_TEXT)


def test_chunks_overlap_so_a_clause_is_not_cut_in_half():
    chunks = chunk_text(RENEWAL_TEXT, size=200, overlap=40)

    assert len(chunks) > 1
    for earlier, later in zip(chunks, chunks[1:]):
        assert later.char_start < earlier.char_end


def test_a_short_document_is_one_chunk():
    chunks = chunk_text("Net 30 days.", size=200, overlap=40)

    assert len(chunks) == 1
    assert chunks[0].text == "Net 30 days."


# --- the fake embedding is a real instrument --------------------------------


def test_the_fake_embedding_is_deterministic():
    assert fake_embedding("Net 30 days") == fake_embedding("Net 30 days")


def test_the_fake_embedding_carries_lexical_similarity():
    """Otherwise retrieval tests would prove nothing at all.

    A hash-only embedding would make every passage equidistant, and a search
    that returned an arbitrary chunk would still pass.
    """
    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    renewal = fake_embedding("this agreement renews automatically each year")
    similar = fake_embedding("the agreement renews automatically")
    unrelated = fake_embedding("public liability insurance cover")

    assert dot(renewal, similar) > dot(renewal, unrelated)


# --- retrieval --------------------------------------------------------------


@pytest.fixture
def index(store):
    return VectorIndex(store, FakeProvider({}))


def index_small(index, doc):
    """Index with a small window.

    The sample contracts here are short enough to read in a test file, which
    makes them shorter than the production chunk size. Passing the window
    explicitly exercises real multi-chunk behaviour without padding the
    fixtures with filler prose.
    """
    return index.index_document(doc, size=200, overlap=40)


@pytest.mark.requires_db
def test_indexing_a_document_stores_its_chunks(index):
    doc = text_doc("msa-001", RENEWAL_TEXT)

    index_small(index, doc)

    assert index.chunk_count(doc.sha256) > 1


@pytest.mark.requires_db
def test_indexing_the_same_bytes_twice_does_not_duplicate(index):
    """Content addressing again: re-indexing is free and idempotent."""
    doc = text_doc("msa-001", RENEWAL_TEXT)

    index_small(index, doc)
    first = index.chunk_count(doc.sha256)
    index_small(index, doc)

    assert index.chunk_count(doc.sha256) == first


@pytest.mark.requires_db
def test_search_finds_the_relevant_passage(index):
    index_small(index, text_doc("msa-001", RENEWAL_TEXT))

    hits = index.search("does this agreement renew automatically", limit=1)

    assert "renews automatically" in hits[0].text


@pytest.mark.requires_db
def test_search_does_not_return_everything(index):
    index_small(index, text_doc("msa-001", RENEWAL_TEXT))

    hits = index.search("automatic renewal", limit=2)

    assert len(hits) == 2


@pytest.mark.requires_db
def test_search_can_be_scoped_to_one_document(index):
    index_small(index, text_doc("msa-001", RENEWAL_TEXT))
    index_small(index, text_doc("msa-002", MSA_TEXT))

    hits = index.search("payment terms", limit=5, doc_ids=["msa-002"])

    assert {h.doc_id for h in hits} == {"msa-002"}


@pytest.mark.requires_db
def test_a_citation_into_a_retrieved_passage_still_verifies(index):
    """The tie back to N1.

    A model reads a retrieved chunk and cites a span inside it. That span must
    verify against the *original document*, or every model-backed rule would
    produce findings that cannot be checked.
    """
    doc = text_doc("msa-001", RENEWAL_TEXT)
    index_small(index, doc)

    hit = index.search("does this agreement renew automatically", limit=1)[0]
    quote = "renews automatically"
    offset_in_chunk = hit.text.index(quote)
    citation = Citation(
        doc_id=doc.doc_id,
        char_start=hit.char_start + offset_in_chunk,
        char_end=hit.char_start + offset_in_chunk + len(quote),
        quoted_text=quote,
    )

    assert verify_citation(doc.text, citation) is CitationStatus.VERIFIED
