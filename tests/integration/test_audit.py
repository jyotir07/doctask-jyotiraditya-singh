"""The examine movement, driven through a real run.

Three claims:

A clean corpus produces an honest report of no findings -- and says how much
it checked, so "nothing was wrong" cannot be confused with "nothing ran".

A violating corpus produces findings that each resolve to a real span in a
real document, held to N1 exactly like register entries are.

And a model-backed accusation whose citation does not verify is *not* a
finding. An unsupported accusation is still a bluff, and it is arguably worse
than an unsupported fact: it accuses someone.
"""

from __future__ import annotations

import pytest

from doctask.domain import CitationStatus, FindingKind, ReviewItemKind
from doctask.corpus import Corpus
from doctask.engine import Engine
from doctask.llm import FakeProvider
from doctask.provenance import verify_citation
from doctask.rules import load_rule_pack_file
from tests.conftest import SENTINEL_API_KEY
from tests.support import MSA_TEXT, classify, fact, span_of, text_doc

pytestmark = pytest.mark.requires_db

PACK = load_rule_pack_file("rulepacks/contract-playbook.yaml")

RENEWING_MSA = """MASTER SERVICES AGREEMENT

This Master Services Agreement is entered into between Acme Industrial Supply
Ltd ("Supplier") and Northwind Trading Co ("Customer") on 1 March 2024.

3. PAYMENT TERMS
Customer shall pay all undisputed invoices Net 30 days from the invoice date.

9. TERM
This agreement renews automatically for successive twelve month periods.
"""

EXPENSIVE_MSA = """MASTER SERVICES AGREEMENT

This Master Services Agreement is entered into between Acme Industrial Supply
Ltd ("Supplier") and Northwind Trading Co ("Customer") on 1 March 2024.

3. PAYMENT TERMS
Customer shall pay all undisputed invoices Net 30 days from the invoice date.
The agreed rate for principal consultants is USD 450 per hour.
"""


def engine_for(store, script, *, judge_answer=None):
    full = dict(script)
    if judge_answer is not None:
        full[("audit", "no-automatic-renewal")] = judge_answer
    return Engine(
        provider=FakeProvider(full), store=store,
        api_key=SENTINEL_API_KEY, rule_pack=PACK,
    )


def test_a_clean_corpus_yields_no_findings(store):
    """The rarest output in this industry."""
    engine = engine_for(store, {
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": [
            fact("payment_terms", "Net 30", MSA_TEXT, "Net 30"),
            fact("hourly_rate", "USD 145", MSA_TEXT, "USD 145 per hour"),
        ]},
    }, judge_answer={"verdict": "no"})

    run = engine.start_run(Corpus.from_documents([text_doc("msa-001", MSA_TEXT)]))

    assert run.findings == ()


def test_a_value_outside_policy_becomes_a_finding(store):
    """Deterministic stage, real violation: the rate ceiling is USD 200."""
    engine = engine_for(store, {
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": [
            fact("payment_terms", "Net 30", EXPENSIVE_MSA, "Net 30"),
            fact("hourly_rate", "USD 450", EXPENSIVE_MSA, "USD 450 per hour"),
        ]},
    }, judge_answer={"verdict": "no"})

    run = engine.start_run(Corpus.from_documents([text_doc("msa-001", EXPENSIVE_MSA)]))

    rate_findings = [f for f in run.findings if f.rule_id == "hourly-rate-ceiling"]
    assert len(rate_findings) == 1
    assert "450" in rate_findings[0].message
    assert rate_findings[0].kind is FindingKind.RULE_VIOLATION


def test_a_deterministic_finding_costs_no_model_call(store):
    """The rate ceiling is arithmetic. It must never reach a model."""
    provider = FakeProvider({
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": [
            fact("payment_terms", "Net 30", EXPENSIVE_MSA, "Net 30"),
            fact("hourly_rate", "USD 450", EXPENSIVE_MSA, "USD 450 per hour"),
        ]},
        ("audit", "no-automatic-renewal"): {"verdict": "no"},
    })
    engine = Engine(provider=provider, store=store, api_key=SENTINEL_API_KEY, rule_pack=PACK)

    engine.start_run(Corpus.from_documents([text_doc("msa-001", EXPENSIVE_MSA)]))

    assert [c.key for c in provider.calls if c.stage == "audit"] == ["no-automatic-renewal"]


def test_every_finding_resolves_to_a_real_span(store):
    """Findings obey N1. Each one is re-checkable against the source bytes."""
    doc = text_doc("msa-001", RENEWING_MSA)
    start, end = span_of(RENEWING_MSA, "renews automatically")
    engine = engine_for(store, {
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": [
            fact("payment_terms", "Net 30", RENEWING_MSA, "Net 30")
        ]},
    }, judge_answer={
        "verdict": "yes", "doc_id": "msa-001",
        "char_start": start, "char_end": end, "quoted_text": "renews automatically",
    })

    run = engine.start_run(Corpus.from_documents([doc]))

    renewal = [f for f in run.findings if f.rule_id == "no-automatic-renewal"]
    assert len(renewal) == 1
    assert verify_citation(RENEWING_MSA, renewal[0].citation) is CitationStatus.VERIFIED


def test_an_unsupported_accusation_is_not_a_finding(store):
    """The model says the rule is violated but cites a span that does not hold
    its quote. That is a bluff, and it accuses someone -- so it is downgraded
    rather than published."""
    doc = text_doc("msa-001", RENEWING_MSA)
    engine = engine_for(store, {
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": [
            fact("payment_terms", "Net 30", RENEWING_MSA, "Net 30")
        ]},
    }, judge_answer={
        "verdict": "yes", "doc_id": "msa-001",
        "char_start": 0, "char_end": 25,
        "quoted_text": "the customer waives all rights",
    })

    run = engine.start_run(Corpus.from_documents([doc]))

    assert [f for f in run.findings if f.rule_id == "no-automatic-renewal"] == []


def test_findings_reach_the_human_gate(store):
    doc = text_doc("msa-001", RENEWING_MSA)
    start, end = span_of(RENEWING_MSA, "renews automatically")
    engine = engine_for(store, {
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": [
            fact("payment_terms", "Net 30", RENEWING_MSA, "Net 30")
        ]},
    }, judge_answer={
        "verdict": "yes", "doc_id": "msa-001",
        "char_start": start, "char_end": end, "quoted_text": "renews automatically",
    })

    run = engine.start_run(Corpus.from_documents([doc]))

    finding_items = [i for i in run.review_bundle.items if i.kind is ReviewItemKind.FINDING]
    assert len(finding_items) == 1
    assert finding_items[0].decision is None


def test_the_audit_asks_the_model_only_about_judged_rules(store):
    """Four of the five rules in the pack are arithmetic. Only the fifth may
    reach a model."""
    doc = text_doc("msa-001", RENEWING_MSA)
    provider = FakeProvider({
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": [
            fact("payment_terms", "Net 30", RENEWING_MSA, "Net 30")
        ]},
        ("audit", "no-automatic-renewal"): {"verdict": "no"},
    })
    engine = Engine(provider=provider, store=store, api_key=SENTINEL_API_KEY, rule_pack=PACK)

    engine.start_run(Corpus.from_documents([doc]))

    audit_calls = [c for c in provider.calls if c.stage == "audit"]
    assert [c.key for c in audit_calls] == ["no-automatic-renewal"]


def test_a_run_without_a_rule_pack_audits_nothing(store):
    """The audit is opt-in. No pack, no findings, and no spend on judging."""
    provider = FakeProvider({
        ("classify", "msa-001"): classify("MSA"),
        ("extract", "msa-001"): {"facts": [
            fact("payment_terms", "Net 30", MSA_TEXT, "Net 30")
        ]},
    })
    engine = Engine(provider=provider, store=store, api_key=SENTINEL_API_KEY)

    run = engine.start_run(Corpus.from_documents([text_doc("msa-001", MSA_TEXT)]))

    assert run.findings == ()
    assert [c for c in provider.calls if c.stage == "audit"] == []
