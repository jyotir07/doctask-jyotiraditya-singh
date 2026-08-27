"""N10: the system must never write a secret to a log, a row, or an artifact.

No key ever written to a log, a commit, or a shell history. The engine is
handed a sentinel API key, a full run is exercised including its failure
paths, and then every surface a key could escape through is searched for it.

Searching real surfaces is the point. Asserting that a redaction helper
returns "***" would prove only that the helper works; these tests ask whether
the value actually reached the log stream, the journal rows, or the rendered
deliverable.

Breaks this catches: logging the provider config at debug level; storing the
key on the run row for convenience; a provider error whose message echoes the
request headers; a repr that dumps the whole config; the key reaching an
exported artifact.
"""

from __future__ import annotations

import logging

import pytest

from doctask.corpus import Corpus
from doctask.errors import ProviderError
from doctask.kernel import FaultPlan
from doctask.llm import FakeProvider
from tests.conftest import SENTINEL_API_KEY
from tests.support import (
    MSA_TEXT,
    approve_all,
    classify,
    fact,
    scan_database_for,
    text_doc,
)

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("extract", "msa-001"): {"facts": [fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")]},
}


@pytest.fixture
def corpus():
    return Corpus.from_documents([text_doc("msa-001", MSA_TEXT)])


@pytest.mark.requires_db
def test_no_log_line_contains_the_key(make_engine, corpus, caplog):
    engine = make_engine(FakeProvider(SCRIPT))

    with caplog.at_level(logging.DEBUG):
        approve_all(engine, engine.start_run(corpus))

    assert SENTINEL_API_KEY not in caplog.text


@pytest.mark.requires_db
def test_no_database_row_contains_the_key(make_engine, corpus, store):
    engine = make_engine(FakeProvider(SCRIPT))

    approve_all(engine, engine.start_run(corpus))

    assert scan_database_for(store, SENTINEL_API_KEY) == []


@pytest.mark.requires_db
def test_the_rendered_deliverable_does_not_contain_the_key(make_engine, corpus):
    engine = make_engine(FakeProvider(SCRIPT))

    committed = approve_all(engine, engine.start_run(corpus))

    assert SENTINEL_API_KEY not in committed.register.render()


@pytest.mark.requires_db
def test_the_engine_does_not_expose_the_key_in_its_repr(make_engine, corpus):
    """A repr lands in tracebacks, debuggers, and error reporters."""
    engine = make_engine(FakeProvider(SCRIPT))

    run = engine.start_run(corpus)

    assert SENTINEL_API_KEY not in repr(engine)
    assert SENTINEL_API_KEY not in repr(run)
    assert SENTINEL_API_KEY not in str(engine.config)


@pytest.mark.requires_db
def test_a_provider_failure_does_not_echo_the_key(make_engine, corpus, caplog):
    """The failure path is where secrets usually escape: an exception that
    helpfully includes the request it was making."""
    engine = make_engine(FakeProvider(SCRIPT), faults=FaultPlan(provider_error_at="extract.msa-001"))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderError) as raised:
            engine.start_run(corpus)

    assert SENTINEL_API_KEY not in str(raised.value)
    assert SENTINEL_API_KEY not in caplog.text


@pytest.mark.requires_db
def test_the_journal_records_the_failure_without_the_key(make_engine, corpus, store):
    engine = make_engine(FakeProvider(SCRIPT), faults=FaultPlan(provider_error_at="extract.msa-001"))

    with pytest.raises(ProviderError):
        engine.start_run(corpus)

    assert scan_database_for(store, SENTINEL_API_KEY) == []
    failed = [s for s in engine.journal(engine.last_run_id).steps if not s.succeeded]
    assert failed, "the failure must still be recorded, just without the secret"
