"""N6: the system must never silently resolve a contradiction between two
sources.

When a new source contradicts what the deliverable already says, the conflict
is surfaced, not silently resolved. The register does not pick a winner on its
own: it records that the sources disagree, cites both sides, and puts the
choice in front of a person.

Breaks this catches: last-writer-wins composition; preferring the newer
document by rule and calling it resolved; dropping the losing citation;
rendering a single confident value where the corpus supports two.
"""

from __future__ import annotations

import pytest

from doctask.corpus import Corpus
from doctask.domain import (
    ConflictKind,
    Decision,
    EntryState,
    ReviewDecision,
    ReviewItemKind,
)
from doctask.llm import FakeProvider
from tests.support import (
    AMENDMENT_TEXT,
    MSA_TEXT,
    approve_all,
    classify,
    fact,
    span_of,
    text_doc,
)

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("classify", "amd-001"): classify("AMENDMENT"),
    ("extract", "msa-001"): {"facts": [fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")]},
    ("extract", "amd-001"): {
        "facts": [fact("payment_terms", "Net 45", AMENDMENT_TEXT, "Net 45")]
    },
}


@pytest.fixture
def disputed(make_engine):
    engine = make_engine(FakeProvider(SCRIPT))
    corpus = Corpus.from_documents(
        [text_doc("msa-001", MSA_TEXT), text_doc("amd-001", AMENDMENT_TEXT)]
    )
    return engine, corpus, engine.start_run(corpus)


@pytest.mark.requires_db
def test_contradiction_between_two_sources_becomes_a_conflict(disputed):
    _, _, run = disputed

    conflicts = [c for c in run.conflicts if c.field == "payment_terms"]
    assert len(conflicts) == 1
    assert conflicts[0].kind is ConflictKind.VALUE_MISMATCH


@pytest.mark.requires_db
def test_the_conflict_cites_both_sides(disputed):
    """Each side of the disagreement traces to the place it came from."""
    _, _, run = disputed
    conflict = next(c for c in run.conflicts if c.field == "payment_terms")

    cited = {(c.doc_id, c.char_start, c.char_end) for c in conflict.citations}
    assert cited == {
        ("msa-001", *span_of(MSA_TEXT, "Net 30")),
        ("amd-001", *span_of(AMENDMENT_TEXT, "Net 45")),
    }


@pytest.mark.requires_db
def test_the_register_does_not_pick_a_winner_on_its_own(disputed):
    _, _, run = disputed

    entry = next(e for e in run.register.entries if e.field == "payment_terms")
    assert entry.state is EntryState.DISPUTED
    assert sorted(entry.candidate_values) == ["Net 30", "Net 45"]


@pytest.mark.requires_db
def test_the_deliverable_shows_the_disagreement_rather_than_one_value(disputed):
    """A reader of the rendered output must be able to see that the sources
    disagree. Rendering only the newer value would hide it."""
    _, _, run = disputed

    rendered = run.register.render()
    assert "Net 30" in rendered
    assert "Net 45" in rendered


@pytest.mark.requires_db
def test_the_conflict_reaches_the_human_gate(disputed):
    _, _, run = disputed

    conflict_items = [i for i in run.review_bundle.items if i.kind is ReviewItemKind.CONFLICT]
    assert len(conflict_items) == 1


@pytest.mark.requires_db
def test_a_person_can_resolve_it_and_only_a_person_can(disputed):
    """The counter-test. Surfacing a conflict must not make it unresolvable:
    a human choosing Net 45 settles it, and nothing else does."""
    engine, _, run = disputed
    item = next(i for i in run.review_bundle.items if i.kind is ReviewItemKind.CONFLICT)

    committed = engine.submit_decisions(
        run.run_id,
        [
            ReviewDecision(
                item_id=item.item_id,
                decision=Decision.APPROVE,
                reason="amendment supersedes",
                resolution="Net 45",
            )
        ],
    )

    entry = next(e for e in committed.register.entries if e.field == "payment_terms")
    assert entry.state is EntryState.ASSERTED
    assert entry.value == "Net 45"


@pytest.mark.requires_db
def test_an_arriving_contradiction_reopens_a_settled_entry(make_engine):
    """The stays-alive half: a new source contradicting the committed
    deliverable surfaces rather than overwriting it."""
    engine = make_engine(FakeProvider(SCRIPT))
    corpus = Corpus.from_documents([text_doc("msa-001", MSA_TEXT)])
    settled = approve_all(engine, engine.start_run(corpus))
    assert next(e for e in settled.register.entries if e.field == "payment_terms").value == "Net 30"

    update = engine.ingest(corpus, text_doc("amd-001", AMENDMENT_TEXT))

    assert any(c.field == "payment_terms" for c in update.conflicts)
    entry = next(e for e in update.register.entries if e.field == "payment_terms")
    assert entry.state is EntryState.DISPUTED
