"""Behaviour 4: another program can drive the whole flow end to end.

The test that matters here is the last one -- a complete run, gate included,
driven entirely over HTTP with no reference to the Python objects underneath.
Approval is an operation the machine interface exposes, not something only a
human clicking in a browser can do.

The others exist to make a failure legible when that one goes red.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doctask.api import build_app
from doctask.llm import FakeProvider
from tests.conftest import SENTINEL_API_KEY, TEST_DSN
from tests.support import AMENDMENT_TEXT, INVOICE_TEXT, MSA_TEXT, classify, fact

pytestmark = pytest.mark.requires_db

SCRIPT = {
    ("classify", "msa-001"): classify("MSA"),
    ("classify", "amd-001"): classify("AMENDMENT"),
    ("classify", "inv-001"): classify("INVOICE"),
    ("extract", "msa-001"): {
        "facts": [
            fact("payment_terms", "Net 30", MSA_TEXT, "Net 30"),
            fact("hourly_rate", "USD 145", MSA_TEXT, "USD 145 per hour"),
        ]
    },
    ("extract", "amd-001"): {
        "facts": [fact("payment_terms", "Net 45", AMENDMENT_TEXT, "Net 45")]
    },
    ("extract", "inv-001"): {
        "facts": [fact("invoice_total", "USD 5,800.00", INVOICE_TEXT, "USD 5,800.00")]
    },
}

DOCS = [
    {"doc_id": "msa-001", "filename": "msa.txt", "text": MSA_TEXT},
    {"doc_id": "amd-001", "filename": "amendment.txt", "text": AMENDMENT_TEXT},
    {"doc_id": "inv-001", "filename": "invoice.txt", "text": INVOICE_TEXT},
]


@pytest.fixture
def client(store):
    app = build_app(
        provider_factory=lambda: FakeProvider(SCRIPT),
        dsn=TEST_DSN,
        api_key=SENTINEL_API_KEY,
    )
    with TestClient(app) as c:
        yield c


def _start(client):
    return client.post("/runs", json={"corpus_id": "acme", "documents": DOCS}).json()["run_id"]


def test_submitting_documents_starts_a_run(client):
    response = client.post("/runs", json={"corpus_id": "acme", "documents": DOCS})

    assert response.status_code == 201
    assert response.json()["status"] == "awaiting_review"


def test_a_run_can_be_read_back(client):
    run_id = _start(client)

    body = client.get(f"/runs/{run_id}").json()

    assert body["run_id"] == run_id
    assert body["corpus_id"] == "acme"


def test_the_review_bundle_is_readable(client):
    run_id = _start(client)

    body = client.get(f"/runs/{run_id}/review").json()

    assert len(body["items"]) >= 2
    assert all(item["decision"] is None for item in body["items"])


def test_every_proposed_entry_carries_its_evidence(client):
    """A reviewer deciding over the API needs the citations a reviewer in the
    interface would see."""
    run_id = _start(client)

    items = client.get(f"/runs/{run_id}/review").json()["items"]

    entries = [i for i in items if i["kind"] == "entry"]
    assert entries
    assert all(i["payload"].get("citations") for i in entries)


def test_cost_is_reported_over_the_api(client):
    run_id = _start(client)

    body = client.get(f"/runs/{run_id}/cost").json()

    assert body["calls"] > 0
    assert "extract" in body["by_stage"]


def test_provenance_is_reported_over_the_api(client):
    run_id = _start(client)

    body = client.get(f"/runs/{run_id}/provenance").json()

    assert body["unsupported_facts"] == []
    assert len(body["steps"]) > 0


def test_a_decision_on_an_unknown_item_is_refused(client):
    run_id = _start(client)

    response = client.post(
        f"/runs/{run_id}/decisions",
        json={"decisions": [{"item_id": "entry:never-proposed", "decision": "approve"}]},
    )

    assert response.status_code == 404


def test_the_deliverable_is_absent_before_anything_is_committed(client):
    _start(client)

    assert client.get("/corpora/acme/deliverable").status_code == 404


def test_a_machine_can_drive_the_whole_flow_including_the_gate(client):
    """The behaviour-4 test.

    Submit, review, approve some and reject one in the same call, then export
    -- all over HTTP, with the gate crossed by an explicit operation rather
    than by anyone clicking anything.
    """
    run_id = _start(client)
    items = client.get(f"/runs/{run_id}/review").json()["items"]
    rate_item = next(i for i in items if "USD 145" in i["summary"])

    decisions = [
        {"item_id": i["item_id"], "decision": "approve"}
        for i in items
        if i["item_id"] != rate_item["item_id"]
    ] + [
        {"item_id": rate_item["item_id"], "decision": "reject",
         "reason": "rate renegotiated for FY25"}
    ]

    committed = client.post(f"/runs/{run_id}/decisions", json={"decisions": decisions})
    assert committed.status_code == 200
    assert committed.json()["status"] == "committed"

    deliverable = client.get("/corpora/acme/deliverable")
    assert deliverable.status_code == 200
    assert "USD 145" not in deliverable.json()["rendered"]
    assert "Net 30" in deliverable.json()["rendered"]
