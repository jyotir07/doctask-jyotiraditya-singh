"""The same operations again, as MCP tools.

MCP is the shape SuperDocs uses, so it is the strongest form of behaviour 4.
There is deliberately nothing here the REST surface cannot do and nothing
there this cannot: both are thin adapters over `Engine`, and the React
interface is a third client of the same operations.

The gate is a tool like any other. An agent driving this server approves and
rejects items explicitly, one at a time, exactly as a person would.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from doctask.corpus import Corpus
from doctask.domain import Decision, Document, ReviewDecision
from doctask.engine import Engine
from doctask.ingest import sha256_bytes
from doctask.llm import FakeProvider, Provider
from doctask.store import Store


def build_server(*, provider_factory: Callable[[], Provider], dsn: str,
                 api_key: str | None = None) -> MCPServer:
    server = MCPServer("doctask")

    def engine() -> tuple[Engine, Store]:
        store = Store.connect(dsn)
        return Engine(provider=provider_factory(), store=store, api_key=api_key), store

    def _document(doc: dict) -> Document:
        text = doc["text"]
        return Document(
            doc_id=doc["doc_id"],
            filename=doc.get("filename", f"{doc['doc_id']}.txt"),
            media_type=doc.get("media_type", "text/plain"),
            text=text,
            sha256=sha256_bytes(text.encode("utf-8")),
        )

    @server.tool()
    def start_run(corpus_id: str, documents: list[dict]) -> dict[str, Any]:
        """Ingest a pile of related documents and stop at the review gate.

        Returns the run id, its status, and how many items are waiting for a
        decision. Nothing is committed until `decide` is called.
        """
        eng, store = engine()
        try:
            corpus = Corpus.from_documents(
                [_document(d) for d in documents], corpus_id=corpus_id
            )
            run = eng.start_run(corpus)
            return {"run_id": run.run_id, "status": run.status.value,
                    "items_awaiting_review": len(run.review_bundle.items),
                    "conflicts": len(run.conflicts)}
        finally:
            store.close()

    @server.tool()
    def ingest_document(corpus_id: str, document: dict) -> dict[str, Any]:
        """Add one document to a corpus that already has a deliverable.

        Produces a focused update: entries the new source does not bear on are
        left exactly as they were.
        """
        eng, store = engine()
        try:
            run = eng.ingest(Corpus.from_documents([], corpus_id=corpus_id),
                             _document(document))
            return {"run_id": run.run_id, "status": run.status.value,
                    "impact_set": sorted(run.impact_set),
                    "items_awaiting_review": len(run.review_bundle.items)}
        finally:
            store.close()

    @server.tool()
    def get_review_bundle(run_id: str) -> dict[str, Any]:
        """What the run intends to do, itemised for a decision.

        Each item carries the citations behind it, so a decision can be made
        on evidence rather than on the system's say-so.
        """
        eng, store = engine()
        try:
            run = eng.get_run(run_id)
            return {
                "run_id": run_id,
                "status": run.status.value,
                "items": [
                    {"item_id": i.item_id, "kind": i.kind.value, "summary": i.summary,
                     "payload": i.payload, "decision": i.decision.value if i.decision else None}
                    for i in run.review_bundle.items
                ],
            }
        finally:
            store.close()

    @server.tool()
    def decide(run_id: str, decisions: list[dict]) -> dict[str, Any]:
        """Cross the gate: approve and reject items, then commit.

        Each decision is `{"item_id": ..., "decision": "approve"|"reject",
        "reason": ..., "resolution": ...}`. Items left undecided are not
        committed -- silence is not approval.
        """
        eng, store = engine()
        try:
            run = eng.submit_decisions(
                run_id,
                [
                    ReviewDecision(
                        item_id=d["item_id"],
                        decision=Decision(d["decision"]),
                        reason=d.get("reason"),
                        resolution=d.get("resolution"),
                    )
                    for d in decisions
                ],
            )
            return {"run_id": run.run_id, "status": run.status.value,
                    "applied": [i.item_id for i in run.review_bundle.items if i.applied]}
        finally:
            store.close()

    @server.tool()
    def resume_run(run_id: str) -> dict[str, Any]:
        """Continue a run that was interrupted, without redoing finished work."""
        eng, store = engine()
        try:
            run = eng.resume(run_id)
            return {"run_id": run.run_id, "status": run.status.value}
        finally:
            store.close()

    @server.tool()
    def export_deliverable(corpus_id: str) -> dict[str, Any]:
        """The committed Obligation Register, rendered."""
        eng, store = engine()
        try:
            register = eng.committed_register(corpus_id)
            if register is None:
                return {"corpus_id": corpus_id, "committed": False,
                        "detail": "nothing has been committed for this corpus yet"}
            return {"corpus_id": corpus_id, "committed": True,
                    "version": register.version, "rendered": register.render()}
        finally:
            store.close()

    @server.tool()
    def get_run_cost(run_id: str) -> dict[str, Any]:
        """What the run spent and where the time went, stage by stage."""
        eng, store = engine()
        try:
            cost = eng.get_run(run_id).cost
            return {
                "calls": cost.calls, "input_tokens": cost.input_tokens,
                "output_tokens": cost.output_tokens, "duration_ms": cost.duration_ms,
                "by_stage": {
                    stage: {"calls": s.calls, "input_tokens": s.input_tokens,
                            "duration_ms": s.duration_ms}
                    for stage, s in cost.by_stage.items()
                },
            }
        finally:
            store.close()

    @server.tool()
    def get_provenance(run_id: str) -> dict[str, Any]:
        """Which steps ran, and which proposed claims the sources did not support."""
        eng, store = engine()
        try:
            run = eng.get_run(run_id)
            return {
                "steps": [
                    {"step_id": s.step_id, "stage": s.stage, "succeeded": s.succeeded,
                     "attempts": s.attempts, "duration_ms": s.duration_ms}
                    for s in eng.journal(run_id).steps
                ],
                "unsupported_facts": [
                    {"doc_id": u.doc_id, "field": u.field, "value": u.value,
                     "status": u.status.value}
                    for u in run.unsupported_facts
                ],
            }
        finally:
            store.close()

    return server


def main() -> None:  # pragma: no cover - process entry point
    dsn = os.environ.get("DOCTASK_DSN", "postgresql://doctask:doctask@localhost:5433/doctask")
    build_server(provider_factory=lambda: FakeProvider({}), dsn=dsn).run()


if __name__ == "__main__":  # pragma: no cover
    main()
