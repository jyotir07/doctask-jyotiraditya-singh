"""Risk intelligence through the REST and MCP surfaces, on the shipped corpus.

The claims under test are the boundary ones. Jev is advisory: it cannot
approve, reject, commit or change a finding; the review works identically
when it is off or failing; an unchanged finding is never paid for twice; and
its key reaches no response, row, or log line.

Every Jev response comes from httpx.MockTransport. No real key is needed.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from doctask.api import build_app
from doctask.demo import CORPORA_ROOT, RULE_PACK
from doctask.llm import FakeProvider
from doctask.mcp_server import build_server
from doctask.risk import JevClient, JevConfig, assessment_key
from doctask.rules import load_rule_pack_file
from tests.conftest import SENTINEL_API_KEY, TEST_DSN
from tests.support import scan_database_for
from tests.unit.test_risk import JEV_KEY, jev_body

pytestmark = pytest.mark.requires_db

ENABLED = JevConfig(enabled=True, api_key=JEV_KEY, base_url="https://jev.test", timeout_s=2.0)


class FakeJev:
    """A mocked Jev endpoint that records every request it receives."""

    def __init__(self, respond=None):
        self.requests: list[httpx.Request] = []
        self._respond = respond or (lambda request: httpx.Response(200, json=jev_body()))

    def handler(self, request):
        self.requests.append(request)
        return self._respond(request)

    def client_factory(self, config=ENABLED):
        return lambda: JevClient(config, transport=httpx.MockTransport(self.handler),
                                 sleep=lambda _s: None)


def make_client(jev: FakeJev | None = None, config: JevConfig | None = ENABLED):
    jev = jev or FakeJev()
    app = build_app(
        provider_factory=lambda: FakeProvider({}), dsn=TEST_DSN, api_key=SENTINEL_API_KEY,
        rule_pack=load_rule_pack_file(RULE_PACK), corpora_root=CORPORA_ROOT,
        jev_config=config, jev_client_factory=jev.client_factory(config or JevConfig()),
    )
    return TestClient(app)


def start(client, hold_back=("appendix-a",)) -> str:
    return client.post("/demo/acme-v1/runs", json={"hold_back": list(hold_back)}).json()["run_id"]


def review(client, run_id) -> list[dict]:
    return client.get(f"/runs/{run_id}/review").json()["items"]


def approve_all(client, run_id):
    decisions = [{"item_id": i["item_id"], "decision": "approve"} for i in review(client, run_id)]
    return client.post(f"/runs/{run_id}/decisions", json={"decisions": decisions})


def without_risk(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in i.items() if k != "risk"} for i in items]


def assessable(items):
    return [i for i in items if i["kind"] in ("conflict", "finding")]


# --- the happy path -----------------------------------------------------------


def test_conflicts_and_findings_are_assessed_and_entries_are_not(store):
    jev = FakeJev()
    with make_client(jev) as client:
        run_id = start(client)
        result = client.post(f"/runs/{run_id}/risk").json()
        items = review(client, run_id)

    targets = assessable(items)
    assert targets, "the acme corpus proposes both a conflict and findings"
    assert result["advisory"] is True
    assert set(result["items"]) == {i["item_id"] for i in targets}
    assert len(jev.requests) == len(targets)
    for item in items:
        if item["kind"] == "entry":
            assert item["risk"] is None
        else:
            risk = item["risk"]
            assert risk["status"] == "assessed" and risk["advisory"] is True
            assert risk["risk_level"] == "high"
            assert risk["model_version"] == "jev-1.13.0" and risk["assessed_at"]


def test_the_idempotency_key_is_the_content_derived_assessment_key(store):
    jev = FakeJev()
    with make_client(jev) as client:
        run_id = start(client)
        client.post(f"/runs/{run_id}/risk")
        keys = {assessment_key(i) for i in assessable(review(client, run_id))}

    assert {r.headers["idempotency-key"] for r in jev.requests} == keys


# --- cost: unchanged findings are never re-paid for ---------------------------


def test_assessing_twice_costs_nothing_the_second_time(store):
    jev = FakeJev()
    with make_client(jev) as client:
        run_id = start(client)
        client.post(f"/runs/{run_id}/risk")
        first = len(jev.requests)
        again = client.post(f"/runs/{run_id}/risk").json()

    assert len(jev.requests) == first
    assert all(r["cached"] for r in again["items"].values())


def test_a_later_run_with_unchanged_findings_makes_no_calls(store):
    """appendix-a changes an entry, not the conflict or the findings."""
    jev = FakeJev()
    with make_client(jev) as client:
        first_run = start(client)
        client.post(f"/runs/{first_run}/risk")
        approve_all(client, first_run)
        paid = len(jev.requests)

        second_run = client.post("/demo/acme-v1/ingest/appendix-a").json()["run_id"]
        result = client.post(f"/runs/{second_run}/risk").json()

    assert result["items"], "the second run still proposes the conflict and findings"
    assert len(jev.requests) == paid
    assert all(r["status"] == "assessed" and r["cached"] for r in result["items"].values())


# --- it is advisory: no path to the gate or the rules -------------------------


def test_a_critical_escalate_verdict_changes_no_decision_and_no_finding(store):
    alarmist = FakeJev(lambda r: httpx.Response(
        200, json=jev_body(risk="critical", escalation=0.99, category="policy_violation")))
    with make_client(alarmist) as client:
        run_id = start(client)
        before = without_risk(review(client, run_id))
        run_before = client.get(f"/runs/{run_id}").json()

        client.post(f"/runs/{run_id}/risk")

        after = review(client, run_id)
        assert without_risk(after) == before
        assert all(i["decision"] is None and not i["applied"] for i in after)
        assert client.get(f"/runs/{run_id}").json() == run_before
        assert run_before["status"] == "awaiting_review"


def test_jev_cannot_override_a_human_decision(store):
    """Approve every item despite 'critical, escalate'; reject one Jev rated low."""
    with make_client(FakeJev(lambda r: httpx.Response(
            200, json=jev_body(risk="critical", escalation=0.99)))) as client:
        run_id = start(client)
        client.post(f"/runs/{run_id}/risk")
        items = review(client, run_id)
        decisions = [{"item_id": i["item_id"], "decision": "approve"} for i in items]
        decisions[0]["decision"] = "reject"

        committed = client.post(f"/runs/{run_id}/decisions", json={"decisions": decisions})
        final = {i["item_id"]: i for i in review(client, run_id)}

    assert committed.status_code == 200
    assert committed.json()["status"] == "committed"
    assert not final[items[0]["item_id"]]["applied"]
    assert all(final[i["item_id"]]["applied"] for i in items[1:])


def test_jev_cannot_bypass_the_deterministic_rules(store):
    """A 'low risk, no escalation, other' verdict on every item leaves every
    rule finding in the queue exactly as the rule pack produced it."""
    with make_client(config=None) as client:
        baseline = assessable(review(client, start(client)))
    store.reset()

    lenient = FakeJev(lambda r: httpx.Response(
        200, json=jev_body(risk="low", escalation=0.01, category="other")))
    with make_client(lenient) as client:
        run_id = start(client)
        client.post(f"/runs/{run_id}/risk")
        assessed = assessable(review(client, run_id))

    assert without_risk(assessed) == without_risk(baseline)
    assert {i["payload"]["rule_id"] for i in assessed if i["kind"] == "finding"} == {
        i["payload"]["rule_id"] for i in baseline if i["kind"] == "finding"}


# --- optional: the review is unchanged when Jev is off or failing -------------


def test_with_jev_disabled_the_review_and_commit_work_and_nothing_is_called(store):
    jev = FakeJev()
    with make_client(jev, config=None) as client:
        run_id = start(client)
        items = review(client, run_id)
        result = client.post(f"/runs/{run_id}/risk").json()
        committed = approve_all(client, run_id)

    assert jev.requests == []
    assert result["enabled"] is False
    assert {r["reason"] for r in result["items"].values()} == {"disabled"}
    assert all(i["risk"]["status"] == "unavailable" for i in assessable(items))
    assert committed.json()["status"] == "committed"


def test_enabled_without_a_key_degrades_to_unavailable(store):
    no_key = JevConfig(enabled=True, api_key=None)
    with make_client(config=no_key) as client:
        run_id = start(client)
        result = client.post(f"/runs/{run_id}/risk")
        committed = approve_all(client, run_id)

    assert result.status_code == 200
    assert {r["reason"] for r in result.json()["items"].values()} == {"missing_key"}
    assert committed.json()["status"] == "committed"


@pytest.mark.parametrize("respond,reason,calls", [
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "timeout", 1),
    (lambda r: httpx.Response(401), "auth_failed", 1),
    (lambda r: httpx.Response(429), "rate_limited", 2),
    (lambda r: httpx.Response(500), "upstream_error", None),
    (lambda r: httpx.Response(200, json={"answers": {}}), "invalid_response", None),
])
def test_a_failing_jev_leaves_the_review_usable(store, respond, reason, calls):
    jev = FakeJev(respond)
    with make_client(jev) as client:
        run_id = start(client)
        result = client.post(f"/runs/{run_id}/risk")
        items = review(client, run_id)
        committed = approve_all(client, run_id)

    assert result.status_code == 200
    assert {r["status"] for r in result.json()["items"].values()} == {"unavailable"}
    assert {r["reason"] for r in result.json()["items"].values()} == {reason}
    # Failures that would repeat for every item stop after the first one.
    if calls is not None:
        assert len(jev.requests) == calls
    assert all(i["risk"]["status"] == "not_assessed" for i in assessable(items))
    assert committed.json()["status"] == "committed"


def test_a_failed_assessment_is_not_cached_so_a_retry_can_succeed(store):
    upstream = {"healthy": False}

    def flaky(request):
        if upstream["healthy"]:
            return httpx.Response(200, json=jev_body())
        return httpx.Response(500)

    with make_client(FakeJev(flaky)) as client:
        run_id = start(client)
        first = client.post(f"/runs/{run_id}/risk").json()
        with store.raw_cursor() as cur:
            cur.execute("SELECT count(*) FROM risk_assessments")
            assert cur.fetchone()[0] == 0
        upstream["healthy"] = True
        second = client.post(f"/runs/{run_id}/risk").json()

    assert {r["status"] for r in first["items"].values()} == {"unavailable"}
    assert {r["status"] for r in second["items"].values()} == {"assessed"}


def test_an_unknown_run_is_a_404(store):
    with make_client() as client:
        assert client.post("/runs/run-nope/risk").status_code == 404


# --- N10 for the new surface --------------------------------------------------


def test_the_jev_key_reaches_no_response_row_or_log(store, caplog):
    def echo(request):
        return httpx.Response(401, json={"echo": dict(request.headers)})

    # The failing upstream first: a success would fill the cache and the
    # echoing 401 would never be reached.
    echoing, healthy = FakeJev(echo), FakeJev()
    bodies = []
    with caplog.at_level(logging.DEBUG):
        for jev in (echoing, healthy):
            with make_client(jev) as client:
                run_id = start(client, hold_back=())
                bodies.append(client.post(f"/runs/{run_id}/risk").text)
                bodies.append(client.get(f"/runs/{run_id}/review").text)
                bodies.append(approve_all(client, run_id).text)

    assert all(JEV_KEY not in b for b in bodies)
    assert JEV_KEY not in caplog.text
    assert scan_database_for(store, JEV_KEY) == []
    assert echoing.requests and healthy.requests, "both paths must actually reach Jev"
    assert healthy.requests[0].headers["authorization"] == f"Bearer {JEV_KEY}"


# --- MCP parity ---------------------------------------------------------------


async def call(server, name, **arguments):
    result = await server.call_tool(name, arguments)
    assert not result.is_error, f"{name} failed: {result.content}"
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_mcp_exposes_the_same_assessment(store):
    jev = FakeJev()
    with make_client(config=None) as client:
        run_id = start(client)
    server = build_server(provider_factory=lambda: FakeProvider({}), dsn=TEST_DSN,
                          api_key=SENTINEL_API_KEY, jev_config=ENABLED,
                          jev_client_factory=jev.client_factory())

    assert "assess_risk" in {t.name for t in await server.list_tools()}
    result = await call(server, "assess_risk", run_id=run_id)
    bundle = await call(server, "get_review_bundle", run_id=run_id)

    assert result["advisory"] is True and result["items"]
    assert {r["status"] for r in result["items"].values()} == {"assessed"}
    assert all(i["risk"]["status"] == "assessed"
               for i in bundle["items"] if i["kind"] != "entry")
