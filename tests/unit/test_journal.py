"""The step journal is what makes an interrupted step exactly-once rather
than at-least-once, which is the difference between resuming and re-paying.

Everything N7 and N9 claim rests on two properties proven here: a step that
succeeded is durably known to have succeeded, and that knowledge survives the
process that wrote it.
"""

from __future__ import annotations

import pytest

from doctask.kernel import Journal, StepRecord
from doctask.store import Store
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.requires_db


def step(step_id: str, *, succeeded: bool = True, stage: str = "extract",
         key: str | None = "doc-1", attempt: int = 1, tokens: int = 100) -> StepRecord:
    return StepRecord(
        step_id=step_id,
        stage=stage,
        key=key,
        succeeded=succeeded,
        attempts=attempt,
        input_tokens=tokens,
        output_tokens=tokens // 2,
        duration_ms=12,
        error=None if succeeded else "boom",
    )


@pytest.fixture
def journal(store):
    store.create_run(run_id="run-1", corpus_id="acme")
    return Journal(store, "run-1")


def test_a_recorded_step_appears_in_the_view(journal):
    journal.record(step("extract.doc-1"))

    assert [s.step_id for s in journal.view().steps] == ["extract.doc-1"]


def test_a_succeeded_step_is_reported_as_completed(journal):
    journal.record(step("extract.doc-1", succeeded=True))

    assert journal.completed_step_ids() == {"extract.doc-1"}


def test_a_failed_step_is_recorded_but_not_completed(journal):
    """The distinction the resume path depends on: work that failed must be
    retried, work that succeeded must not."""
    journal.record(step("extract.doc-1", succeeded=False))

    assert journal.completed_step_ids() == set()
    assert [s.step_id for s in journal.view().steps] == ["extract.doc-1"]


def test_a_retry_is_recorded_as_a_second_attempt(journal):
    """The journal is append-only: a retry adds a row rather than editing one,
    so the cost of the failed attempt stays visible."""
    journal.record(step("extract.doc-1", succeeded=False, attempt=1))
    journal.record(step("extract.doc-1", succeeded=True, attempt=2))

    assert len(journal.view().steps) == 2
    assert journal.completed_step_ids() == {"extract.doc-1"}


def test_the_journal_records_what_the_step_cost(journal):
    journal.record(step("extract.doc-1", tokens=250))

    recorded = journal.view().steps[0]
    assert recorded.input_tokens == 250
    assert recorded.output_tokens == 125
    assert recorded.duration_ms == 12


def test_a_failure_reason_is_kept(journal):
    journal.record(step("extract.doc-1", succeeded=False))

    assert journal.view().steps[0].error == "boom"


def test_journals_are_scoped_to_their_run(store):
    """One run's progress must never let another run skip its own work."""
    store.create_run(run_id="run-1", corpus_id="acme")
    store.create_run(run_id="run-2", corpus_id="acme")
    Journal(store, "run-1").record(step("extract.doc-1"))

    assert Journal(store, "run-2").completed_step_ids() == set()


def test_completed_work_survives_the_process_that_did_it(store):
    """The load-bearing durability property. A second connection -- standing
    in for the process that restarts after a crash -- must see the work."""
    store.create_run(run_id="run-1", corpus_id="acme")
    Journal(store, "run-1").record(step("extract.doc-1"))

    reopened = Store.connect(TEST_DSN)
    try:
        assert Journal(reopened, "run-1").completed_step_ids() == {"extract.doc-1"}
    finally:
        reopened.close()


def test_steps_come_back_in_the_order_they_happened(journal):
    journal.record(step("classify.doc-1", stage="classify"))
    journal.record(step("extract.doc-1"))
    journal.record(step("compose", stage="compose", key=None))

    assert [s.step_id for s in journal.view().steps] == [
        "classify.doc-1",
        "extract.doc-1",
        "compose",
    ]
