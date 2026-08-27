"""The machine interface (behaviour 4).

Every operation the system has is here, the gate included. Approving a review
item is an HTTP call, so a program can drive a run from submission to export
without a person clicking anything -- and a person clicking in the React
interface is doing exactly what a program would do, through the same door.

The MCP server in `mcp_server.py` exposes the same operations as tools. There
is deliberately no capability on either surface that the other lacks.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from doctask.corpus import Corpus
from doctask.domain import Decision, Document, ReviewDecision
from doctask.engine import Engine
from doctask.errors import (
    CommitVerificationFailed,
    StaleBaseVersion,
    UnknownReviewItem,
)
from doctask.ingest import sha256_bytes
from doctask.llm import Provider
from doctask.store import Store


class DocumentIn(BaseModel):
    doc_id: str
    filename: str
    text: str
    media_type: str = "text/plain"


class StartRunIn(BaseModel):
    corpus_id: str
    documents: list[DocumentIn]


class IngestIn(BaseModel):
    corpus_id: str
    document: DocumentIn


class DecisionIn(BaseModel):
    item_id: str
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = None
    resolution: str | None = None


class DecisionsIn(BaseModel):
    decisions: list[DecisionIn]


def _to_document(payload: DocumentIn) -> Document:
    return Document(
        doc_id=payload.doc_id,
        filename=payload.filename,
        media_type=payload.media_type,
        text=payload.text,
        sha256=sha256_bytes(payload.text.encode("utf-8")),
    )


def _run_body(run) -> dict:
    return {
        "run_id": run.run_id,
        "corpus_id": run.corpus_id,
        "status": run.status.value,
        "impact_set": sorted(run.impact_set),
        "conflicts": [
            {
                "conflict_id": c.conflict_id,
                "field": c.field,
                "kind": c.kind.value,
                "values": list(c.values),
                "suggested_resolution": c.suggested_resolution,
                "citations": [_citation(x) for x in c.citations],
            }
            for c in run.conflicts
        ],
        "review_items": len(run.review_bundle.items),
    }


def _citation(c) -> dict:
    return {"doc_id": c.doc_id, "char_start": c.char_start,
            "char_end": c.char_end, "quoted_text": c.quoted_text}


def build_app(*, provider_factory: Callable[[], Provider], dsn: str,
              api_key: str | None = None) -> FastAPI:
    app = FastAPI(title="doctask", version="0.1.0")

    def get_engine() -> Engine:
        # One store per request. The engine holds no run state in memory --
        # everything a later call needs is in PostgreSQL -- so this is safe and
        # keeps the API honest about where state lives.
        store = Store.connect(dsn)
        try:
            yield Engine(provider=provider_factory(), store=store, api_key=api_key)
        finally:
            store.close()

    @app.post("/runs", status_code=201)
    def start_run(body: StartRunIn, engine: Engine = Depends(get_engine)) -> dict:
        corpus = Corpus.from_documents(
            [_to_document(d) for d in body.documents], corpus_id=body.corpus_id
        )
        return _run_body(engine.start_run(corpus))

    @app.post("/runs/ingest", status_code=201)
    def ingest(body: IngestIn, engine: Engine = Depends(get_engine)) -> dict:
        """A focused update for one arriving document."""
        corpus = Corpus.from_documents([], corpus_id=body.corpus_id)
        return _run_body(engine.ingest(corpus, _to_document(body.document)))

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, engine: Engine = Depends(get_engine)) -> dict:
        try:
            return _run_body(engine.get_run(run_id))
        except UnknownReviewItem as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/runs/{run_id}/review")
    def get_review(run_id: str, engine: Engine = Depends(get_engine)) -> dict:
        try:
            run = engine.get_run(run_id)
        except UnknownReviewItem as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {
            "run_id": run_id,
            "status": run.status.value,
            "items": [
                {
                    "item_id": i.item_id,
                    "kind": i.kind.value,
                    "summary": i.summary,
                    "payload": i.payload,
                    "decision": i.decision.value if i.decision else None,
                    "reason": i.reason,
                    "applied": i.applied,
                }
                for i in run.review_bundle.items
            ],
        }

    @app.post("/runs/{run_id}/decisions")
    def decide(run_id: str, body: DecisionsIn,
               engine: Engine = Depends(get_engine)) -> dict:
        """Cross the gate. Item by item, approvals and rejections together."""
        decisions = [
            ReviewDecision(
                item_id=d.item_id,
                decision=Decision(d.decision),
                reason=d.reason,
                resolution=d.resolution,
            )
            for d in body.decisions
        ]
        try:
            return _run_body(engine.submit_decisions(run_id, decisions))
        except UnknownReviewItem as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except StaleBaseVersion as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except CommitVerificationFailed as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None

    @app.post("/runs/{run_id}/resume")
    def resume(run_id: str, engine: Engine = Depends(get_engine)) -> dict:
        try:
            return _run_body(engine.resume(run_id))
        except UnknownReviewItem as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.get("/runs/{run_id}/cost")
    def get_cost(run_id: str, engine: Engine = Depends(get_engine)) -> dict:
        try:
            cost = engine.get_run(run_id).cost
        except UnknownReviewItem as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {
            "calls": cost.calls,
            "input_tokens": cost.input_tokens,
            "output_tokens": cost.output_tokens,
            "duration_ms": cost.duration_ms,
            "by_stage": {
                stage: {
                    "calls": s.calls,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "duration_ms": s.duration_ms,
                }
                for stage, s in cost.by_stage.items()
            },
        }

    @app.get("/runs/{run_id}/provenance")
    def get_provenance(run_id: str, engine: Engine = Depends(get_engine)) -> dict:
        """What the run did, and what it could not support.

        The unsupported list is the honest half: claims the model proposed
        that the sources did not bear out.
        """
        try:
            run = engine.get_run(run_id)
        except UnknownReviewItem as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {
            "run_id": run_id,
            "steps": [
                {
                    "step_id": s.step_id,
                    "stage": s.stage,
                    "succeeded": s.succeeded,
                    "attempts": s.attempts,
                    "duration_ms": s.duration_ms,
                    "error": s.error,
                }
                for s in engine.journal(run_id).steps
            ],
            "unsupported_facts": [
                {
                    "doc_id": u.doc_id,
                    "field": u.field,
                    "value": u.value,
                    "status": u.status.value,
                    "citation": _citation(u.citation),
                }
                for u in run.unsupported_facts
            ],
        }

    @app.get("/corpora/{corpus_id}/deliverable")
    def get_deliverable(corpus_id: str, engine: Engine = Depends(get_engine)) -> dict:
        register = engine.committed_register(corpus_id)
        if register is None:
            raise HTTPException(
                status_code=404, detail=f"nothing committed for corpus {corpus_id!r}"
            )
        return {
            "corpus_id": corpus_id,
            "version": register.version,
            "rendered": register.render(),
            "entries": [
                {
                    "entry_id": e.entry_id,
                    "vendor": e.vendor,
                    "field": e.field,
                    "value": e.value,
                    "state": e.state.value,
                    "content_hash": e.content_hash,
                    "candidate_values": list(e.candidate_values),
                    "citations": [_citation(c) for c in e.citations],
                }
                for e in register.entries
            ],
        }

    return app
