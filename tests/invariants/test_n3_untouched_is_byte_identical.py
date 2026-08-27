"""N3: the system must never modify a register entry that the new evidence
did not touch.

An arriving document produces a focused update, not a rewrite and not a
re-run that happens to reproduce the same bytes. The parts the new source did
not affect stay exactly as they were, and the system can prove it: every entry
is content-hashed and independently renderable, so non-modification is
demonstrated by equality rather than asserted in a README.

The counter-test matters as much as the invariant. A system that never changes
anything would satisfy non-modification trivially, so this file also proves
that evidence which *should* move an entry does move it. A fix must not buy
its correctness by wrongly refusing valid work somewhere else.

Breaks this catches: recomposing the whole register on every ingest; an
impact set computed too wide; re-rendering that reorders or reformats
untouched entries; a timestamp or run id leaking into an entry's content hash.
"""

from __future__ import annotations

import pytest

from doctask.corpus import Corpus
from doctask.llm import FakeProvider
from tests.support import (
    AMENDMENT_TEXT,
    INVOICE_TEXT,
    MSA_TEXT,
    UNRELATED_TEXT,
    approve_all,
    classify,
    entry_hashes,
    fact,
    text_doc,
)

ACME_DOCS = [
    text_doc("msa-001", MSA_TEXT),
    text_doc("amd-001", AMENDMENT_TEXT),
    text_doc("inv-001", INVOICE_TEXT),
]
GLOBEX_DOC = text_doc("msa-globex-001", UNRELATED_TEXT)

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("classify", "amd-001"): classify("AMENDMENT"),
    ("classify", "inv-001"): classify("INVOICE"),
    ("classify", "msa-globex-001"): classify("MSA"),
    ("extract", "msa-001"): {
        "facts": [
            fact("payment_terms", "Net 30", MSA_TEXT, "Net 30"),
            fact("hourly_rate", "USD 145", MSA_TEXT, "USD 145 per hour"),
            fact("termination_notice", "90 days", MSA_TEXT, "90 days written notice"),
        ]
    },
    ("extract", "amd-001"): {
        "facts": [fact("payment_terms", "Net 45", AMENDMENT_TEXT, "Net 45")]
    },
    ("extract", "inv-001"): {
        "facts": [fact("invoice_total", "USD 5,800.00", INVOICE_TEXT, "USD 5,800.00")]
    },
    ("extract", "msa-globex-001"): {
        "facts": [fact("payment_terms", "Net 60", UNRELATED_TEXT, "Net 60")]
    },
}


@pytest.fixture
def settled(make_engine):
    """A committed Acme register, plus the engine that produced it."""
    engine = make_engine(FakeProvider(SCRIPT))
    corpus = Corpus.from_documents(ACME_DOCS)
    committed = approve_all(engine, engine.start_run(corpus))
    return engine, corpus, committed


@pytest.mark.requires_db
def test_unrelated_document_leaves_every_existing_entry_hash_intact(settled):
    engine, corpus, before = settled
    before_hashes = entry_hashes(before.register)

    after = approve_all(engine, engine.ingest(corpus, GLOBEX_DOC))

    after_hashes = entry_hashes(after.register)
    for entry_id, digest in before_hashes.items():
        assert after_hashes[entry_id] == digest, f"{entry_id} moved and should not have"


@pytest.mark.requires_db
def test_unrelated_document_leaves_every_existing_entry_byte_identical(settled):
    """Hash equality is necessary but not sufficient: the rendered bytes a
    human reads must also be unchanged."""
    engine, corpus, before = settled
    rendered_before = {e.entry_id: before.register.render_entry(e.entry_id) for e in before.register.entries}

    after = approve_all(engine, engine.ingest(corpus, GLOBEX_DOC))

    for entry_id, text in rendered_before.items():
        assert after.register.render_entry(entry_id) == text


@pytest.mark.requires_db
def test_impact_set_excludes_entries_the_new_document_cannot_affect(settled):
    engine, corpus, before = settled
    existing = set(entry_hashes(before.register))

    update = engine.ingest(corpus, GLOBEX_DOC)

    assert update.impact_set.isdisjoint(existing)


@pytest.mark.requires_db
def test_the_new_document_does_add_its_own_entry(settled):
    """Non-modification must not be achieved by ignoring the arrival."""
    engine, corpus, before = settled

    after = approve_all(engine, engine.ingest(corpus, GLOBEX_DOC))

    globex = [e for e in after.register.entries if e.entry_id not in entry_hashes(before.register)]
    assert [e.value for e in globex if e.field == "payment_terms"] == ["Net 60"]


@pytest.mark.requires_db
def test_related_document_does_move_the_entry_it_bears_on(make_engine):
    """The counter-test. A second Acme invoice bears on the Acme payment
    terms entry, so that entry must land in the impact set. A system that
    refuses all updates fails here."""
    engine = make_engine(FakeProvider(SCRIPT))
    corpus = Corpus.from_documents(ACME_DOCS[:1])
    before = approve_all(engine, engine.start_run(corpus))
    payment_entry = next(e for e in before.register.entries if e.field == "payment_terms")

    update = engine.ingest(corpus, text_doc("amd-001", AMENDMENT_TEXT))

    assert payment_entry.entry_id in update.impact_set


@pytest.mark.requires_db
def test_recomposing_without_new_evidence_changes_nothing(settled):
    """Idempotence. Ingesting a document already in the corpus is a no-op:
    same hashes, and an empty impact set."""
    engine, corpus, before = settled

    update = engine.ingest(corpus, text_doc("msa-001", MSA_TEXT))

    assert update.impact_set == set()
    assert entry_hashes(update.register) == entry_hashes(before.register)


@pytest.mark.requires_db
def test_the_gate_only_asks_about_what_changed(settled):
    """An update must cost like an update in reviewer attention too.

    Ingesting an unrelated document must not put every settled entry back in
    front of a person. Re-approving forty untouched entries to get at the one
    that moved is how a gate degrades into a rubber stamp.
    """
    engine, corpus, before = settled
    settled_ids = set(entry_hashes(before.register))

    update = engine.ingest(corpus, GLOBEX_DOC)

    proposed = {
        i.payload["entry_id"]
        for i in update.review_bundle.items
        if i.kind.value == "entry"
    }
    assert proposed.isdisjoint(settled_ids)
    assert proposed, "the arriving document's own entry must still be proposed"
