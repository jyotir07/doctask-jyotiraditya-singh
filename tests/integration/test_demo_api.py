"""The browser demo's routes: shipped corpora, driven over HTTP.

Only starting a run is demo-specific. Everything after it -- the review, the
decisions, the deliverable -- goes through the ordinary routes, so these tests
also prove the demo is not a separate code path.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doctask.api import build_app
from doctask.demo import CORPORA_ROOT, RULE_PACK
from doctask.llm import FakeProvider
from doctask.rules import load_rule_pack_file
from tests.conftest import SENTINEL_API_KEY, TEST_DSN

pytestmark = pytest.mark.requires_db


@pytest.fixture
def client(store):
    app = build_app(
        provider_factory=lambda: FakeProvider({}),
        dsn=TEST_DSN,
        api_key=SENTINEL_API_KEY,
        rule_pack=load_rule_pack_file(RULE_PACK),
        corpora_root=CORPORA_ROOT,
    )
    with TestClient(app) as c:
        yield c


def _approve_all(client, run_id):
    items = client.get(f"/runs/{run_id}/review").json()["items"]
    decisions = [{"item_id": i["item_id"], "decision": "approve"} for i in items]
    return client.post(f"/runs/{run_id}/decisions", json={"decisions": decisions})


def test_the_shipped_corpora_are_listed_with_their_documents(client):
    corpora = {c["corpus_id"]: c for c in client.get("/demo/corpora").json()}
    assert len(corpora["acme-v1"]["documents"]) == 6


def test_a_demo_run_stops_at_the_gate(client):
    body = client.post("/demo/acme-v1/runs").json()
    assert body["status"] == "awaiting_review"
    assert body["review_items"] > 0


def test_a_demo_run_audits_against_the_rule_pack(client):
    run_id = client.post("/demo/acme-v1/runs").json()["run_id"]
    kinds = {i["kind"] for i in client.get(f"/runs/{run_id}/review").json()["items"]}
    assert "finding" in kinds


def test_the_fabricated_fact_is_refused_and_reported(client):
    run_id = client.post("/demo/acme-v1/runs").json()["run_id"]
    unsupported = client.get(f"/runs/{run_id}/provenance").json()["unsupported_facts"]
    assert [u["field"] for u in unsupported] == ["late_payment_interest"]

    client.post(f"/runs/{run_id}/decisions", json={"decisions": []})
    rendered = client.get("/corpora/acme-v1/deliverable")
    assert "5% per month" not in rendered.text


def test_the_whole_demo_flow_commits_over_http(client):
    run_id = client.post("/demo/acme-v1/runs").json()["run_id"]
    assert _approve_all(client, run_id).json()["status"] == "committed"
    deliverable = client.get("/corpora/acme-v1/deliverable").json()
    assert {e["field"] for e in deliverable["entries"]} == {"hourly_rate", "payment_terms"}


def test_a_held_back_document_arrives_as_a_focused_update(client):
    first = client.post("/demo/acme-v1/runs", json={"hold_back": ["appendix-a"]}).json()
    assert "appendix-a" not in first["documents"]
    _approve_all(client, first["run_id"])
    before = {e["field"]: e for e in client.get("/corpora/acme-v1/deliverable").json()["entries"]}

    update = client.post("/demo/acme-v1/ingest/appendix-a").json()
    assert update["impact_set"] == [before["hourly_rate"]["entry_id"]]

    _approve_all(client, update["run_id"])
    after = {e["field"]: e for e in client.get("/corpora/acme-v1/deliverable").json()["entries"]}
    assert after["payment_terms"]["content_hash"] == before["payment_terms"]["content_hash"]
    assert after["hourly_rate"]["content_hash"] != before["hourly_rate"]["content_hash"]


def test_an_arriving_document_is_not_re_extracted_twice(client):
    client.post("/demo/acme-v1/runs", json={"hold_back": ["appendix-a"]})
    first = client.post("/demo/acme-v1/ingest/appendix-a").json()["run_id"]
    again = client.post("/demo/acme-v1/ingest/appendix-a").json()["run_id"]
    assert client.get(f"/runs/{first}/cost").json()["by_stage"]["extract"]["calls"] == 1
    assert "extract" not in client.get(f"/runs/{again}/cost").json()["by_stage"]


def test_a_citation_indexes_into_the_served_document_text(client):
    run_id = client.post("/demo/acme-v1/runs").json()["run_id"]
    conflict = client.get(f"/runs/{run_id}").json()["conflicts"][0]
    for c in conflict["citations"]:
        text = client.get(f"/corpora/acme-v1/documents/{c['doc_id']}").json()["text"]
        assert text[c["char_start"]:c["char_end"]] == c["quoted_text"]


def test_an_unknown_corpus_is_404(client):
    assert client.post("/demo/nope/runs").status_code == 404


def test_holding_back_an_unknown_document_is_refused(client):
    response = client.post("/demo/acme-v1/runs", json={"hold_back": ["nope"]})
    assert response.status_code == 422


def test_an_unknown_document_is_404(client):
    client.post("/demo/acme-v1/runs")
    assert client.get("/corpora/acme-v1/documents/nope").status_code == 404
