-- Applied once per process per DSN, from Store. Every statement is idempotent,
-- so applying it is safe whether the database is empty or already in use.

-- Declared here rather than only in scripts/init-db.sql, because that file is
-- a docker-entrypoint script and never runs on a managed PostgreSQL. Without
-- this line the vector(64) column below fails with "type vector does not
-- exist" the first time anyone points this at Neon, Supabase or RDS.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    corpus_id   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS runs_corpus_idx ON runs (corpus_id);

-- Append-only. A retry adds a row rather than editing one, so the cost of a
-- failed attempt stays visible and "what happened, in what order" is readable
-- straight off the table.
CREATE TABLE IF NOT EXISTS run_steps (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    step_id       TEXT NOT NULL,
    stage         TEXT NOT NULL,
    "key"         TEXT,
    succeeded     BOOLEAN NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 1,
    provider_calls INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS run_steps_run_idx ON run_steps (run_id, id);
CREATE INDEX IF NOT EXISTS run_steps_completed_idx
    ON run_steps (run_id, step_id) WHERE succeeded;

-- Additive migrations. Kept idempotent and guarded so that connecting to an
-- older database upgrades it in place rather than failing on drift. When the
-- schema stops changing daily this moves to numbered migration files.
ALTER TABLE run_steps ADD COLUMN IF NOT EXISTS provider_calls INTEGER NOT NULL DEFAULT 0;

-- Documents, addressed by content. The unique constraint on (corpus_id,
-- sha256) is the mechanism, not a nicety: re-submitting the same bytes under
-- a new filename must not buy a second extraction.
CREATE TABLE IF NOT EXISTS documents (
    corpus_id   TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    body        TEXT NOT NULL,
    doc_type    TEXT,
    vendor      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (corpus_id, doc_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS documents_content_idx ON documents (corpus_id, sha256);

-- Extraction results, keyed by content rather than by run. This is what makes
-- an update cost like an update and a resume cost nothing: work already done
-- for these bytes is never done again.
CREATE TABLE IF NOT EXISTS extracted_docs (
    doc_sha256        TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    PRIMARY KEY (doc_sha256, extractor_version)
);

CREATE TABLE IF NOT EXISTS facts (
    fact_id     TEXT NOT NULL,
    doc_sha256  TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    quoted_text TEXT NOT NULL,
    PRIMARY KEY (fact_id, doc_sha256)
);

CREATE TABLE IF NOT EXISTS unsupported_facts (
    id          BIGSERIAL PRIMARY KEY,
    doc_sha256  TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    quoted_text TEXT NOT NULL,
    status      TEXT NOT NULL
);

-- One row per version of the deliverable. The primary key doubles as the
-- optimistic concurrency control: two runs racing to commit version N+1 means
-- one of them loses on a unique violation rather than silently overwriting.
CREATE TABLE IF NOT EXISTS deliverables (
    corpus_id  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    entries    JSONB NOT NULL,
    run_id     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (corpus_id, version)
);

-- What a suspended run intends to do. The gate is a durable suspension point,
-- so this is the state a different process picks up when it resumes the run.
CREATE TABLE IF NOT EXISTS run_proposals (
    run_id       TEXT PRIMARY KEY REFERENCES runs (run_id) ON DELETE CASCADE,
    entries      JSONB NOT NULL,
    conflicts    JSONB NOT NULL,
    findings     JSONB NOT NULL,
    unsupported  JSONB NOT NULL,
    impact_set   JSONB NOT NULL,
    base_version INTEGER
);

CREATE TABLE IF NOT EXISTS review_items (
    item_id    TEXT NOT NULL,
    run_id     TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    summary    TEXT NOT NULL,
    payload    JSONB NOT NULL,
    decision   TEXT,
    reason     TEXT,
    resolution TEXT,
    applied    BOOLEAN NOT NULL DEFAULT FALSE,
    position   INTEGER NOT NULL,
    PRIMARY KEY (run_id, item_id)
);

-- Document chunks for retrieval. The character offsets are load-bearing: a
-- citation produced while reading a chunk is mapped back onto the original
-- document through them, so the verifier can still re-read it (N1).
CREATE TABLE IF NOT EXISTS chunks (
    id         BIGSERIAL PRIMARY KEY,
    doc_sha256 TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end   INTEGER NOT NULL,
    body       TEXT NOT NULL,
    embedding  vector(64) NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_doc_idx ON chunks (doc_sha256);
