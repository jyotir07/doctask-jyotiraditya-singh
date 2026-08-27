"""The engine: stages, the gate, and resume.

Every entry point here is an operation a program can drive without a human
clicking through anything. The React interface and the MCP server are both
clients of this surface; neither can do anything it cannot.

Three ideas hold the whole thing together.

**A step is exactly-once.** Every unit of work has a deterministic id and is
recorded in the journal when it completes. Resuming consults the journal and
the content-addressed fact store, so finished work is never redone and never
re-paid for.

**The gate is a durable suspension point, not a thread waiting in memory.**
A run parks in AWAITING_REVIEW with its proposal written to the database. The
process can exit entirely; a different process picks the run up and commits it.
That is why behaviours 2 and 3 are one mechanism rather than two.

**Success is verified, never assumed.** Commit writes, then reads back, then
compares. Only after that does the run report itself committed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace

from doctask.classify import CLASSIFICATION_SCHEMA, classify_document
from doctask.corpus import Corpus
from doctask.cost import aggregate
from doctask.domain import (
    Citation,
    ClassificationOutcome,
    Conflict,
    ConflictKind,
    Decision,
    DocType,
    EntryState,
    Fact,
    Finding,
    FindingKind,
    RegisterEntry,
    ReviewBundle,
    ReviewDecision,
    ReviewItem,
    ReviewItemKind,
    RunCost,
    RunStatus,
    UnsupportedFact,
    CitationStatus,
)
from doctask.entities import MatchOutcome, VendorRegistry
from doctask.errors import (
    CommitVerificationFailed,
    ProviderError,
    StaleBaseVersion,
    UnknownReviewItem,
)
from doctask.extract import EXTRACTION_SCHEMA, parse_extraction
from doctask.provenance import verify_citation
from doctask.kernel import FaultPlan, Journal, JournalView, StepRecord
from doctask.llm import Provider
from doctask.register import Register, build_entry
from doctask.reconcile import DocContext, reconcile
from doctask.retrieval import VectorIndex
from doctask.injection import RULE_ID as INJECTION_RULE_ID, detect_injection
from doctask.rules import RulePack, Verdict, evaluate
from doctask.store import Store

# Bumping this invalidates every cached extraction, deliberately and visibly.
EXTRACTOR_VERSION = "v1"

CLASSIFY_INSTRUCTIONS = (
    "Identify the type of the document in the DOCUMENT section. "
    "Respond only with the required fields."
)
EXTRACT_INSTRUCTIONS = (
    "Extract contractual facts from the DOCUMENT section. For each fact give the "
    "exact character span in the document that supports it, and quote that span "
    "verbatim. Respond only with the required fields."
)
AUDIT_INSTRUCTIONS = (
    "Answer the QUESTION using only the PASSAGES section. Cite the exact span "
    "that supports your answer, quoting it verbatim. If the passages do not "
    "settle the question, answer 'unclear' rather than guessing."
)

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "doc_id": {"type": "string"},
        "char_start": {"type": "integer"},
        "char_end": {"type": "integer"},
        "quoted_text": {"type": "string"},
    },
    "required": ["verdict"],
}


class _FaultInjected(Exception):
    """Raised by the configured fault point. Test scaffolding, not a failure
    mode the system has in production."""


@dataclass(frozen=True)
class EngineConfig:
    """Configuration. Never renders credential material (N10)."""

    provider_name: str = "fake"
    model: str = "claude-sonnet-5"
    max_extract_retries: int = 1
    classify_confidence_floor: float = 0.6


@dataclass(frozen=True)
class Run:
    run_id: str
    corpus_id: str
    status: RunStatus
    register: Register
    review_bundle: ReviewBundle
    conflicts: tuple[Conflict, ...]
    findings: tuple[Finding, ...]
    unsupported_facts: tuple[UnsupportedFact, ...]
    cost: RunCost
    impact_set: frozenset[str]


def _citation_dict(c: Citation) -> dict:
    return {"doc_id": c.doc_id, "char_start": c.char_start,
            "char_end": c.char_end, "quoted_text": c.quoted_text}


def _citation_from(d: dict) -> Citation:
    return Citation(doc_id=d["doc_id"], char_start=d["char_start"],
                    char_end=d["char_end"], quoted_text=d["quoted_text"])


def _entry_dict(e: RegisterEntry) -> dict:
    return {
        "entry_id": e.entry_id, "vendor": e.vendor, "field": e.field, "value": e.value,
        "state": e.state.value, "content_hash": e.content_hash,
        "citations": [_citation_dict(c) for c in e.citations],
        "candidate_values": list(e.candidate_values),
    }


def _entry_from(d: dict) -> RegisterEntry:
    return build_entry(
        vendor=d["vendor"], field=d["field"], value=d["value"],
        state=EntryState(d["state"]),
        citations=tuple(_citation_from(c) for c in d["citations"]),
        candidate_values=tuple(d["candidate_values"]),
    )


class Engine:
    """Runs the three movements, and stops where a person is required."""

    def __init__(self, *, provider: Provider, store: Store,
                 faults: FaultPlan | None = None, api_key: str | None = None,
                 config: EngineConfig | None = None,
                 rule_pack: RulePack | None = None) -> None:
        self._provider = provider
        self._store = store
        self._rule_pack = rule_pack
        self._index = VectorIndex(store, provider)
        self._faults = faults or FaultPlan()
        # Held privately and never rendered. See __repr__.
        self.__api_key = api_key
        self._config = config or EngineConfig()
        self._last_run_id: str | None = None

    def __repr__(self) -> str:
        # Explicit, because the default repr of a dataclass-ish object is a
        # very effective way to leak a key into a traceback (N10).
        return f"<Engine provider={self._config.provider_name!r} store=Store>"

    @property
    def config(self) -> EngineConfig:
        return self._config

    @property
    def last_run_id(self) -> str:
        if self._last_run_id is None:
            raise RuntimeError("this engine has not started a run")
        return self._last_run_id

    def journal(self, run_id: str) -> JournalView:
        return Journal(self._store, run_id).view()

    # --- step plumbing ----------------------------------------------------

    def _check_fault(self, step_id: str) -> None:
        if self._faults.die_at == step_id:
            raise _FaultInjected(f"fault injected at {step_id}")
        if self._faults.provider_error_at == step_id:
            raise ProviderError(f"provider call failed at {step_id}")

    def _call_provider(self, journal: Journal, step_id: str, stage: str, key: str | None,
                       instructions: str, data: str, schema: dict) -> dict:
        """One cost-bearing call, timed and journalled.

        Retries are recorded as separate attempts so the failed call's cost
        stays visible: you paid for it.
        """
        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            try:
                self._check_fault(step_id)
                if self._faults.fail_once_at == step_id and attempt == 1:
                    # Burn a real provider call, then fail: a transient error
                    # after the model has already been paid for.
                    self._provider.complete(stage=stage, key=key, instructions=instructions,
                                            data=data, schema=schema)
                    raise ProviderError(f"transient failure at {step_id}")
                result = self._provider.complete(stage=stage, key=key,
                                                 instructions=instructions,
                                                 data=data, schema=schema)
            except _FaultInjected:
                raise
            except ProviderError as exc:
                journal.record(StepRecord(
                    step_id=step_id, stage=stage, key=key, succeeded=False,
                    attempts=attempt, provider_calls=1,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    error=str(exc),
                ))
                if attempt > self._config.max_extract_retries:
                    raise
                continue

            journal.record(StepRecord(
                step_id=step_id, stage=stage, key=key, succeeded=True, attempts=attempt,
                provider_calls=1, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_ms=int((time.perf_counter() - started) * 1000),
            ))
            return result.data

    def _record_local(self, journal: Journal, step_id: str, stage: str,
                      started: float) -> None:
        """A deterministic stage: timed, journalled, and costing nothing."""
        journal.record(StepRecord(
            step_id=step_id, stage=stage, key=None, succeeded=True, attempts=1,
            provider_calls=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
        ))

    # --- driving a run ----------------------------------------------------

    def start_run(self, corpus: Corpus) -> Run:
        """Understand the pile, stopping at the gate."""
        return self._run(corpus, corpus.documents, incremental=False)

    def ingest(self, corpus: Corpus, document) -> Run:
        """A focused update for one arriving document."""
        return self._run(corpus, [document], incremental=True)

    def _run(self, corpus: Corpus, documents, *, incremental: bool) -> Run:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        self._last_run_id = run_id
        self._store.create_run(run_id=run_id, corpus_id=corpus.corpus_id, status="running")

        try:
            return self._execute(run_id, corpus, documents, incremental=incremental)
        except _FaultInjected:
            self._store.set_run_status(run_id, RunStatus.ABORTED.value, "aborted mid-run")
            raise
        except Exception as exc:
            self._store.set_run_status(run_id, RunStatus.FAILED.value, str(exc))
            raise

    def _execute(self, run_id: str, corpus: Corpus, documents, *, incremental: bool) -> Run:
        journal = Journal(self._store, run_id)
        done = journal.completed_step_ids()
        corpus_id = corpus.corpus_id

        for document in documents:
            self._process_document(run_id, corpus_id, document, journal, done)

        return self._compose_and_gate(run_id, corpus_id, journal,
                                      incremental=incremental, documents=documents)

    def _process_document(self, run_id, corpus_id, document, journal, done) -> None:
        """Intake, classify, extract, verify -- for one document.

        Every branch here is a real decision that changes the path: content
        already seen is skipped, a low-confidence classification escalates, an
        unrecognised type is skipped with a reason.
        """
        existing = self._store.document_with_content(corpus_id, document.sha256)
        if existing is not None and existing != document.doc_id:
            # These exact bytes are already in the corpus under another name.
            # Recording it again would double-count the evidence and re-pay
            # for the extraction.
            self._record_local(
                journal, f"skip.{document.doc_id}", "skip", time.perf_counter()
            )
            return

        self._store.upsert_document(corpus_id, document)

        classify_step = f"classify.{document.doc_id}"
        extract_step = f"extract.{document.doc_id}"

        # Content addressing: these exact bytes have been through the model
        # already, under this or any other name.
        if self._store.is_extracted(document.sha256, EXTRACTOR_VERSION):
            started = time.perf_counter()
            if extract_step not in done:
                self._record_local(journal, f"skip.{document.doc_id}", "skip", started)
            return

        if classify_step not in done:
            payload = self._call_provider(
                journal, classify_step, "classify", document.doc_id,
                CLASSIFY_INSTRUCTIONS, self._data_region(document.text),
                CLASSIFICATION_SCHEMA,
            )
            decision = classify_document(
                payload, confidence_floor=self._config.classify_confidence_floor
            )

            if decision.outcome is ClassificationOutcome.SKIP:
                self._store.mark_extracted(document.sha256, EXTRACTOR_VERSION)
                return

            vendor = self._vendor_for(corpus_id, document, decision.doc_type)
            self._store.upsert_document(
                corpus_id, document, doc_type=decision.doc_type.value, vendor=vendor
            )

        if extract_step not in done:
            payload = self._call_provider(
                journal, extract_step, "extract", document.doc_id,
                EXTRACT_INSTRUCTIONS, self._data_region(document.text), EXTRACTION_SCHEMA,
            )
            started = time.perf_counter()
            result = parse_extraction(payload, document)
            self._record_local(journal, f"verify_citations.{document.doc_id}",
                               "verify_citations", started)
            self._store.save_facts(document.sha256, result.facts, result.unsupported)

            # Indexing is a cost-bearing call like any other, so it is
            # journalled like any other. A cost line that counted completions
            # but not embeddings would under-report every run that ingested
            # anything.
            started = time.perf_counter()
            indexed = self._index.index_document(document)
            journal.record(StepRecord(
                step_id=f"index.{document.doc_id}", stage="index", key=document.doc_id,
                succeeded=True, attempts=1, provider_calls=indexed.provider_calls,
                input_tokens=indexed.input_tokens,
                duration_ms=int((time.perf_counter() - started) * 1000),
            ))
            self._store.mark_extracted(document.sha256, EXTRACTOR_VERSION)

    @staticmethod
    def _data_region(text: str) -> str:
        """Source text, fenced off from the instructions.

        The structural half of N2. Document content never joins the
        instruction region, and the output schema is closed, so a document can
        influence which facts are proposed but has no route to control flow.
        """
        return f"<<<DOCUMENT>>>\n{text}\n<<<END DOCUMENT>>>"

    def _vendor_for(self, corpus_id: str, document, doc_type: DocType) -> str:
        """Resolve the vendor this document is about."""
        known = [d["vendor"] for d in self._store.documents_for(corpus_id) if d["vendor"]]
        registry = VendorRegistry(sorted(set(known)))
        guess = self._guess_vendor_name(document.text)
        if guess is None:
            return "Unknown"
        match = registry.resolve(guess)
        if match.outcome is MatchOutcome.ESCALATE:
            return match.candidates[0]
        return match.canonical

    @staticmethod
    def _guess_vendor_name(text: str) -> str | None:
        """First capitalised company-looking phrase before a legal form.

        Deliberately dumb and deterministic. Vendor identity feeds entry ids,
        and a model call here would make those ids vary between runs.
        """
        import re

        match = re.search(
            r"([A-Z][A-Za-z&.\-]*(?:\s+[A-Z][A-Za-z&.\-]*){0,4}\s+"
            r"(?:Ltd|Limited|Inc|Incorporated|LLC|PLC|Corp|Corporation|GmbH))",
            text,
        )
        if match is None:
            return None
        # Collapse internal whitespace: the pattern spans line breaks, so a
        # name wrapped across two lines would otherwise carry the newline into
        # the entry id and render as a broken heading in the deliverable.
        return " ".join(match.group(1).split())

    # --- composing and the gate -------------------------------------------

    def _load_all_facts(self, corpus_id: str):
        facts, unsupported, contexts = [], [], {}
        for row in self._store.documents_for(corpus_id):
            for f in self._store.facts_for(row["sha256"]):
                facts.append(Fact(
                    fact_id=f["fact_id"], doc_id=f["doc_id"], field=f["field"],
                    value=f["value"],
                    citation=Citation(doc_id=f["doc_id"], char_start=f["char_start"],
                                      char_end=f["char_end"], quoted_text=f["quoted_text"]),
                ))
            for u in self._store.unsupported_for(row["sha256"]):
                unsupported.append(UnsupportedFact(
                    doc_id=u["doc_id"], field=u["field"], value=u["value"],
                    citation=Citation(doc_id=u["doc_id"], char_start=u["char_start"],
                                      char_end=u["char_end"], quoted_text=u["quoted_text"]),
                    status=CitationStatus(u["status"]),
                ))
            if row["vendor"]:
                contexts[row["doc_id"]] = DocContext(
                    doc_id=row["doc_id"], vendor=row["vendor"],
                    doc_type=DocType(row["doc_type"]) if row["doc_type"] else DocType.UNKNOWN,
                )
        facts = [f for f in facts if f.doc_id in contexts]
        return facts, unsupported, contexts

    def _compose_and_gate(self, run_id, corpus_id, journal, *, incremental: bool,
                          documents=()) -> Run:
        started = time.perf_counter()
        facts, unsupported, contexts = self._load_all_facts(corpus_id)
        result = reconcile(facts, contexts)
        self._record_local(journal, "reconcile", "reconcile", started)

        started = time.perf_counter()
        base_version = self._store.latest_version(corpus_id)
        committed = self.committed_register(corpus_id)
        committed_hashes = (
            {e.entry_id: e.content_hash for e in committed.entries} if committed else {}
        )
        impact = frozenset(
            e.entry_id for e in result.entries
            if committed_hashes.get(e.entry_id) != e.content_hash
        )
        self._record_local(journal, "compose", "compose", started)

        findings, report = self._audit(run_id, corpus_id, result.entries, journal,
                                       documents)
        items = self._build_bundle(result.entries, result.conflicts, findings,
                                   committed_hashes)

        self._store.save_proposal(
            run_id,
            entries=[_entry_dict(e) for e in result.entries],
            conflicts=[self._conflict_dict(c) for c in result.conflicts],
            findings=[self._finding_dict(f) for f in findings],
            unsupported=[{"doc_id": u.doc_id, "field": u.field, "value": u.value,
                          "citation": _citation_dict(u.citation), "status": u.status.value}
                         for u in unsupported],
            impact_set=impact,
            base_version=base_version,
        )
        self._store.save_review_items(run_id, items)
        self._store.set_run_status(run_id, RunStatus.AWAITING_REVIEW.value)

        return self.get_run(run_id)

    @staticmethod
    def _finding_dict(f: Finding) -> dict:
        return {"finding_id": f.finding_id, "rule_id": f.rule_id, "kind": f.kind.value,
                "severity": f.severity, "message": f.message,
                "citation": _citation_dict(f.citation)}

    @staticmethod
    def _finding_from(d: dict) -> Finding:
        return Finding(
            finding_id=d["finding_id"], rule_id=d["rule_id"],
            kind=FindingKind(d["kind"]), severity=d["severity"], message=d["message"],
            citation=_citation_from(d["citation"]),
        )

    def _audit(self, run_id, corpus_id, entries, journal, documents=()):
        """Stage 2 of the examine movement.

        Deterministic checks have already run for free inside `evaluate`. What
        reaches the model is only what genuinely needs judgement, and only
        while a rule pack is configured.
        """
        # Injection detection runs before the rule-pack guard on purpose. An
        # attempt to give the system orders must be reported whether or not a
        # playbook happens to be configured.
        injections = self._detect_injections(corpus_id, documents)

        if self._rule_pack is None:
            return tuple(injections), None

        started = time.perf_counter()
        doc_types = {
            d["doc_type"] for d in self._store.documents_for(corpus_id) if d["doc_type"]
        }
        report = evaluate(
            self._rule_pack, entries,
            judge=lambda rule, _entries: self._judge(journal, corpus_id, rule),
            doc_types=doc_types,
        )
        self._record_local(journal, "audit", "audit", started)
        return tuple(injections) + tuple(report.findings), report

    def _detect_injections(self, corpus_id: str, documents=()) -> list[Finding]:
        """Documents that try to give orders, reported as data.

        Deterministic and free: no provider call, so an attacker cannot make
        the audit expensive by burying imperatives in a long document.

        Texts from this run win over the stored copy. A doc_id re-ingested
        with different bytes must be scanned as the bytes that just arrived,
        not as the version already on disk -- otherwise an injected revision
        of a known-clean document would slip past.
        """
        texts = {row["doc_id"]: row["text"] for row in self._store.documents_for(corpus_id)}
        texts.update({d.doc_id: d.text for d in documents})

        findings: list[Finding] = []
        for doc_id, text in sorted(texts.items()):
            hit = detect_injection(text)
            if hit is None:
                continue
            start, end, quoted = hit
            findings.append(Finding(
                finding_id=f"injection:{doc_id}",
                rule_id=INJECTION_RULE_ID,
                kind=FindingKind.INJECTION_ATTEMPT,
                severity="high",
                message=(
                    f"{doc_id} contains instructions aimed at the system "
                    f"({quoted!r}). Treated as data and reported; it did not "
                    f"affect extraction, reconciliation, or any rule verdict."
                ),
                citation=Citation(doc_id=doc_id, char_start=start,
                                  char_end=end, quoted_text=quoted),
            ))
        return findings

    def _judge(self, journal, corpus_id, rule):
        """Ask a model one rule's question, over retrieved passages only.

        A verdict whose citation does not verify is downgraded to UNDECIDED
        rather than becoming a finding. An unsupported accusation is still a
        bluff, and findings are held to N1 exactly like register entries.
        """
        question = rule.check["question"]
        passages = self._index.search(question, limit=3)
        if not passages:
            return Verdict.UNDECIDED, None

        # Each passage is labelled with the span it came from, so a citation
        # the model returns can be mapped back onto the original document.
        rendered = "\n\n".join(
            f"[{p.doc_id} {p.char_start}:{p.char_end}]\n{p.text}" for p in passages
        )
        payload = self._call_provider(
            journal, f"audit.{rule.rule_id}", "audit", rule.rule_id,
            AUDIT_INSTRUCTIONS,
            f"QUESTION:\n{question}\n\nPASSAGES:\n{self._data_region(rendered)}",
            AUDIT_SCHEMA,
        )

        verdict_word = payload.get("verdict")
        expected = rule.check.get("violation_when", "yes")
        if verdict_word == "unclear" or verdict_word is None:
            return Verdict.UNDECIDED, None
        if verdict_word != expected:
            return Verdict.SATISFIED, None

        citation = Citation(
            doc_id=payload.get("doc_id", passages[0].doc_id),
            char_start=payload.get("char_start", 0),
            char_end=payload.get("char_end", 0),
            quoted_text=payload.get("quoted_text", ""),
        )
        source = next(
            (d["text"] for d in self._store.documents_for(corpus_id)
             if d["doc_id"] == citation.doc_id),
            None,
        )
        if source is None or verify_citation(source, citation) is not CitationStatus.VERIFIED:
            return Verdict.UNDECIDED, None
        return Verdict.VIOLATED, citation

    @staticmethod
    def _conflict_dict(c: Conflict) -> dict:
        return {"conflict_id": c.conflict_id, "entry_id": c.entry_id, "field": c.field,
                "kind": c.kind.value, "values": list(c.values),
                "citations": [_citation_dict(x) for x in c.citations],
                "suggested_resolution": c.suggested_resolution}

    @staticmethod
    def _conflict_from(d: dict) -> Conflict:
        return Conflict(
            conflict_id=d["conflict_id"], entry_id=d["entry_id"], field=d["field"],
            kind=ConflictKind(d["kind"]), values=tuple(d["values"]),
            citations=tuple(_citation_from(c) for c in d["citations"]),
            suggested_resolution=d["suggested_resolution"],
        )

    def _build_bundle(self, entries, conflicts, findings, committed_hashes) -> list[ReviewItem]:
        """What the run intends to do, itemised for a person to decide on.

        Only entries that actually moved appear. Asking someone to re-approve
        an entry nothing touched is how review fatigue starts, and review
        fatigue is how a gate stops being a gate.
        """
        conflicted = {c.entry_id for c in conflicts}
        items: list[ReviewItem] = []

        for entry in entries:
            if committed_hashes.get(entry.entry_id) == entry.content_hash:
                continue
            if entry.entry_id in conflicted:
                continue
            items.append(ReviewItem(
                item_id=f"entry:{entry.entry_id}",
                kind=ReviewItemKind.ENTRY,
                summary=f"{entry.vendor} - {entry.field}: {entry.value}",
                payload=_entry_dict(entry),
            ))

        for conflict in conflicts:
            items.append(ReviewItem(
                item_id=f"conflict:{conflict.conflict_id}",
                kind=ReviewItemKind.CONFLICT,
                summary=f"{conflict.field}: sources disagree ({', '.join(conflict.values)})",
                payload=self._conflict_dict(conflict),
            ))

        for finding in findings:
            items.append(ReviewItem(
                item_id=f"finding:{finding.finding_id}",
                kind=ReviewItemKind.FINDING,
                summary=finding.message,
                payload={"rule_id": finding.rule_id, "kind": finding.kind.value,
                         "citation": _citation_dict(finding.citation)},
            ))

        return items

    # --- the human gate ---------------------------------------------------

    def submit_decisions(self, run_id: str, decisions: list[ReviewDecision]) -> Run:
        """Apply the gate, item by item, then commit what was approved."""
        stored = {i["item_id"]: i for i in self._store.load_review_items(run_id)}

        for decision in decisions:
            if decision.item_id not in stored:
                raise UnknownReviewItem(
                    f"{decision.item_id!r} was never proposed by run {run_id}"
                )

        proposal = self._store.load_proposal(run_id)
        if proposal is None:
            raise UnknownReviewItem(f"run {run_id} has nothing awaiting review")

        by_id = {d.item_id: d for d in decisions}
        items = self._rebuild_items(stored.values(), by_id)
        self._store.save_review_items(run_id, items)

        return self._commit(run_id, proposal, items)

    def _rebuild_items(self, stored_rows, by_id) -> list[ReviewItem]:
        items = []
        for row in stored_rows:
            decision = by_id.get(row["item_id"])
            items.append(ReviewItem(
                item_id=row["item_id"],
                kind=ReviewItemKind(row["kind"]),
                summary=row["summary"],
                payload=row["payload"],
                decision=decision.decision if decision else None,
                reason=decision.reason if decision else None,
                resolution=decision.resolution if decision else None,
                # Silence is not approval: an item nobody decided on is not
                # applied, and neither is a rejected one.
                applied=bool(decision and decision.decision is Decision.APPROVE),
            ))
        return items

    def _commit(self, run_id: str, proposal: dict, items: list[ReviewItem]) -> Run:
        corpus_row = self._store.get_run_row(run_id)
        corpus_id = corpus_row["corpus_id"]

        approved = {i.item_id: i for i in items if i.applied}
        previous = self.committed_register(corpus_id)
        surviving = {e.entry_id: e for e in (previous.entries if previous else [])}

        for entry_dict in proposal["entries"]:
            entry = _entry_from(entry_dict)
            item_id = f"entry:{entry.entry_id}"
            conflict_item = next(
                (i for i in approved.values()
                 if i.kind is ReviewItemKind.CONFLICT
                 and i.payload.get("entry_id") == entry.entry_id),
                None,
            )

            if conflict_item is not None:
                # A person settled the disagreement. Their choice, not ours.
                surviving[entry.entry_id] = build_entry(
                    vendor=entry.vendor, field=entry.field,
                    value=conflict_item.resolution,
                    state=EntryState.ASSERTED if conflict_item.resolution else EntryState.DISPUTED,
                    citations=entry.citations,
                    candidate_values=() if conflict_item.resolution else entry.candidate_values,
                )
            elif item_id in approved:
                surviving[entry.entry_id] = entry
            elif entry.entry_id not in surviving:
                continue  # proposed but not approved: it does not land

        version = (proposal["base_version"] or 0) + 1
        entries = [_entry_dict(e) for e in surviving.values()]

        try:
            if self._faults.die_at != "commit.persist":
                self._store.save_deliverable(
                    corpus_id, version, entries, run_id,
                    drop_entries=self._faults.drop_entries_on_persist,
                )
        except Exception as exc:
            if "duplicate key" in str(exc).lower() or "unique" in str(exc).lower():
                raise StaleBaseVersion(
                    f"deliverable for {corpus_id} already advanced past version {version}"
                ) from None
            raise

        self._verify_commit(corpus_id, version, entries)
        self._store.set_run_status(run_id, RunStatus.COMMITTED.value)
        return self.get_run(run_id)

    def _verify_commit(self, corpus_id: str, version: int, intended: list[dict]) -> None:
        """Read back what was written and compare.

        Without this, "committed" would mean "the write did not raise", which
        is not the same claim at all.
        """
        stored = self._store.load_deliverable(corpus_id, version)
        if stored is None:
            raise CommitVerificationFailed(
                f"{corpus_id} version {version} is not readable after commit"
            )

        intended_hashes = sorted(e["content_hash"] for e in intended)
        stored_hashes = sorted(e["content_hash"] for e in stored)
        if intended_hashes != stored_hashes:
            raise CommitVerificationFailed(
                f"{corpus_id} version {version} holds {len(stored_hashes)} entries, "
                f"expected {len(intended_hashes)}"
            )

    # --- resume -----------------------------------------------------------

    def resume(self, run_id: str) -> Run:
        """Continue an interrupted or parked run without redoing finished work."""
        row = self._store.get_run_row(run_id)
        if row is None:
            raise UnknownReviewItem(f"no such run: {run_id}")

        self._last_run_id = run_id
        status = RunStatus(row["status"])

        # Already finished, or already waiting on a person: resuming is a
        # no-op, and must not cost a single call.
        if status in (RunStatus.COMMITTED, RunStatus.AWAITING_REVIEW):
            return self.get_run(run_id)

        corpus_id = row["corpus_id"]
        documents = [
            _document_from(d) for d in self._store.documents_for(corpus_id)
        ]
        corpus = Corpus.from_documents(documents, corpus_id=corpus_id)
        self._store.set_run_status(run_id, RunStatus.RUNNING.value)

        journal = Journal(self._store, run_id)
        done = journal.completed_step_ids()
        for document in corpus.documents:
            self._process_document(run_id, corpus_id, document, journal, done)
        return self._compose_and_gate(run_id, corpus_id, journal, incremental=False)

    # --- reading ----------------------------------------------------------

    def get_run(self, run_id: str) -> Run:
        row = self._store.get_run_row(run_id)
        if row is None:
            raise UnknownReviewItem(f"no such run: {run_id}")

        corpus_id = row["corpus_id"]
        status = RunStatus(row["status"])
        proposal = self._store.load_proposal(run_id) or {
            "entries": [], "conflicts": [], "findings": [], "unsupported": [],
            "impact_set": [], "base_version": None,
        }

        items = [
            ReviewItem(
                item_id=r["item_id"], kind=ReviewItemKind(r["kind"]), summary=r["summary"],
                payload=r["payload"],
                decision=Decision(r["decision"]) if r["decision"] else None,
                reason=r["reason"], resolution=r["resolution"], applied=r["applied"],
            )
            for r in self._store.load_review_items(run_id)
        ]

        if status is RunStatus.COMMITTED:
            register = self.committed_register(corpus_id)
        else:
            register = Register(
                corpus_id=corpus_id,
                version=(proposal["base_version"] or 0) + 1,
                entries=[_entry_from(e) for e in proposal["entries"]],
            )

        return Run(
            run_id=run_id,
            corpus_id=corpus_id,
            status=status,
            register=register,
            review_bundle=ReviewBundle(bundle_id=f"bundle-{run_id}", run_id=run_id, items=items),
            conflicts=tuple(self._conflict_from(c) for c in proposal["conflicts"]),
            findings=tuple(self._finding_from(f) for f in proposal["findings"]),
            unsupported_facts=tuple(
                UnsupportedFact(
                    doc_id=u["doc_id"], field=u["field"], value=u["value"],
                    citation=_citation_from(u["citation"]),
                    status=CitationStatus(u["status"]),
                )
                for u in proposal["unsupported"]
            ),
            cost=aggregate(self.journal(run_id)),
            impact_set=frozenset(proposal["impact_set"]),
        )

    def committed_register(self, corpus_id: str) -> Register | None:
        entries = self._store.load_deliverable(corpus_id)
        if entries is None:
            return None
        return Register(
            corpus_id=corpus_id,
            version=self._store.latest_version(corpus_id) or 1,
            entries=[_entry_from(e) for e in entries],
        )


def _document_from(row: dict):
    from doctask.domain import Document

    return Document(
        doc_id=row["doc_id"], filename=row["filename"], media_type=row["media_type"],
        text=row["text"], sha256=row["sha256"],
    )
