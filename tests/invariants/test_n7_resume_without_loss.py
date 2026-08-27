"""N7: the system must never lose or duplicate finished work across a crash.

Kill the process in the middle of a run and start it again. It continues from
where it left off, and no finished work is lost.

Every test here resumes through a *fresh engine with a fresh provider* over
the same store. That is the whole point: a provider whose call count starts at
zero cannot silently re-do work, so any step the resume repeats shows up as a
call that should not exist. State living in process memory fails these tests
immediately.

Breaks this catches: keeping run state in memory; a checkpointer that replays
a completed node; resume that restarts the run from the top; duplicate entries
after replay; a step journal keyed on something non-deterministic.
"""

from __future__ import annotations

import pytest

from doctask.corpus import Corpus
from doctask.domain import RunStatus
from doctask.kernel import FaultPlan
from doctask.llm import FakeProvider
from tests.support import (
    AMENDMENT_TEXT,
    INVOICE_TEXT,
    MSA_TEXT,
    approve_all,
    classify,
    entry_hashes,
    fact,
    text_doc,
)

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("classify", "amd-001"): classify("AMENDMENT"),
    ("classify", "inv-001"): classify("INVOICE"),
    ("extract", "msa-001"): {"facts": [fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")]},
    ("extract", "amd-001"): {
        "facts": [fact("payment_terms", "Net 45", AMENDMENT_TEXT, "Net 45")]
    },
    ("extract", "inv-001"): {
        "facts": [fact("invoice_total", "USD 5,800.00", INVOICE_TEXT, "USD 5,800.00")]
    },
}


@pytest.fixture
def corpus():
    return Corpus.from_documents(
        [
            text_doc("msa-001", MSA_TEXT),
            text_doc("amd-001", AMENDMENT_TEXT),
            text_doc("inv-001", INVOICE_TEXT),
        ]
    )


@pytest.fixture
def crashed(make_engine, corpus):
    """A run that died after extracting two of three documents."""
    engine = make_engine(FakeProvider(SCRIPT), faults=FaultPlan(die_at="extract.inv-001"))
    with pytest.raises(Exception):
        engine.start_run(corpus)
    return engine.last_run_id


@pytest.mark.requires_db
def test_a_resumed_run_reaches_the_gate(make_engine, crashed):
    resumed = make_engine(FakeProvider(SCRIPT)).resume(crashed)

    assert resumed.status is RunStatus.AWAITING_REVIEW


@pytest.mark.requires_db
def test_resume_does_not_re_extract_documents_already_done(make_engine, crashed):
    """The load-bearing assertion. A fresh provider must never be asked for
    work the crashed run already finished."""
    provider = FakeProvider(SCRIPT)

    make_engine(provider).resume(crashed)

    redone = [c for c in provider.calls if c.stage == "extract" and c.key in ("msa-001", "amd-001")]
    assert redone == []


@pytest.mark.requires_db
def test_resume_completes_the_step_that_was_interrupted(make_engine, crashed):
    """Work in flight when the process died is not lost either -- it is
    retried, exactly once."""
    provider = FakeProvider(SCRIPT)

    make_engine(provider).resume(crashed)

    interrupted = [c for c in provider.calls if c.stage == "extract" and c.key == "inv-001"]
    assert len(interrupted) == 1


@pytest.mark.requires_db
def test_a_resumed_run_produces_the_same_register_as_an_uninterrupted_one(
    make_engine, corpus, crashed
):
    """Crashing must not change the answer, only the path to it."""
    resumed = approve_all(make_engine(FakeProvider(SCRIPT)), make_engine(FakeProvider(SCRIPT)).resume(crashed))

    clean_engine = make_engine(FakeProvider(SCRIPT))
    clean = approve_all(clean_engine, clean_engine.start_run(Corpus.from_documents(corpus.documents)))

    assert sorted(entry_hashes(resumed.register).values()) == sorted(
        entry_hashes(clean.register).values()
    )


@pytest.mark.requires_db
def test_no_step_is_recorded_as_executed_twice(make_engine, crashed):
    engine = make_engine(FakeProvider(SCRIPT))

    run = engine.resume(crashed)

    step_ids = [s.step_id for s in engine.journal(run.run_id).steps if s.succeeded]
    assert len(step_ids) == len(set(step_ids))


@pytest.mark.requires_db
def test_a_run_parked_at_the_gate_survives_the_process(make_engine, corpus):
    """The gate is a durable suspension point, not a thread waiting in memory.
    A different engine must be able to pick up a parked run and commit it."""
    parked = make_engine(FakeProvider(SCRIPT)).start_run(corpus)

    committed = approve_all(make_engine(FakeProvider({})), parked)

    assert committed.status is RunStatus.COMMITTED


@pytest.mark.requires_db
def test_resuming_a_finished_run_changes_nothing(make_engine, corpus):
    """Resume is idempotent: calling it again must not re-run or re-charge."""
    engine = make_engine(FakeProvider(SCRIPT))
    committed = approve_all(engine, engine.start_run(corpus))

    provider = FakeProvider(SCRIPT)
    again = make_engine(provider).resume(committed.run_id)

    assert provider.call_count == 0
    assert entry_hashes(again.register) == entry_hashes(committed.register)


@pytest.mark.requires_db
def test_resuming_a_parked_run_does_not_rebuild_its_gate(make_engine, corpus):
    """A run waiting on a person is finished until that person answers.

    Re-entering the pipeline would redo deterministic work for nothing and --
    worse -- rewrite the proposal a reviewer may already be looking at.
    """
    engine = make_engine(FakeProvider(SCRIPT))
    parked = engine.start_run(corpus)
    steps_before = len(engine.journal(parked.run_id).steps)
    items_before = [i.item_id for i in parked.review_bundle.items]

    provider = FakeProvider(SCRIPT)
    resumed = make_engine(provider).resume(parked.run_id)

    assert provider.call_count == 0
    assert len(engine.journal(parked.run_id).steps) == steps_before
    assert [i.item_id for i in resumed.review_bundle.items] == items_before
