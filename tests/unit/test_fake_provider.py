"""The fake provider is the measuring instrument for every cost invariant,
so it needs to be trustworthy in its own right.

Two properties matter. It must record every call it was asked to make, or N9
measures nothing. And it must be loud when a script entry is missing, because
a fake that quietly returns empty output would let a stage silently stop
working while the suite stayed green.
"""

from __future__ import annotations

import pytest

from doctask.errors import MissingScriptEntry
from doctask.llm import FakeProvider

SCRIPT = {
    ("classify", "doc-1"): {"doc_type": "MSA", "confidence": 0.9},
    ("extract", "doc-1"): {"facts": [{"field": "payment_terms", "value": "Net 30"}]},
}


def _complete(provider, stage, key):
    return provider.complete(
        stage=stage, key=key, instructions="do the thing", data="source text", schema={}
    )


def test_returns_the_scripted_payload():
    provider = FakeProvider(SCRIPT)

    result = _complete(provider, "classify", "doc-1")

    assert result.data == {"doc_type": "MSA", "confidence": 0.9}


def test_records_one_call_per_completion():
    provider = FakeProvider(SCRIPT)

    _complete(provider, "classify", "doc-1")
    _complete(provider, "extract", "doc-1")

    assert [(c.stage, c.key) for c in provider.calls] == [
        ("classify", "doc-1"),
        ("extract", "doc-1"),
    ]
    assert provider.call_count == 2


def test_a_missing_script_entry_is_loud():
    """Silence here would let a broken stage pass as a working one."""
    provider = FakeProvider(SCRIPT)

    with pytest.raises(MissingScriptEntry) as raised:
        _complete(provider, "extract", "doc-that-was-never-scripted")

    assert "doc-that-was-never-scripted" in str(raised.value)


def test_token_counts_are_positive():
    provider = FakeProvider(SCRIPT)

    result = _complete(provider, "classify", "doc-1")

    assert result.input_tokens > 0
    assert result.output_tokens > 0


def test_token_counts_are_deterministic():
    """Cost assertions are only meaningful if the same work always costs the
    same amount."""
    first = _complete(FakeProvider(SCRIPT), "classify", "doc-1")
    second = _complete(FakeProvider(SCRIPT), "classify", "doc-1")

    assert (first.input_tokens, first.output_tokens) == (
        second.input_tokens,
        second.output_tokens,
    )


def test_longer_input_costs_more():
    """Token counts track the work actually sent, rather than being a constant
    that would hide a stage sending far too much context."""
    provider = FakeProvider(SCRIPT)

    short = provider.complete(
        stage="classify", key="doc-1", instructions="hi", data="x", schema={}
    )
    long = provider.complete(
        stage="classify", key="doc-1", instructions="hi", data="x" * 4000, schema={}
    )

    assert long.input_tokens > short.input_tokens


def test_the_recorded_call_carries_the_tokens_it_cost():
    provider = FakeProvider(SCRIPT)

    result = _complete(provider, "extract", "doc-1")

    assert provider.calls[-1].input_tokens == result.input_tokens
    assert provider.calls[-1].output_tokens == result.output_tokens
