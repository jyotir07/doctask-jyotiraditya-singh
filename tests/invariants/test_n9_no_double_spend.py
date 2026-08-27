"""N9: the system must never re-pay for a model call it already made.

Idempotency wherever an operation costs money. An update should cost like an
update: a document already extracted is never extracted again, and a run can
report what it spent and where the time went, stage by stage.

The measurement instrument is the fake provider's call log. Every assertion
here is a count of real calls the system chose to make, not a claim in a
README.

Breaks this catches: a fact cache keyed on something that changes per run;
re-extracting on resume; a retry that bills the successful attempt twice;
recomposing the whole corpus when one document arrives; cost accounting that
reports estimates rather than what was spent.
"""

from __future__ import annotations

import pytest

from doctask.corpus import Corpus
from doctask.kernel import FaultPlan
from doctask.llm import FakeProvider
from tests.support import (
    AMENDMENT_TEXT,
    INVOICE_TEXT,
    MSA_TEXT,
    UNRELATED_TEXT,
    approve_all,
    classify,
    fact,
    text_doc,
)

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("classify", "amd-001"): classify("AMENDMENT"),
    ("classify", "inv-001"): classify("INVOICE"),
    ("classify", "msa-globex-001"): classify("MSA"),
    ("extract", "msa-001"): {"facts": [fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")]},
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

ACME = [
    text_doc("msa-001", MSA_TEXT),
    text_doc("amd-001", AMENDMENT_TEXT),
    text_doc("inv-001", INVOICE_TEXT),
]


@pytest.fixture
def corpus():
    return Corpus.from_documents(ACME, corpus_id="acme")


@pytest.mark.requires_db
def test_re_running_a_settled_corpus_makes_no_extraction_calls(make_engine, corpus):
    """Identical bytes in, zero spend. The fact cache is keyed on content."""
    first = make_engine(FakeProvider(SCRIPT))
    approve_all(first, first.start_run(corpus))

    provider = FakeProvider(SCRIPT)
    make_engine(provider).start_run(corpus)

    assert [c for c in provider.calls if c.stage == "extract"] == []


@pytest.mark.requires_db
def test_an_arriving_document_costs_one_document(make_engine, corpus):
    """The update-cost invariant. Three documents are already settled; the
    fourth must be the only one extracted."""
    settled = make_engine(FakeProvider(SCRIPT))
    approve_all(settled, settled.start_run(corpus))

    provider = FakeProvider(SCRIPT)
    make_engine(provider).ingest(corpus, text_doc("msa-globex-001", UNRELATED_TEXT))

    extracted = [c.key for c in provider.calls if c.stage == "extract"]
    assert extracted == ["msa-globex-001"]


@pytest.mark.requires_db
def test_the_same_document_under_a_new_name_is_not_re_extracted(make_engine, corpus):
    """Content addressing, not filename addressing."""
    settled = make_engine(FakeProvider(SCRIPT))
    approve_all(settled, settled.start_run(corpus))

    provider = FakeProvider(SCRIPT)
    renamed = text_doc("msa-001-copy", MSA_TEXT, filename="msa_final_v2.txt")
    make_engine(provider).ingest(corpus, renamed)

    assert [c for c in provider.calls if c.stage == "extract"] == []


@pytest.mark.requires_db
def test_a_retry_bills_only_the_attempts_it_actually_made(make_engine, corpus):
    """A transient failure costs one extra call, not a doubled stage."""
    provider = FakeProvider(SCRIPT)
    engine = make_engine(provider, faults=FaultPlan(fail_once_at="extract.amd-001"))

    run = engine.start_run(corpus)

    attempts = [c for c in provider.calls if c.stage == "extract" and c.key == "amd-001"]
    assert len(attempts) == 2
    assert run.cost.calls == provider.call_count


@pytest.mark.requires_db
def test_a_run_reports_what_it_spent_by_stage(make_engine, corpus):
    engine = make_engine(FakeProvider(SCRIPT))

    run = engine.start_run(corpus)

    by_stage = run.cost.by_stage
    assert set(by_stage) >= {"classify", "extract"}
    assert by_stage["extract"].calls == 3
    assert by_stage["extract"].input_tokens > 0
    assert sum(s.calls for s in by_stage.values()) == run.cost.calls


@pytest.mark.requires_db
def test_a_run_reports_where_the_time_went(make_engine, corpus):
    engine = make_engine(FakeProvider(SCRIPT))

    run = engine.start_run(corpus)

    assert run.cost.duration_ms > 0
    assert all(s.duration_ms >= 0 for s in run.cost.by_stage.values())


@pytest.mark.requires_db
def test_deterministic_stages_cost_nothing(make_engine, corpus):
    """Citation verification is byte comparison. It must never reach a model."""
    provider = FakeProvider(SCRIPT)

    make_engine(provider).start_run(corpus)

    assert [c for c in provider.calls if c.stage in ("verify_citations", "compose")] == []
