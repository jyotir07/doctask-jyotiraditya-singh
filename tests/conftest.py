"""Shared fixtures.

Only the model is faked anywhere in this suite. The store is real PostgreSQL,
because a test that proves its own mocks work proves nothing about the system.
"""

from __future__ import annotations

import os

import pytest

from doctask.engine import Engine
from doctask.store import Store

TEST_DSN = os.environ.get(
    "DOCTASK_TEST_DSN",
    "postgresql://doctask:doctask@localhost:5433/doctask_test",
)

# A value that must never appear in a log line, a journal row, or an artifact.
SENTINEL_API_KEY = "sk-ant-SENTINEL-MUST-NEVER-BE-LOGGED-a1b2c3d4"


@pytest.fixture
def store():
    """A store scoped to one test, truncated on the way out."""
    s = Store.connect(TEST_DSN)
    s.reset()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def store_factory(store):
    """Independent store connections sharing one database.

    Concurrency is proven with real connections against real PostgreSQL. Two
    runs sharing a single connection would serialise for reasons that have
    nothing to do with the locking this system claims to do.
    """
    opened = []

    def _open():
        s = Store.connect(TEST_DSN)
        opened.append(s)
        return s

    try:
        yield _open
    finally:
        for s in opened:
            s.close()


@pytest.fixture
def make_engine(store):
    """Build engines that share one store.

    A factory rather than a single engine because the resume and double-spend
    invariants need a *second* engine over the *same* store: that is what
    proves run state lives in PostgreSQL and not in process memory.
    """

    def _make(provider, *, faults=None, api_key=SENTINEL_API_KEY):
        return Engine(provider=provider, store=store, faults=faults, api_key=api_key)

    return _make


@pytest.fixture
def anyio_backend():
    """MCP tools are async. asyncio only -- trio is not a target here."""
    return "asyncio"
