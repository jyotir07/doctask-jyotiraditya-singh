"""N4: the system must never commit an item the human did not approve, and
never let one rejection discard the other decisions.

A person reviews what the system intends to do, approves what is right and
rejects what is wrong in the same review, item by item, and the system
respects every decision.

Breaks this catches: applying the whole bundle when any item is approved;
discarding the bundle when any item is rejected; committing items left
undecided; losing the rejection reason; accepting a decision for an item that
was never proposed.
"""

from __future__ import annotations

import pytest

from doctask.corpus import Corpus
from doctask.domain import Decision, ReviewDecision, RunStatus
from doctask.errors import UnknownReviewItem
from doctask.llm import FakeProvider
from tests.support import (
    AMENDMENT_TEXT,
    INVOICE_TEXT,
    MSA_TEXT,
    classify,
    fact,
    text_doc,
)

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("classify", "amd-001"): classify("AMENDMENT"),
    ("classify", "inv-001"): classify("INVOICE"),
    ("extract", "msa-001"): {
        "facts": [
            fact("payment_terms", "Net 30", MSA_TEXT, "Net 30"),
            fact("hourly_rate", "USD 145", MSA_TEXT, "USD 145 per hour"),
            fact("termination_notice", "90 days", MSA_TEXT, "90 days written notice"),
        ]
    },
    ("extract", "amd-001"): {"facts": [fact("payment_terms", "Net 45", AMENDMENT_TEXT, "Net 45")]},
    ("extract", "inv-001"): {
        "facts": [fact("invoice_total", "USD 5,800.00", INVOICE_TEXT, "USD 5,800.00")]
    },
}


@pytest.fixture
def gated(make_engine):
    engine = make_engine(FakeProvider(SCRIPT))
    corpus = Corpus.from_documents(
        [
            text_doc("msa-001", MSA_TEXT),
            text_doc("amd-001", AMENDMENT_TEXT),
            text_doc("inv-001", INVOICE_TEXT),
        ]
    )
    run = engine.start_run(corpus)
    return engine, run


@pytest.mark.requires_db
def test_run_stops_at_the_gate_before_committing_anything(gated):
    engine, run = gated

    assert run.status is RunStatus.AWAITING_REVIEW
    assert len(run.review_bundle.items) >= 3
    assert engine.committed_register(run.corpus_id) is None


@pytest.mark.requires_db
def test_mixed_decisions_are_each_respected_in_one_review(gated):
    """Approve, reject, approve -- submitted together, honoured separately."""
    engine, run = gated
    first, second, third = run.review_bundle.items[:3]

    committed = engine.submit_decisions(
        run.run_id,
        [
            ReviewDecision(item_id=first.item_id, decision=Decision.APPROVE, reason=None),
            ReviewDecision(item_id=second.item_id, decision=Decision.REJECT, reason="wrong party"),
            ReviewDecision(item_id=third.item_id, decision=Decision.APPROVE, reason=None),
        ],
    )

    applied = {i.item_id for i in committed.review_bundle.items if i.applied}
    assert first.item_id in applied
    assert third.item_id in applied
    assert second.item_id not in applied


@pytest.mark.requires_db
def test_one_rejection_does_not_discard_the_other_decisions(gated):
    """The whole bundle must not be thrown away because one item was wrong."""
    engine, run = gated
    items = run.review_bundle.items
    rejected, *approved = items

    committed = engine.submit_decisions(
        run.run_id,
        [ReviewDecision(item_id=rejected.item_id, decision=Decision.REJECT, reason="not ours")]
        + [
            ReviewDecision(item_id=i.item_id, decision=Decision.APPROVE, reason=None)
            for i in approved
        ],
    )

    applied = {i.item_id for i in committed.review_bundle.items if i.applied}
    assert applied == {i.item_id for i in approved}


@pytest.mark.requires_db
def test_rejection_reason_is_recorded(gated):
    engine, run = gated
    target = run.review_bundle.items[0]

    committed = engine.submit_decisions(
        run.run_id,
        [ReviewDecision(item_id=target.item_id, decision=Decision.REJECT, reason="superseded in 2023")],
    )

    stored = next(i for i in committed.review_bundle.items if i.item_id == target.item_id)
    assert stored.decision is Decision.REJECT
    assert stored.reason == "superseded in 2023"


@pytest.mark.requires_db
def test_undecided_items_are_not_committed(gated):
    """Silence is not approval."""
    engine, run = gated
    decided = run.review_bundle.items[0]

    committed = engine.submit_decisions(
        run.run_id,
        [ReviewDecision(item_id=decided.item_id, decision=Decision.APPROVE, reason=None)],
    )

    applied = {i.item_id for i in committed.review_bundle.items if i.applied}
    assert applied == {decided.item_id}


@pytest.mark.requires_db
def test_a_decision_for_an_unknown_item_is_refused(gated):
    """The gate is not a place to smuggle in work nobody proposed."""
    engine, run = gated

    with pytest.raises(UnknownReviewItem):
        engine.submit_decisions(
            run.run_id,
            [ReviewDecision(item_id="item-that-was-never-proposed", decision=Decision.APPROVE, reason=None)],
        )


@pytest.mark.requires_db
def test_rejected_content_is_absent_from_the_deliverable(gated):
    """The end-to-end consequence: what was rejected does not reach the page."""
    engine, run = gated
    rate_item = next(i for i in run.review_bundle.items if "USD 145" in i.summary)

    committed = engine.submit_decisions(
        run.run_id,
        [ReviewDecision(item_id=rate_item.item_id, decision=Decision.REJECT, reason="rate renegotiated")],
    )

    assert "USD 145" not in committed.register.render()
