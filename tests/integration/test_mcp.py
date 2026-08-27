"""The MCP surface, driven the way an agent would drive it.

Same claim as the REST tests, through the other door: an agent can take a pile
of documents all the way to an exported deliverable, crossing the gate by
calling a tool rather than by anyone clicking anything.

The parity test is the one that keeps the two surfaces honest. It is easy to
add an operation to one and forget the other, and the moment that happens
"the interface a program uses" and "the interface a person uses" stop being
the same system.
"""

from __future__ import annotations

import json

import pytest

from doctask.llm import FakeProvider
from doctask.mcp_server import build_server
from tests.conftest import SENTINEL_API_KEY, TEST_DSN
from tests.integration.test_api import DOCS, SCRIPT

pytestmark = pytest.mark.requires_db


@pytest.fixture
def server(store):
    return build_server(
        provider_factory=lambda: FakeProvider(SCRIPT),
        dsn=TEST_DSN,
        api_key=SENTINEL_API_KEY,
    )


async def call(server, name, **arguments):
    """Invoke a tool the way a client would, and unwrap its result.

    Deliberately goes through call_tool and decodes the wire content rather
    than calling the Python function directly. A tool that works when called
    as a function but not as a tool is not a tool.
    """
    result = await server.call_tool(name, arguments)
    assert not result.is_error, f"{name} failed: {result.content}"

    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_the_four_core_operations_are_exposed(server):
    """Upload, instruct, approve, export -- the minimum a machine needs."""
    names = {tool.name for tool in await server.list_tools()}

    assert {"start_run", "get_review_bundle", "decide", "export_deliverable"} <= names


@pytest.mark.anyio
async def test_every_tool_documents_itself(server):
    """An agent picks tools by their descriptions. An undocumented tool is one
    that will be called wrongly."""
    for tool in await server.list_tools():
        assert tool.description, f"{tool.name} has no description"


@pytest.mark.anyio
async def test_an_agent_can_drive_the_whole_flow(server):
    started = await call(server, "start_run", corpus_id="acme", documents=DOCS)
    assert started["status"] == "awaiting_review"

    bundle = await call(server, "get_review_bundle", run_id=started["run_id"])
    rate_item = next(i for i in bundle["items"] if "USD 145" in i["summary"])

    committed = await call(
        server,
        "decide",
        run_id=started["run_id"],
        decisions=[
            {"item_id": i["item_id"], "decision": "approve"}
            for i in bundle["items"]
            if i["item_id"] != rate_item["item_id"]
        ]
        + [{"item_id": rate_item["item_id"], "decision": "reject",
            "reason": "rate renegotiated"}],
    )
    assert committed["status"] == "committed"

    exported = await call(server, "export_deliverable", corpus_id="acme")
    assert exported["committed"] is True
    assert "Net 30" in exported["rendered"]
    assert "USD 145" not in exported["rendered"]


@pytest.mark.anyio
async def test_exporting_before_committing_says_so_rather_than_failing(server):
    """An agent needs a usable answer, not an exception it has to parse."""
    await call(server, "start_run", corpus_id="acme", documents=DOCS)

    exported = await call(server, "export_deliverable", corpus_id="acme")

    assert exported["committed"] is False
    assert "nothing has been committed" in exported["detail"]


@pytest.mark.anyio
async def test_cost_is_reportable_over_mcp(server):
    started = await call(server, "start_run", corpus_id="acme", documents=DOCS)

    cost = await call(server, "get_run_cost", run_id=started["run_id"])

    assert cost["calls"] > 0
    assert cost["by_stage"]["extract"]["calls"] == 3
