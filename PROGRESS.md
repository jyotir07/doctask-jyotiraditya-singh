# Assumptions and decisions

A running log of calls made while building, and the reasoning behind them.
Where the brief was ambiguous I made a reasonable call and wrote it down here
rather than waiting.

---

## Domain

**Vendor contracts, their amendments, and invoices against them.**

Chosen because the conflicts are objective rather than judgement calls. An
amendment changing Net 30 to Net 45 while an invoice still bills Net 30 is a
contradiction anyone can check, which means "the system found a real conflict"
is verifiable rather than a matter of opinion. Domains like project status
reports have softer disagreements, and softer disagreements make an honest
no-findings report harder to trust.

**Declared formats:** PDF, DOCX, MD, TXT, CSV, EML.

A format outside that set raises `UnsupportedFormat` rather than being skipped.
A silently omitted document makes the deliverable quietly incomplete, which is
the exact failure this system exists to prevent.

Spreadsheets are read as a *source* only. Nothing this system produces is a
spreadsheet.

## The deliverable is a set, not a document

The Obligation Register is a set of independently addressable, content-hashed
entries, rendered by a pure function of that set. This is the decision the
whole "stays alive" movement rests on:

- an entry can be rendered on its own, so "nothing else changed" is proven by
  byte equality rather than asserted
- an entry's identity is `(vendor, field)` and deliberately excludes the value,
  so an amendment *updates* the payment-terms entry rather than appending a
  second one beside it
- an entry's content hash covers everything that is part of its meaning and
  nothing else — no clock, no run id, no ordering accident, or every re-run
  would look like a change

## Anti-hallucination is mechanical, not prompted

Every fact carries `(doc_id, char_start, char_end, quoted_text)`. A verifier
re-reads `source[char_start:char_end]` and compares under whitespace
normalisation. A fabricated payment term produces a span that does not hold its
quote and dies before reaching the register.

Rejected facts are **kept**, as unsupported facts with a reason. A system that
quietly drops what it could not verify looks identical, from the outside, to
one that never hallucinated.

The same rule binds findings. A model-backed rule violation whose citation does
not verify is downgraded to UNDECIDED rather than published — an unsupported
accusation is worse than an unsupported fact, because it accuses someone.

## Deviations from the suggested stack

**No LangGraph.** The brief suggests it; a hand-rolled loop is listed as an
equally legitimate choice. The decisive issue is that LangGraph's checkpointer
resumes *graph state* but will re-execute a node interrupted mid-flight, which
re-pays for the model call. What this system needs is work-level idempotency,
and that comes from a durable step journal keyed on deterministic step ids.
Once the journal exists, the graph layer adds a dependency without adding a
capability. The stages are explicit functions with real branches instead.

**Entity resolution is deterministic string matching, not embeddings.** Vendor
identity feeds entry ids; entry ids feed content hashes; content hashes are how
non-modification is proven. A model call anywhere in that chain would make the
proof vary between runs. "Acme Ltd" versus "Acme Limited" is a suffix problem,
not a semantic one. Ambiguity uses the ratio test — the best match must be
clearly ahead of the second, not merely above a threshold — and escalates to a
person when it is not.

**pgvector is used for retrieval, where the question really is semantic.**
"Does this agreement renew automatically" over long prose is what vector search
is for.

**Synchronous core.** Concurrency is proven with real threads on independent
PostgreSQL connections. `asyncio.gather` on one connection would serialise for
reasons unrelated to the locking this system claims to do, and would pass
whether or not that locking existed.

## Assumptions logged

- **Postgres runs on host port 5433, not 5432.** A native PostgreSQL 17 service
  already owned 5432 on the development machine. Connections were silently
  reaching that server instead of the container. A pre-existing local Postgres
  is a likely collision for anyone cloning this, so avoiding the default is the
  more robust choice, not a workaround.
- **A corpus without an explicit id is `"default"`.** Explicit ids are used
  wherever isolation matters.
- **The vendor name is extracted by regex**, not by a model — see the entity
  resolution note above. This is the weakest part of the ingest path and is
  listed under known gaps.
- **Autocommit on every connection.** A journal write buffered inside an open
  transaction would be rolled back by the very crash it exists to survive.
- **Embeddings report their token cost.** A cost line counting completions but
  not embeddings would under-report every run that indexed anything.
- **Failed attempts count toward cost.** You paid for the call that failed, and
  hiding it flatters the run precisely when it went worst.
- **Rule packs refuse to half-load.** A pack that silently dropped an
  unparseable rule would stop checking something nobody realised had stopped
  being checked.

## Tests are verified, not just written

`scripts/mutation_check.py` breaks the code on purpose — 30 realistic defects —
and requires every one to turn the suite red. A survivor names a behaviour
nothing is protecting.

It has repeatedly earned its place. Four examples of real gaps it exposed:

- **Conflict ids were only stable within a process.** Values were collected
  into a `set`, which iterates consistently within one process however it was
  built — so a single-process test passed whether or not the code sorted. That
  order derives from string hashing, which Python randomises per process. The
  ids would have been rock solid in CI and drifted the moment a deployment
  restarted, re-surfacing conflicts a reviewer had already decided.
- **A directory-ordering test passed for the wrong reason.** NTFS returns
  entries already sorted, so removing `sorted()` changed nothing on Windows. It
  would have caught the bug on ext4 and silently missed it here.
- **The gate re-proposed untouched entries on every run.** No invariant caught
  it: the entries did not *change*, they were just put back in front of a
  reviewer. Re-approving forty untouched entries to reach the one that moved is
  how a gate degrades into a rubber stamp.
- **Greedy suffix stripping** only breaks on names where a legal-form word is
  not last. "Company Shop Group Ltd" normalises to the empty string.

Note for CI: run the script unpiped. A pipeline's exit code is the last
command's, so `mutation_check.py | tail` reports success even when mutations
survive. `make mutants` does it correctly.

## Known gaps

Honest list of what does not work yet, or works less well than it reads.

- **N2 (documents must not give orders) is not implemented.** The structural
  half is in place — source text is confined to a delimited data region and
  never joins the instruction region, and the extraction schema is closed — but
  the injection *detector* that raises a finding does not exist. Three tests are
  red and correctly so.
- **`impact_set` is computed by hash comparison, not by tracing evidence.** It
  is correct for what the non-modification invariant asserts, but an update
  still reconciles the whole corpus. It just does not re-extract, re-embed, or
  re-propose anything unchanged. A real blast-radius calculation (entities
  touched, then entries referencing them) is still to come.
- **Vendor extraction is a regex** and will be brittle on real documents.
- **No React review interface yet.** The REST and MCP surfaces are complete and
  tested; the interface is a third client of the same operations.
- **No filesystem watcher yet.** Incremental ingest works and is tested; nothing
  polls a directory to trigger it automatically.
- **Schema migrations are additive DDL in `schema.sql`**, guarded with
  `IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`. Adequate while the schema
  changes daily; it needs numbered migrations before anything real depends on
  it.

## Environment note

The development machine's home directory is itself a git repository with no
`.gitignore`, with `.ssh/`, `.aws/`, and `.git-credentials` sitting untracked
inside its working tree. This project therefore has its own repository and its
own `.gitignore`, and no git command is ever run from the home directory.

**Correction, logged late.** That paragraph was aspirational for most of the
build: the project had *no* `.git` of its own and was sitting inside the home
directory's working tree the whole time. I found this with about two hours left
and fixed it by initialising a repository here. The `.gitignore` was real
throughout; the separate repository was not. Recording it because a progress log
that quietly edits out its own wrong entries is worth nothing.
