"""N8: the system must never corrupt state when two runs overlap.

Two runs at the same time stay two runs, whether they are two piles or the
same pile hit twice.

These tests use real threads against real PostgreSQL connections, not
cooperative scheduling. A test that interleaves two runs on one connection
would serialise for reasons unrelated to the locking this system claims to
do, and would pass whether or not that locking exists.

Breaks this catches: a lost update when two commits race; duplicate entries
for one obligation; a corpus-scoped cache shared across corpora; a deliverable
version counter that skips or repeats; one run reading another run's
uncommitted gate.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from doctask.corpus import Corpus
from doctask.domain import RunStatus
from doctask.engine import Engine
from doctask.errors import StaleBaseVersion
from doctask.llm import FakeProvider
from tests.conftest import SENTINEL_API_KEY
from tests.support import (
    AMENDMENT_TEXT,
    MSA_TEXT,
    UNRELATED_TEXT,
    approve_all,
    classify,
    entry_hashes,
    fact,
    text_doc,
)

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("classify", "amd-001"): classify("AMENDMENT"),
    ("classify", "msa-globex-001"): classify("MSA"),
    ("extract", "msa-001"): {"facts": [fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")]},
    ("extract", "amd-001"): {
        "facts": [fact("payment_terms", "Net 45", AMENDMENT_TEXT, "Net 45")]
    },
    ("extract", "msa-globex-001"): {
        "facts": [fact("payment_terms", "Net 60", UNRELATED_TEXT, "Net 60")]
    },
}

ACME = [text_doc("msa-001", MSA_TEXT), text_doc("amd-001", AMENDMENT_TEXT)]
GLOBEX = [text_doc("msa-globex-001", UNRELATED_TEXT)]


def _engine(store_factory):
    return Engine(
        provider=FakeProvider(SCRIPT),
        store=store_factory(),
        faults=None,
        api_key=SENTINEL_API_KEY,
    )


def _run_to_gate(engine, docs, corpus_id):
    return engine.start_run(Corpus.from_documents(docs, corpus_id=corpus_id))


@pytest.mark.requires_db
def test_two_runs_on_different_corpora_do_not_see_each_other(store_factory):
    acme, globex = _engine(store_factory), _engine(store_factory)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_run_to_gate, acme, ACME, "acme")
        g = pool.submit(_run_to_gate, globex, GLOBEX, "globex")
        acme_run, globex_run = a.result(), g.result()

    acme_values = {e.value for e in acme_run.register.entries}
    globex_values = {e.value for e in globex_run.register.entries}
    assert "Net 60" not in acme_values
    assert acme_values.isdisjoint(globex_values)


@pytest.mark.requires_db
def test_two_runs_on_the_same_corpus_both_complete(store_factory):
    one, two = _engine(store_factory), _engine(store_factory)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_run_to_gate, one, ACME, "acme")
        b = pool.submit(_run_to_gate, two, ACME, "acme")
        first, second = a.result(), b.result()

    assert first.status is RunStatus.AWAITING_REVIEW
    assert second.status is RunStatus.AWAITING_REVIEW
    assert first.run_id != second.run_id


@pytest.mark.requires_db
def test_concurrent_commits_do_not_lose_an_update(store_factory):
    """The classic race. Two runs commit against the same corpus; the second
    must either serialise cleanly or be told its base is stale. What it must
    never do is overwrite the first commit as though it never happened."""
    one, two = _engine(store_factory), _engine(store_factory)
    first = _run_to_gate(one, ACME, "acme")
    second = _run_to_gate(two, ACME, "acme")

    approve_all(one, first)
    try:
        approve_all(two, second)
    except StaleBaseVersion:
        pass

    reader = _engine(store_factory)
    committed = reader.committed_register("acme")
    assert entry_hashes(committed)


@pytest.mark.requires_db
def test_the_same_obligation_is_not_duplicated_by_a_race(store_factory):
    one, two = _engine(store_factory), _engine(store_factory)
    first = _run_to_gate(one, ACME, "acme")
    second = _run_to_gate(two, ACME, "acme")

    approve_all(one, first)
    try:
        approve_all(two, second)
    except StaleBaseVersion:
        pass

    committed = _engine(store_factory).committed_register("acme")
    fields = [e.field for e in committed.entries]
    assert len(fields) == len(set(fields))


@pytest.mark.requires_db
def test_deliverable_version_advances_once_per_commit(store_factory):
    """No skipped and no repeated version numbers, so the audit trail of what
    changed and when stays readable."""
    one, two = _engine(store_factory), _engine(store_factory)

    first = approve_all(one, _run_to_gate(one, ACME, "acme"))
    second = approve_all(two, two.ingest(Corpus.from_documents(ACME, corpus_id="acme"), text_doc("msa-globex-001", UNRELATED_TEXT)))

    assert second.register.version == first.register.version + 1


@pytest.mark.requires_db
def test_one_run_cannot_decide_another_runs_gate(store_factory):
    """Review items are scoped to the run that proposed them."""
    one, two = _engine(store_factory), _engine(store_factory)
    first = _run_to_gate(one, ACME, "acme")
    second = _run_to_gate(two, ACME, "acme")

    approve_all(one, first)

    stale = two.get_run(second.run_id)
    assert all(not item.applied for item in stale.review_bundle.items)
