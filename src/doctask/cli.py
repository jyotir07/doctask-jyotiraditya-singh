"""A thin client over the same operations the REST and MCP surfaces expose.

The CLI is deliberately not a fourth implementation. It calls `Engine`
directly, the way `api.py` and `mcp_server.py` do, so anything it can do they
can do and nothing here is demo-only scaffolding.

    python -m doctask.cli demo --corpus acme-v1

Runs with no API key: the provider seam is filled by the deterministic fake,
scripted per corpus. Everything downstream of the script -- parsing, citation
construction, verification, reconciliation, composition, the gate -- is the
same production code a live run uses.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from doctask.corpus import Corpus
from doctask.domain import Decision, EntryState, ReviewDecision
from doctask.engine import Engine
from doctask.llm import FakeProvider
from doctask.provenance import verify_citation
from doctask.rules import load_rule_pack_file
from doctask.store import Store

DEFAULT_DSN = os.environ.get(
    "DOCTASK_DSN", "postgresql://doctask:doctask@localhost:5433/doctask"
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPORA_ROOT = PROJECT_ROOT / "corpora"
RULE_PACK = PROJECT_ROOT / "rulepacks" / "contract-playbook.yaml"

RULE = "-" * 72


def _resolve(text: str, quote: str, where: str) -> tuple[int, int]:
    """Locate `quote` in `text`, or fail loudly.

    Offsets are never hand-written into a script. If a scripted quote is not
    in its document the demo stops here rather than emitting a citation that
    would fail verification later and read like a system bug.
    """
    start = text.find(quote)
    if start < 0:
        raise SystemExit(
            f"script error in {where}: quote {quote!r} does not appear in the "
            f"parsed text of that document"
        )
    return start, start + len(quote)


def build_script(corpus: Corpus, spec: dict) -> dict:
    """Turn a corpus script into fake-provider responses with real offsets."""
    by_id = {d.doc_id: d for d in corpus.documents}
    script: dict[tuple[str, str], dict] = {}

    for doc_id, entry in (spec.get("documents") or {}).items():
        if doc_id not in by_id:
            raise SystemExit(f"script names {doc_id!r}, which is not in the corpus")
        text = by_id[doc_id].text
        script[("classify", doc_id)] = {
            "doc_type": entry["doc_type"],
            "confidence": entry.get("confidence", 0.97),
        }
        facts = []
        for f in entry.get("facts", []):
            start, end = _resolve(text, f["quote"], f"{doc_id}.{f['field']}")
            facts.append({
                "field": f["field"], "value": f["value"],
                "char_start": start, "char_end": end, "quoted_text": f["quote"],
            })
        script[("extract", doc_id)] = {"facts": facts}

    for rule_id, answer in (spec.get("audit") or {}).items():
        payload = {"verdict": answer["verdict"]}
        if answer.get("quote"):
            doc = by_id[answer["doc_id"]]
            start, end = _resolve(doc.text, answer["quote"], f"audit.{rule_id}")
            payload.update({
                "doc_id": doc.doc_id, "char_start": start,
                "char_end": end, "quoted_text": answer["quote"],
            })
        script[("audit", rule_id)] = payload

    return script


def _print_documents(corpus: Corpus) -> None:
    print(RULE)
    print(f"INGEST  {len(corpus.documents)} documents in {corpus.corpus_id}")
    print(RULE)
    for d in corpus.documents:
        print(f"  {d.filename:<22} {d.media_type:<58} {d.sha256[:12]}")


def _print_gate(run) -> None:
    print()
    print(RULE)
    print(f"GATE    run {run.run_id} is {run.status.value}")
    print(RULE)
    if not run.review_bundle.items:
        print("  (nothing proposed)")
    for item in run.review_bundle.items:
        print(f"  [{item.kind.value:<8}] {item.item_id[:12]}  {item.summary}")


def _print_findings(run, corpus) -> None:
    by_id = {d.doc_id: d for d in corpus.documents}
    print()
    print(RULE)
    print(f"EXAMINE {len(run.findings)} findings")
    print(RULE)
    if not run.findings:
        print("  No findings. Every rule in the pack was checked and satisfied.")
    for f in run.findings:
        c = f.citation
        status = verify_citation(by_id[c.doc_id].text, c).value
        print(f"  [{f.severity:<6}] {f.rule_id}: {f.message}")
        quoted = c.quoted_text
        print(f"            {c.doc_id} [{c.char_start}:{c.char_end}] "
              f"{quoted!r} -> {status}")


def _print_unsupported(run) -> None:
    if not run.unsupported_facts:
        return
    print()
    print(RULE)
    print(f"UNSUPPORTED  {len(run.unsupported_facts)} claims refused")
    print(RULE)
    for u in run.unsupported_facts:
        print(f"  {u.field} = {u.value!r}: {u.reason}")


def _print_cost(run) -> None:
    c = run.cost
    print()
    print(RULE)
    print(f"COST    {c.calls} provider calls, {c.input_tokens} in / "
          f"{c.output_tokens} out, {c.duration_ms} ms")
    print(RULE)
    for stage, s in sorted(c.by_stage.items()):
        print(f"  {stage:<12} {s.calls:>3} calls  {s.input_tokens:>6} in  "
              f"{s.output_tokens:>5} out  {s.duration_ms:>6} ms")


def _print_register(run) -> None:
    print()
    print(RULE)
    print(f"DELIVERABLE  Obligation Register v{run.register.version}")
    print(RULE)
    print(run.register.render())
    disputed = [e for e in run.register.entries if e.state is EntryState.DISPUTED]
    if disputed:
        noun = "entry" if len(disputed) == 1 else "entries"
        print(f"({len(disputed)} {noun} left disputed rather than silently resolved.)")


def cmd_demo(args: argparse.Namespace) -> int:
    root = CORPORA_ROOT / args.corpus
    if not root.is_dir():
        raise SystemExit(f"no corpus at {root}")

    corpus = Corpus.from_dir(str(root / "docs"), corpus_id=args.corpus)
    spec = yaml.safe_load((root / "script.yaml").read_text(encoding="utf-8"))
    provider = FakeProvider(build_script(corpus, spec))

    store = _connect()

    try:
        engine = Engine(provider=provider, store=store,
                        rule_pack=load_rule_pack_file(str(RULE_PACK)),
                        api_key=os.environ.get("ANTHROPIC_API_KEY"))

        _print_documents(corpus)
        run = engine.start_run(corpus)
        _print_findings(run, corpus)
        _print_unsupported(run)
        _print_gate(run)

        decisions = [
            ReviewDecision(item_id=i.item_id, decision=Decision.APPROVE,
                           reason="approved in demo")
            for i in run.review_bundle.items
        ]
        if args.reject_first and decisions:
            first = run.review_bundle.items[0]
            decisions[0] = ReviewDecision(
                item_id=first.item_id, decision=Decision.REJECT,
                reason="rejected in demo to show the gate holds per item")
            print(f"\n  -> rejecting {first.item_id[:12]} to show per-item decisions")

        run = engine.submit_decisions(run.run_id, decisions)
        print(f"\nCOMMIT  run is {run.status.value}")
        _print_register(run)
        _print_cost(run)
        return 0
    finally:
        store.close()


def cmd_reset(args: argparse.Namespace) -> int:
    """Empty the demo database, so the next demo pays for extraction again.

    Without this a second `demo` finds every document already extracted and
    shows a run that skipped everything -- correct, but not the run you meant
    to show.
    """
    store = _connect()
    try:
        store.reset()
    finally:
        store.close()
    print(f"reset: every table in {_redacted(DEFAULT_DSN)} is empty")
    return 0


def _connect() -> Store:
    try:
        return Store(DEFAULT_DSN)
    except Exception as exc:
        raise SystemExit(
            f"cannot reach PostgreSQL at {_redacted(DEFAULT_DSN)}\n"
            f"  {type(exc).__name__}: {exc}\n"
            f"  run `make up` first"
        ) from None


def _redacted(dsn: str) -> str:
    """The DSN with its password removed, for printing (N10)."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}@{host}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doctask")
    sub = parser.add_subparsers(dest="command", required=True)

    reset = sub.add_parser("reset", help="empty the demo database")
    reset.set_defaults(func=cmd_reset)

    demo = sub.add_parser("demo", help="run a corpus end to end and commit it")
    demo.add_argument("--corpus", default="acme-v1")
    demo.add_argument("--reject-first", action="store_true",
                      help="reject the first review item, to show the gate is per-item")
    demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
