"""The Jev client and parser, against mocked HTTP only.

No test here needs a key or a network: every response is served by
httpx.MockTransport, so what is exercised is the real request construction,
status handling, retry policy and parsing.
"""

from __future__ import annotations

import json

import httpx
import pytest

from doctask.risk import (
    ENDPOINT,
    MAX_EXCERPT_CHARS,
    MAX_EXCERPTS,
    MODEL,
    AssessmentUnavailable,
    JevClient,
    JevConfig,
    assessment_key,
    assessment_state,
    parse_response,
)

JEV_KEY = "jev-SENTINEL-MUST-NEVER-LEAK-9f8e7d"
CONFIG = JevConfig(enabled=True, api_key=JEV_KEY, base_url="https://jev.test", timeout_s=2.0)


def jev_body(risk="high", escalation=0.8, category="conflict", model="jev-1.13.0") -> dict:
    return {
        "model": model,
        "answers": {
            "risk_level": {"type": "choice", "choice": risk,
                           "probabilities": {risk: 0.7}, "confidence": 0.6},
            "needs_escalation": {"type": "noul", "noul": escalation},
            "finding_category": {"type": "choice", "choice": category,
                                 "probabilities": {category: 0.9}, "confidence": 0.8},
        },
        "usage": {"input_tokens": 300, "output_tokens": 12},
    }


CONFLICT_ITEM = {
    "item_id": "conflict:c1", "kind": "conflict",
    "summary": "payment_terms: sources disagree (Net 30, Net 45)",
    "payload": {
        "conflict_id": "c1", "entry_id": "e1", "field": "payment_terms",
        "kind": "value_mismatch", "values": ["Net 30", "Net 45"],
        "suggested_resolution": "Net 45",
        "citations": [
            {"doc_id": "msa", "char_start": 0, "char_end": 6, "quoted_text": "Net 30"},
            {"doc_id": "amd", "char_start": 0, "char_end": 6, "quoted_text": "Net 45"},
        ],
    },
}


def client_with(handler, calls=None, sleeps=None) -> JevClient:
    def recording(request):
        if calls is not None:
            calls.append(request)
        return handler(request)

    return JevClient(CONFIG, transport=httpx.MockTransport(recording),
                     sleep=(sleeps.append if sleeps is not None else (lambda _s: None)))


# --- parsing ------------------------------------------------------------------


def test_a_typed_response_parses_into_the_three_answers():
    parsed = parse_response(jev_body(risk="critical", escalation=0.93, category="policy_violation"))

    assert parsed["risk_level"] == "critical"
    assert parsed["escalation_probability"] == pytest.approx(0.93)
    assert parsed["finding_category"] == "policy_violation"
    assert parsed["model_version"] == "jev-1.13.0"
    assert parsed["risk_probabilities"] == {"critical": 0.7}
    assert (parsed["input_tokens"], parsed["output_tokens"]) == (300, 12)


@pytest.mark.parametrize("mutate", [
    lambda b: b.pop("answers"),
    lambda b: b.pop("model"),
    lambda b: b["answers"].pop("risk_level"),
    lambda b: b["answers"]["risk_level"].update(choice="catastrophic"),
    lambda b: b["answers"]["risk_level"].update(type="score"),
    lambda b: b["answers"]["risk_level"].update(probabilities={"unknown": 0.5}),
    lambda b: b["answers"]["finding_category"].update(choice="fraud"),
    lambda b: b["answers"]["needs_escalation"].update(noul=1.7),
    lambda b: b["answers"]["needs_escalation"].update(noul="yes"),
    lambda b: b["answers"]["needs_escalation"].update(noul=True),
    lambda b: b["answers"]["needs_escalation"].update(type="choice"),
])
def test_an_answer_outside_the_declared_types_is_rejected(mutate):
    body = jev_body()
    mutate(body)

    with pytest.raises(AssessmentUnavailable) as raised:
        parse_response(body)
    assert raised.value.reason == "invalid_response"


@pytest.mark.parametrize("body", [None, [], "ok", 42])
def test_a_response_that_is_not_an_object_is_rejected(body):
    with pytest.raises(AssessmentUnavailable) as raised:
        parse_response(body)
    assert raised.value.reason == "invalid_response"


# --- the request --------------------------------------------------------------


def test_the_request_is_typed_authenticated_and_idempotent():
    calls = []
    client = client_with(lambda r: httpx.Response(200, json=jev_body()), calls)

    client.assess(assessment_state(CONFLICT_ITEM), idempotency_key="k-123")

    (request,) = calls
    assert request.method == "POST"
    assert str(request.url) == f"https://jev.test{ENDPOINT}"
    assert request.headers["authorization"] == f"Bearer {JEV_KEY}"
    assert request.headers["idempotency-key"] == "k-123"
    sent = json.loads(request.content)
    assert sent["model"] == MODEL
    assert {q: sent["questions"][q]["type"] for q in sent["questions"]} == {
        "risk_level": "choice", "needs_escalation": "noul", "finding_category": "choice",
    }
    assert set(sent["questions"]["risk_level"]["criteria"]) == {"low", "medium", "high", "critical"}
    assert set(sent["questions"]["finding_category"]["criteria"]) == {
        "conflict", "policy_violation", "unsupported_claim", "other",
    }


def test_only_bounded_item_data_is_sent():
    long_quote = "x" * 5000
    item = {**CONFLICT_ITEM, "payload": {
        **CONFLICT_ITEM["payload"],
        "citations": [{"doc_id": f"d{i}", "char_start": 0, "char_end": 1,
                       "quoted_text": long_quote} for i in range(20)],
    }}

    state = assessment_state(item)

    assert len(state["source_excerpts"]) == MAX_EXCERPTS
    assert all(len(e["text"]) <= MAX_EXCERPT_CHARS + 1 for e in state["source_excerpts"])
    assert "suggested_resolution" not in json.dumps(state)
    assert set(state) == {"domain", "review_item", "source_excerpts"}


def test_the_assessment_key_is_stable_and_follows_content():
    same = json.loads(json.dumps(CONFLICT_ITEM))
    same["item_id"] = "conflict:renamed-in-another-run"
    changed = json.loads(json.dumps(CONFLICT_ITEM))
    changed["payload"]["values"] = ["Net 30", "Net 60"]

    assert assessment_key(CONFLICT_ITEM) == assessment_key(same)
    assert assessment_key(CONFLICT_ITEM) != assessment_key(changed)


# --- failures -----------------------------------------------------------------


def test_a_missing_key_is_reported_not_raised_as_a_crash():
    with pytest.raises(AssessmentUnavailable) as raised:
        JevClient(JevConfig(enabled=True, api_key=None))
    assert raised.value.reason == "missing_key"


@pytest.mark.parametrize("status,reason", [
    (401, "auth_failed"), (403, "auth_failed"), (422, "upstream_error"), (500, "upstream_error"),
])
def test_http_failures_map_to_a_reason_without_retrying(status, reason):
    calls = []
    client = client_with(lambda r: httpx.Response(status, json={"error": "x"}), calls)

    with pytest.raises(AssessmentUnavailable) as raised:
        client.assess({}, idempotency_key="k")

    assert raised.value.reason == reason
    assert len(calls) == 1


@pytest.mark.parametrize("status,reason", [(429, "rate_limited"), (529, "upstream_error")])
def test_rate_limits_are_retried_once_with_the_same_idempotency_key(status, reason):
    calls, sleeps = [], []
    client = client_with(lambda r: httpx.Response(status), calls, sleeps)

    with pytest.raises(AssessmentUnavailable) as raised:
        client.assess({}, idempotency_key="k-stable")

    assert raised.value.reason == reason
    assert len(calls) == 2 and len(sleeps) == 1
    assert {c.headers["idempotency-key"] for c in calls} == {"k-stable"}


def test_a_rate_limit_followed_by_success_succeeds():
    responses = iter([httpx.Response(429), httpx.Response(200, json=jev_body())])
    calls = []
    client = client_with(lambda r: next(responses), calls)

    assert client.assess({}, idempotency_key="k")["risk_level"] == "high"
    assert len(calls) == 2


def test_a_read_timeout_is_never_retried_because_it_may_have_been_billed():
    calls = []

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = client_with(timeout, calls)

    with pytest.raises(AssessmentUnavailable) as raised:
        client.assess({}, idempotency_key="k")

    assert raised.value.reason == "timeout"
    assert len(calls) == 1


def test_a_connection_that_never_opened_is_retried_once():
    calls = []

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    client = client_with(refused, calls)

    with pytest.raises(AssessmentUnavailable) as raised:
        client.assess({}, idempotency_key="k")

    assert raised.value.reason == "upstream_error"
    assert len(calls) == 2


def test_a_non_json_body_is_an_invalid_response():
    client = client_with(lambda r: httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(AssessmentUnavailable) as raised:
        client.assess({}, idempotency_key="k")
    assert raised.value.reason == "invalid_response"


# --- the key never leaks ------------------------------------------------------


def test_the_key_is_not_in_any_repr_or_error_message():
    def echo_headers(request):
        # An upstream that helpfully echoes the request back in its error body.
        return httpx.Response(401, json={"echo": dict(request.headers)})

    client = client_with(echo_headers)

    with pytest.raises(AssessmentUnavailable) as raised:
        client.assess({}, idempotency_key="k")

    assert JEV_KEY not in str(raised.value)
    assert JEV_KEY not in repr(raised.value)
    assert JEV_KEY not in repr(client)
    assert JEV_KEY not in repr(CONFIG)
    assert JEV_KEY not in str(CONFIG)


def test_config_reads_the_documented_environment():
    config = JevConfig.from_env({
        "DOCTASK_JEV_ENABLED": "true", "JEVMODEL_API_KEY": JEV_KEY,
        "DOCTASK_JEV_TIMEOUT_S": "3.5",
    })
    assert config.enabled and config.api_key == JEV_KEY and config.timeout_s == 3.5
    assert config.base_url == "https://jevmodel.org"

    assert not JevConfig.from_env({}).enabled
    assert JevConfig.from_env({"DOCTASK_JEV_BASE_URL": "https://x.test"}).base_url == "https://x.test"
