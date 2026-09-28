# doctask

An agentic system that owns a pile of vendor contracts, amendments and invoices
end to end: it works out what each document is, extracts the facts that matter,
notices where the documents disagree, and composes one grounded deliverable — an
**Obligation Register** — in which every claim traces to the exact bytes it came
from. It then checks that register against a rule pack, and a human gates every
commit.

Built for the SuperDocs Round 2 engineering task.

---

## Quick start

Prerequisites: Docker Desktop running, and Python 3.11+.

```bash
make install # .venv with the package and test dependencies
make up      # PostgreSQL 16 + pgvector, healthy
make test    # the full suite — NO API KEY REQUIRED
make demo    # acme-v1, end to end, ingest to committed register
make serve   # the browser review console on http://localhost:8000
```

Then the second run, on different documents inside the same declared set:

```bash
make demo CORPUS=globex-v1
```

`make reset` empties the demo database. Without it a repeated demo finds every
document already extracted and correctly skips them all — which is the
focused-update behaviour working, but not the run you meant to show.

On Windows without `make`, every target has a PowerShell equivalent:

```powershell
.\scripts\demo.ps1 install
.\scripts\demo.ps1 up
.\scripts\demo.ps1 demo -Corpus globex-v1
.\scripts\demo.ps1 serve
```

There is no key to configure and nothing to sign up for. `.env.example` works
untouched, because the default provider is the deterministic fake described
under [The provider seam](#the-provider-seam).

**If something hangs:** `docker ps` must return instantly. If it does not,
restart Docker Desktop (it can take a few minutes to answer again). If
`docker ps` shows no `doctask-db`, run `make up`: with the container stopped,
commands fail after a 10-second connect timeout per address.

### The browser demo

`make serve`, then open <http://localhost:8000>. The page is a client of the
REST routes below and can do nothing they cannot. A walk-through that shows all
three movements:

1. **Understand.** Pick `acme-v1`, untick `appendix-a.pdf` to hold it back, and
   start the run. It stops at the gate. One claim is shown **refused**: the
   script deliberately misquotes the late-payment rate, and the citation
   verifier rejects it before it reaches the register.
2. **Click any citation.** The source panel shows the stored text with the
   exact span highlighted, and whether the quote holds against those bytes.
   *Optional:* flip the **Risk intelligence** switch in the header for Jev's
   advisory read of each conflict and finding (see
   [Risk intelligence](#risk-intelligence-optional-advisory)). With the switch
   off the console is unchanged; with Jev off on the server, each item says
   *Assessment unavailable* and nothing else changes.
3. **Examine, and gate it.** Approve some items and reject one with a reason.
   The payment-terms conflict can be resolved to one value or left disputed.
   Submit: only what was approved lands.
4. **Stay alive.** Ingest the held-back `appendix-a`. The new run costs 4
   provider calls instead of 16, reports exactly one entry changed, and the
   register outlines that entry — every other entry is byte-identical.

The API explorer is at <http://localhost:8000/docs>.

## Declared formats and domains

**Formats:** `.pdf`, `.docx`, `.md`, `.txt`, `.csv`, `.eml`

**Domain:** vendor contracts, their amendments, and invoices raised against them.

A file outside the declared set raises `UnsupportedFormat` rather than being
skipped. A silently omitted document makes the deliverable quietly incomplete,
which is the exact failure this system exists to prevent.

Both demo corpora contain all-synthetic documents for fictional companies. The
`acme-v1` corpus exercises every one of the six formats.

I chose this domain because its conflicts are **objective rather than
judgement calls**. An amendment moving Net 30 to Net 45 while an invoice still
bills Net 30 is a contradiction anyone can check by reading. That matters,
because it makes "the system found a real conflict" verifiable instead of a
matter of taste — and it makes an honest report of *no* findings trustworthy.

---

## The three movements

### 1. Understand

`ingest` → `classify` → `extract` → `entities` → `reconcile` → `register`

Documents are addressed by **sha256 of their bytes**, so renaming a file never
buys a second extraction. Classification has real branches: low confidence
escalates to a person, an unrecognised document type is skipped with a logged
reason. Extraction is schema-constrained, with one bounded repair attempt.

Every extracted fact carries `(doc_id, char_start, char_end, quoted_text)`.

### 2. Examine

A rule pack is **data, not code** (`rulepacks/contract-playbook.yaml`). Adding a
rule, moving a threshold, or covering a new document type is an edit to that
file and nothing else.

Evaluation is staged by cost:

| Stage | What runs | Spend |
|---|---|---|
| 1 | Deterministic checks — `required`, `numeric_range`, `not_disputed`, `forbidden_value` | zero tokens |
| 0 | Injection detection — imperative prose aimed at the system | zero tokens |
| 2 | Model-judged rules, over pgvector-retrieved passages, only where stage 1 could not decide | metered |

The `judge` callable is **injected** into `evaluate()` rather than reached for
internally, which is what makes "the free stage is provably free" a testable
claim instead of an intention: a test passes no judge at all and asserts the
finding still appears.

A clean corpus produces `findings: []` alongside the count of rules actually
checked, so "nothing was wrong" can never be confused with "nothing ran".

### 3. Stay alive

The register is **a set of independently addressable, content-hashed entries**,
rendered by a pure function of that set. This is the decision the whole movement
rests on:

- an entry renders on its own, so "nothing else changed" is proven by **byte
  equality**, not asserted
- an entry's identity is `(vendor, field)` and deliberately **excludes the
  value**, so an amendment *updates* the payment-terms entry rather than
  appending a second one beside it
- an entry's content hash covers everything that is part of its meaning and
  nothing else — no clock, no run id, no ordering accident — or every re-run
  would look like a change

Ingesting a new document re-extracts nothing already extracted (the fact cache
is keyed on `(doc_sha256, extractor_version)`) and re-embeds nothing already
embedded.

### And a human gates all of it

The gate is a **durable suspension point**, not a callback. The run parks in
`AWAITING_REVIEW` with its proposal in PostgreSQL; the process can exit
entirely, and a *different* process can resume and commit. Decisions are
per-item: rejecting one finding does not discard the others, and silence is not
approval — an item with no decision is not applied.

---

## Architecture, and why

```
corpus ─► ingest ─► classify ─► extract ─► verify ─┬─► facts
                       │           │               └─► unsupported (kept, not dropped)
                       │           └─ 1 bounded repair attempt
                       └─ escalate / skip, with reason
                                    │
                 entities ─► reconcile ─► compose ─► audit ─► GATE ─► commit ─► verify
                                                                │
                                                    (durable suspension point)
```

Everything above runs on top of a **durable step journal** in PostgreSQL, keyed
on deterministic step ids.

### Why not LangGraph

The brief suggests LangGraph and names a hand-rolled loop as an equally
legitimate choice. I went with the loop, for one decisive reason:

LangGraph's checkpointer resumes *graph state*, but a node interrupted
mid-flight is **re-executed** on resume — which re-pays for the model call
inside it. What this system needs is **work-level** idempotency: the unit that
must not repeat is the provider call, not the node. That comes from a step
journal keyed on deterministic ids, with the outcome recorded before the step
is considered done.

Once that journal exists, the graph layer adds a dependency without adding a
capability. So the stages are explicit functions with real branches, and the
journal is the thing that makes a crash survivable.

**What that costs:** no free visualisation, no ecosystem of pre-built nodes,
and I own the retry semantics myself. I think that is the right trade at this
size, and it is the honest reason a reviewer will not find LangGraph in the
dependency list.

### Why journal writes are autocommit

A journal write buffered inside an open transaction would be rolled back by the
very crash it exists to survive. Every connection is autocommit.

### Why entity resolution is deterministic but retrieval is not

Vendor identity feeds entry ids; entry ids feed content hashes; content hashes
are how non-modification is *proven*. A model call anywhere in that chain would
make the proof vary between runs. "Acme Ltd" vs "Acme Limited" is a suffix
problem, not a semantic one.

Ambiguity uses a **ratio test** — the best match must be clearly ahead of the
runner-up (margin 0.10), not merely above a threshold (0.75) — and escalates to
a person when it is not. I arrived at those numbers by measuring actual
`SequenceMatcher` scores on the vendor names in the corpora, not by guessing.

pgvector is used where the question really *is* semantic: "does this agreement
renew automatically" over long prose is what vector search is for. Chunks carry
the character offsets they were cut from, so a citation a model produces while
reading a retrieved passage still verifies against the original document.

### Why the core is synchronous

Concurrency is proven with **real threads on independent connections**.
`asyncio.gather` on a single connection would serialise for reasons unrelated to
the locking this system claims to do, and would pass whether or not that locking
existed.

### The provider seam

Orchestration never imports a vendor SDK. It talks to a `Provider` protocol with
one method. Two implementations:

- **`FakeProvider`** — deterministic, script-keyed on `(stage, key)`, and it
  **counts every call**. It ships in the package rather than the test tree, so a
  stranger runs the whole demo with no key. Everything downstream of the script
  — parsing, citation construction, verification, reconciliation, composition,
  the gate — is real production code, so the tests are not testing their own
  mocks.
- **Anthropic adapter** — *this is a stub.* See [Honest gaps](#honest-gaps).

---

## It never bluffs, and the mechanism is not a prompt

Every fact carries a citation. `verify_citation` re-reads
`source[char_start:char_end]` from the original bytes and compares it to the
quoted text under whitespace normalisation.

A fabricated payment term produces a span that does not hold its quote, and it
**dies before it reaches the register**. No prompt asks the model to be
truthful; the check is arithmetic on byte offsets.

Two consequences worth stating plainly:

**Rejected facts are kept**, as `UnsupportedFact` with a reason. A system that
quietly drops what it could not verify looks identical, from the outside, to one
that never hallucinated in the first place.

**The same rule binds findings.** A model-backed rule violation whose citation
does not verify is downgraded to `UNDECIDED` rather than published. An
unsupported accusation is worse than an unsupported fact, because it accuses
someone.

An entry with zero surviving citations cannot be published at all.

---

## The ten invariants

The hard part was written test-first: ten properties the system must *never*
violate, each written as a failing test before the code that satisfies it.

| # | The system must never… | Proven by |
|---|---|---|
| N1 | publish a claim whose citation does not re-verify against source bytes | `tests/invariants/test_n1_no_uncited_claims.py` |
| N2 | let document content alter control flow, tool choice, or a rule verdict | `tests/invariants/test_n2_no_orders_from_documents.py` |
| N3 | modify a register entry the new evidence did not touch | `tests/invariants/test_n3_untouched_is_byte_identical.py` |
| N4 | commit an unapproved item, or let one rejection discard other decisions | `tests/invariants/test_n4_gate_is_honored.py` |
| N5 | report success unless the deliverable is genuinely in that state | `tests/invariants/test_n5_success_means_success.py` |
| N6 | silently resolve a contradiction between two sources | `tests/invariants/test_n6_conflicts_surface.py` |
| N7 | lose or duplicate finished work across a crash | `tests/invariants/test_n7_resume_without_loss.py` |
| N8 | corrupt state when two runs overlap | `tests/invariants/test_n8_concurrent_runs_isolated.py` |
| N9 | re-pay for a model call already made | `tests/invariants/test_n9_no_double_spend.py` |
| N10 | write a secret to a log, a row, or an artifact | `tests/invariants/test_n10_no_secret_leakage.py` |

**All ten are green.**

The crash test is not a race: `FaultPlan` names the exact step id to die at, so
"killed mid-run" is deterministic and reproducible rather than timing-dependent.

---

## Tests are verified, not merely written

Every test runs against **real PostgreSQL**. Only the model is faked.

```
$ make test
288 passed in 29.62s
```

Measured on this machine at the time of writing, not remembered.

Beyond that, `scripts/mutation_check.py` breaks the code on purpose — **30
realistic defects** — and requires every one to turn the suite red. A survivor
names a behaviour that nothing is actually protecting.

```bash
make mutants   # run unpiped: a pipeline's exit code is the last command's,
               # so `mutation_check.py | tail` reports success even on survivors
```

It repeatedly earned its place. Four real gaps it exposed:

- **Conflict ids were only stable within a process.** Values were collected into
  a `set`, which iterates consistently within one process however it was built —
  so a single-process test passed whether or not the code sorted. That order
  derives from string hashing, which Python randomises *per process*. The ids
  would have been rock-solid in CI and drifted the moment a deployment
  restarted, re-surfacing conflicts a reviewer had already decided.
- **A directory-ordering test passed for the wrong reason.** NTFS returns entries
  already sorted, so deleting `sorted()` changed nothing on Windows. The test
  would have caught the bug on ext4 and silently missed it here.
- **The gate re-proposed untouched entries on every run.** No invariant caught
  it — the entries did not *change*, they were just put back in front of a
  reviewer. Re-approving forty untouched entries to reach the one that moved is
  how a gate degrades into a rubber stamp.
- **Greedy suffix stripping** broke on names where the legal-form word is not
  last: "Company Shop Group Ltd" normalised to the empty string.

---

## It knows what it cost

Real output from `make demo` on the six-document `acme-v1` corpus:

```
COST    19 provider calls, 3508 in / 389 out, 51 ms
  audit          1 calls     515 in     27 out       6 ms
  classify       6 calls    1003 in     59 out       0 ms
  compose        0 calls       0 in      0 out       2 ms
  extract        6 calls    1153 in    303 out       0 ms
  index          6 calls     837 in      0 out      28 ms
  reconcile      0 calls       0 in      0 out      15 ms
  verify_citations   0 calls       0 in      0 out       0 ms
```

Note what the zeroes prove: `compose`, `reconcile` and `verify_citations` are
stages that cost **no provider calls at all**, and the audit spent exactly one
call — four of the five rules in the pack are arithmetic and never reach a
model.

Cost is **derived from the journal** rather than accumulated beside it, so there
is one source of truth — and a crashed run can still report what it spent before
it died, because the spending was already durable.

Two deliberate accounting choices:

- **Failed attempts count.** You paid for the call that failed, and a meter that
  hid it would flatter the run precisely when it went worst.
- **Embeddings are billed.** A cost line counting completions but not embeddings
  would under-report every run that indexed anything. (This one was a real bug I
  found and fixed by journalling the index step — not by loosening the test.)

---

## Machine interface

Three clients, one set of operations. None of them can do anything the others
cannot.

- **REST** (`src/doctask/api.py`) — `POST /runs`, `POST /runs/ingest`,
  `GET /runs/{id}`, `GET /runs/{id}/review`, `POST /runs/{id}/decisions`,
  `POST /runs/{id}/resume`, `GET /runs/{id}/cost`, `GET /runs/{id}/provenance`,
  `POST /runs/{id}/risk`, `GET /corpora/{id}/deliverable`,
  `GET /corpora/{id}/documents/{doc_id}`
- **Scripted demo** (same file, enabled when the app is given a corpora
  directory) — `GET /demo/corpora`, `POST /demo/{corpus}/runs` (optionally
  holding documents back), `POST /demo/{corpus}/ingest/{doc_id}`. Only starting
  a run is demo-specific; the gate and every read use the routes above.
- **Browser** (`src/doctask/static/index.html`, served at `/`) — a client of
  the REST routes, with no build step and no CDN
- **MCP** (`src/doctask/mcp_server.py`) — the same operations as tools:
  `start_run`, `ingest_document`, `get_review_bundle`, `decide`, `resume_run`,
  `export_deliverable`, `get_run_cost`, `get_provenance`, `assess_risk`
- **CLI** (`src/doctask/cli.py`) — what `make demo` drives

Approval is an explicit operation on all three surfaces, so a program can drive
the entire flow, gate included, without a human touching a UI.

---

## Risk intelligence (optional, advisory)

An optional Jev-backed second opinion on the conflicts and findings waiting at
the gate. It is **off by default**, and nothing in the three movements depends
on it.

### What it does

`POST /runs/{id}/risk` (the MCP `assess_risk` tool, or **Assess risk** in the
browser) sends each `conflict` and `finding` review item to Jev
(`POST {DOCTASK_JEV_BASE_URL}/v1/systemone`, model `jev-latest`). It asks three
typed questions in one request:

| Question | Type | Answers |
|---|---|---|
| `risk_level` | choice | `low` · `medium` · `high` · `critical` |
| `needs_escalation` | noul | probability of "yes, escalate" |
| `finding_category` | choice | `conflict` · `policy_violation` · `unsupported_claim` · `other` |

The answers appear on each item in `GET /runs/{id}/review` under `risk`. In the
browser, the **Risk intelligence** switch in the header shows them as a compact
block on each item: a risk badge, the category, the escalation percentage and
the status, labelled **advisory**. Turning the switch on assesses the open run.
The switch is only a view setting and remembers its state per browser.
Whether Jev may be called at all is server configuration, never something a
browser can turn on; the chip beside the switch shows which.
Plain register entries are not assessed; they are not findings.

**What Jev sees** is bounded by `assessment_state()`: the item's kind, summary,
rule id or conflict values, and at most five cited spans of at most 500
characters each. It never sees whole documents or the register, and it never
sees the system's `suggested_resolution`, so the model is not nudged by it.

### Why it cannot touch the gate

The boundary is structural, not a convention. `RiskAssessor` holds a `Store`
and an HTTP client, never an `Engine`, and it writes only to its own
`risk_assessments` table. Deterministic validation has already run by the time
a run reaches the gate, and the assessor runs after that. Nothing in `engine.py`,
the rule evaluator, decisions or commit reads its output. Tests prove this with
a "critical, escalate" verdict: it changes no decision and no finding, and it
cannot stop an approval. A "low, no escalation" verdict removes no rule finding.

### Setup

```bash
DOCTASK_JEV_ENABLED=true
JEVMODEL_API_KEY=...                      # server-side only
DOCTASK_JEV_BASE_URL=https://jevmodel.org # default
DOCTASK_JEV_TIMEOUT_S=10                  # default
```

The server (`asgi.py`, i.e. `make serve`) also reads them from a local `.env`,
without overriding variables already set in the environment. The CLI and the
MCP server's `main()` read the process environment only. The key lives on a
private attribute of `JevClient`, is excluded from `JevConfig`'s repr, and
appears in no response, row, error message or log line. A test with an
upstream that echoes the request headers back proves it.

CLI demo with the extra step:

```bash
python -m doctask.cli demo --corpus acme-v1 --assess-risk
```

### Fallback

Every failure degrades to `"status": "unavailable"` with a reason on the item,
never an error on the request: `disabled`, `missing_key`, `timeout`,
`auth_failed`, `rate_limited`, `upstream_error` or `invalid_response`. The
browser shows **Assessment unavailable** and the review works exactly as it
does with the feature off. A timeout, an auth failure or a rate limit stops the
loop after the first item, because they would repeat identically for the rest.

The parser is strict. An answer outside the declared options or types, or a
probability outside [0, 1], is `invalid_response`, never a guess.

### Cost and idempotency

- **Assessments are keyed by content.** The key is
  `sha256(question-set version, model, bounded state)`, not the run or item id.
  An unchanged finding re-proposed by a later run, such as the payment-terms
  conflict after `appendix-a` arrives, costs **zero calls**. A test asserts
  exactly that.
- **Only successes are stored.** A failed assessment can be retried later.
- **Each row records** the model version Jev reports (for example
  `jev-1.13.0`), the timestamp, and the input and output tokens.
- **Retries happen at most once**, and only where the first attempt was not
  processed: a 429 or 529, or a connection that never opened. A read timeout is
  **never** retried, because the request may already have been billed.
- **The same key goes out as an `Idempotency-Key` header**, identical across
  retries and across runs.

### Limitations

- **Server-side deduplication is unverified.** I could not find the
  `Idempotency-Key` header in the public Jev API reference, so I cannot confirm
  the service deduplicates on it. The local content-keyed cache is what
  actually guarantees a finding is paid for once. The header is extra
  protection, not the mechanism.
- **Tested against mocks only.** Every test runs against `httpx.MockTransport`.
  The integration has not been exercised against the live endpoint with a real
  key, so treat the first live run as a check of the wire format.
- **The cache pins the first answer.** The cache keys on the requested model
  name (`jev-latest`), not the version that answered, so an assessment made
  before a model upgrade is kept. Bumping `QUESTION_SET_VERSION` in `risk.py`
  invalidates every stored assessment.
- **Jev calls are not in the run's cost panel.** They are recorded in
  `risk_assessments` rather than the run journal, because the assessment
  belongs to the content and not to a run.
- **The risk calibration is unvalidated.** Jev's risk levels are a model's
  judgement, and no one has checked them against labelled outcomes in this
  domain. That is the other reason they are advisory.

---

## Deploying

`Dockerfile` and `render.yaml` build and run the REST surface. The entry point
is `doctask.asgi:app`, which wires `build_app` from environment and adds a
`/health` route that does a real database round trip — a check that only proved
the process was alive would report green while every request failed on a dead
connection.

```bash
docker build -t doctask-api .
docker run -p 8000:8000 -e DOCTASK_DSN="postgresql://..." doctask-api
```

The database is deliberately not declared in `render.yaml`. Render's own free
PostgreSQL expires after 90 days and this system needs `pgvector`, so the
intended pairing is an external Neon or Supabase instance with its pooled
connection string set as `DOCTASK_DSN` in the dashboard. Nothing secret is
committed: that variable is declared `sync: false`.

**What a deployed instance can and cannot do.** `AnthropicProvider` is a stub,
so the only working provider is the deterministic fake, which answers from a
script keyed on `(stage, doc_id)`. A hosted instance therefore serves every read
path, the whole gate, and the browser demo over the shipped corpora, but
`POST /runs` with arbitrary documents fails with `MissingScriptEntry`. That is a
real limitation of the stub, not a deployment mistake, and it disappears when
the live adapter exists. (The image sets `DOCTASK_ROOT=/app` so the installed
package can find `corpora/` and `rulepacks/`; I have not built the image since
adding that.)

## Assumptions I logged

Full reasoning in [`PROGRESS.md`](PROGRESS.md). In brief:

- **PostgreSQL is on host port 5433, not 5432.** A native PostgreSQL 17 service
  already owned 5432 on my machine and connections were silently reaching *it*
  instead of the container. A pre-existing local Postgres is a likely collision
  for anyone cloning this, so avoiding the default is the more robust choice,
  not a workaround.
- A corpus without an explicit id is `"default"`.
- The vendor name is extracted **by regex**, not by a model — see the entity
  resolution note above. This is the weakest link in the ingest path and is
  listed under honest gaps.
- Journal connections are autocommit.
- Embeddings report their token cost; failed attempts count toward cost.
- **Rule packs refuse to half-load.** A pack that silently dropped an
  unparseable rule would stop checking something nobody realised had stopped
  being checked.

---

## What I cut, and why

The brief says a defended cut beats a hollow stage. These are the cuts.

**The filesystem watcher.** Incremental ingest is built and tested; what is
missing is only the poller that triggers it automatically. `Engine.ingest` is
the operation a watcher would call.

**The React review interface.** Replaced by a single static page (see
[The browser demo](#the-browser-demo)): no framework, no build step. It is a
client of operations that already exist, so the smaller version removes
polish, not capability.

**Real blast-radius tracing.** `impact_set` is computed by hash comparison
rather than by tracing evidence. It is correct for what N3 asserts — untouched
entries are provably untouched — but an update still *reconciles* the whole
corpus, even though it re-extracts, re-embeds and re-proposes nothing unchanged.

**Task 2 of the round (the Go CLI) is not built.** Two independent reasons: I
had no SuperDocs account or API key, and the assigned card's entire value is
release engineering — signed archives, checksums, a Homebrew tap, a Scoop
manifest, reproducible builds. A Go binary hitting four endpoints with none of
that would be exactly the hollow stage the brief warns against. Go 1.26 is
installed on the machine; this was a time-and-credentials cut, not a capability
one.

## Honest gaps

Things that do not work, or work less well than they read.

- **The Anthropic adapter is a stub.** The provider seam is real and the fake is
  a real instrument, but no live call has ever been made through this system. A
  capability may be honestly absent; it must never be present and broken, so I
  am saying it here rather than shipping something that looks live.
- **Vendor extraction is a regex** and will be brittle on real documents. The
  right fix is a deterministically-cached model call, so identity stays stable
  across runs while handling messier names.
- **Schema migrations are additive DDL** in `schema.sql`, guarded with
  `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`. Fine while the schema changes
  daily; it needs numbered migrations before anything real depends on it.
- **The demo uses the fake provider.** That is deliberate and the brief sanctions
  it — tests and demos must run without a live key — but it does mean the demo
  shows the *system* working, not a model's extraction quality.
- **`make demo` was added late** and has had far less use than the test suite.
- **A human conflict resolution is not remembered across runs.** Resolve
  payment terms to Net 45, then ingest an unrelated document, and the next run
  recomposes the entry as disputed again, re-proposes it as changed, and puts
  the same conflict back in front of the reviewer. Unresolved conflicts are
  also re-listed at every gate. The fix is to key stored resolutions on the
  conflict id, which already changes whenever the disagreeing values do.

---

## Repository layout

```
src/doctask/
  kernel.py       durable step journal, deterministic ids, fault injection
  store.py        PostgreSQL access; every connection autocommit
  llm.py          provider protocol, deterministic fake, embeddings
  ingest.py       sha256 addressing, one parser per declared format
  classify.py     document type, with escalate / skip branches
  extract.py      schema-constrained extraction, bounded repair
  provenance.py   the citation verifier — the anti-hallucination mechanism
  entities.py     deterministic vendor resolution, ratio test, escalation
  reconcile.py    conflict detection and supersession, as data
  register.py     addressable entries, content hashing, render, diff
  rules.py        rule-pack loader and the staged evaluator
  retrieval.py    chunking with offsets, pgvector search
  engine.py       the stages, the gate, commit and post-commit verification
  cost.py         per-stage cost, derived from the journal
  api.py          FastAPI surface, including the scripted demo routes
  risk.py         optional, advisory Jev risk assessment of conflicts and findings
  static/         index.html — the browser review console
  demo.py         loads a demo corpus and turns its script into fake responses
  mcp_server.py   MCP surface
  cli.py          the demo client
corpora/          acme-v1 (all six formats), globex-v1 (second run)
rulepacks/        contract-playbook.yaml
scripts/          mutation_check.py — 30 planted defects; demo.ps1 — make for Windows
tests/
  invariants/     the ten NEVER tests
  unit/           parsers, hashing, diff, rules, entities, retrieval
  integration/    full runs against real PostgreSQL
```

## AI disclosure

This system was built with heavy use of Claude (Claude Code), which the brief
asks about directly. The architecture decisions, the invariant list, the domain
choice, and every trade-off defended above are mine; the majority of the
implementation typing, and the first drafts of the test suite, were AI-assisted
under close review. The mutation-testing harness exists precisely because I did
not want to trust either of us about whether the tests were real.

to run(post fix):
- docker ps
-  .\scripts\demo.ps1 up / reset / serve / down 
