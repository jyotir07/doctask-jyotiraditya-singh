"""N5: the system must never report success unless the deliverable is
genuinely in the state it claims.

A success message only ever means the output is really there. The tests here
deliberately do not trust the value the engine returns in memory -- they
re-read committed state through a *second* engine over the same store,
because the failure mode being guarded against is precisely an engine that
says "committed" while nothing was persisted.

Breaks this catches: reporting COMMITTED before the write lands; swallowing a
persistence error and returning the in-memory register; a partial commit that
applies some approved items and reports full success; leaving a failed run in
a state that reads as finished to the next caller.
"""

from __future__ import annotations

import pytest

from doctask.corpus import Corpus
from doctask.domain import RunStatus
from doctask.errors import CommitVerificationFailed
from doctask.kernel import FaultPlan
from doctask.llm import FakeProvider
from tests.support import MSA_TEXT, approve_all, classify, entry_hashes, fact, text_doc

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("extract", "msa-001"): {
        "facts": [
            fact("payment_terms", "Net 30", MSA_TEXT, "Net 30"),
            fact("hourly_rate", "USD 145", MSA_TEXT, "USD 145 per hour"),
        ]
    },
}


@pytest.fixture
def corpus():
    return Corpus.from_documents([text_doc("msa-001", MSA_TEXT)])


@pytest.mark.requires_db
def test_committed_status_means_the_deliverable_is_readable_by_someone_else(
    make_engine, corpus
):
    engine = make_engine(FakeProvider(SCRIPT))

    committed = approve_all(engine, engine.start_run(corpus))

    assert committed.status is RunStatus.COMMITTED
    reader = make_engine(FakeProvider({}))
    persisted = reader.committed_register(committed.corpus_id)
    assert entry_hashes(persisted) == entry_hashes(committed.register)


@pytest.mark.requires_db
def test_a_failed_commit_does_not_report_success(make_engine, corpus):
    engine = make_engine(FakeProvider(SCRIPT), faults=FaultPlan(die_at="commit.persist"))
    run = engine.start_run(corpus)

    with pytest.raises(CommitVerificationFailed):
        approve_all(engine, run)

    reader = make_engine(FakeProvider({}))
    assert reader.get_run(run.run_id).status is not RunStatus.COMMITTED


@pytest.mark.requires_db
def test_a_failed_commit_leaves_the_previous_deliverable_untouched(make_engine, corpus):
    """A commit that cannot complete must not half-land."""
    engine = make_engine(FakeProvider(SCRIPT))
    first = approve_all(engine, engine.start_run(corpus))
    before = entry_hashes(first.register)

    broken = make_engine(FakeProvider(SCRIPT), faults=FaultPlan(die_at="commit.persist"))
    with pytest.raises(CommitVerificationFailed):
        approve_all(broken, broken.ingest(corpus, text_doc("msa-002", MSA_TEXT)))

    reader = make_engine(FakeProvider({}))
    assert entry_hashes(reader.committed_register(first.corpus_id)) == before


@pytest.mark.requires_db
def test_commit_verifies_the_post_state_rather_than_assuming_it(make_engine, corpus):
    """If the store silently loses a row, the engine must notice at commit
    time rather than reporting success and finding out later."""
    engine = make_engine(FakeProvider(SCRIPT), faults=FaultPlan(drop_entries_on_persist=1))
    run = engine.start_run(corpus)

    with pytest.raises(CommitVerificationFailed):
        approve_all(engine, run)


@pytest.mark.requires_db
def test_an_aborted_run_reports_its_real_status(make_engine, corpus):
    """A run killed mid-flight must not read as finished to anyone asking."""
    engine = make_engine(FakeProvider(SCRIPT), faults=FaultPlan(die_at="extract.msa-001"))

    with pytest.raises(Exception):
        engine.start_run(corpus)

    reader = make_engine(FakeProvider({}))
    status = reader.get_run(engine.last_run_id).status
    assert status in (RunStatus.ABORTED, RunStatus.FAILED)
