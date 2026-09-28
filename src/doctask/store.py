"""PostgreSQL persistence.

The store is never faked in tests. A suite that proves its own mocks work
proves nothing about whether two runs can safely commit at once.

Connections run with autocommit on, and that is a correctness decision rather
than a convenience one: a journal write buffered inside an open transaction
would be rolled back by the crash it exists to survive, and resume would
redo -- and re-pay for -- work that had already finished.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import psycopg

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Store:
    """Run state, journal, entries, and the deliverable."""

    # DSNs whose schema this process has already applied. The API builds a
    # Store per request, and re-executing the whole schema on every call costs
    # dozens of round trips once the database is not on localhost.
    _migrated_dsns: set[str] = set()

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        # Bounded, because on Windows a connect to a stopped Docker-published
        # port can hang rather than being refused.
        self._conn = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        if dsn not in Store._migrated_dsns:
            self._migrate()
            Store._migrated_dsns.add(dsn)

    @classmethod
    def connect(cls, dsn: str) -> "Store":
        return cls(dsn)

    def _migrate(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    def reset(self) -> None:
        """Truncate every table. Test setup only.

        Discovered from the catalog rather than listed, so a table added later
        cannot quietly start leaking state between tests.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                """
            )
            tables = [row[0] for row in cur.fetchall()]
            if tables:
                joined = ", ".join(f'"{t}"' for t in tables)
                cur.execute(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def raw_cursor(self):
        with self._conn.cursor() as cur:
            yield cur

    # --- runs -------------------------------------------------------------

    def create_run(self, *, run_id: str, corpus_id: str, status: str = "pending") -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs (run_id, corpus_id, status)
                VALUES (%s, %s, %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id, corpus_id, status),
            )

    def set_run_status(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE runs SET status = %s, error = %s, updated_at = now()
                WHERE run_id = %s
                """,
                (status, error, run_id),
            )

    def get_run_row(self, run_id: str) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, corpus_id, status, error FROM runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "corpus_id": row[1], "status": row[2], "error": row[3]}

    # --- documents --------------------------------------------------------

    def upsert_document(self, corpus_id: str, doc, doc_type: str | None = None,
                        vendor: str | None = None) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                    (corpus_id, doc_id, sha256, filename, media_type, body, doc_type, vendor)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (corpus_id, doc_id) DO UPDATE
                    SET doc_type = COALESCE(EXCLUDED.doc_type, documents.doc_type),
                        vendor   = COALESCE(EXCLUDED.vendor, documents.vendor)
                """,
                (corpus_id, doc.doc_id, doc.sha256, doc.filename, doc.media_type,
                 doc.text, doc_type, vendor),
            )

    def documents_for(self, corpus_id: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, sha256, filename, media_type, body, doc_type, vendor
                FROM documents WHERE corpus_id = %s ORDER BY doc_id
                """,
                (corpus_id,),
            )
            return [
                {"doc_id": r[0], "sha256": r[1], "filename": r[2], "media_type": r[3],
                 "text": r[4], "doc_type": r[5], "vendor": r[6]}
                for r in cur.fetchall()
            ]

    def document_with_content(self, corpus_id: str, sha256: str) -> str | None:
        """The doc_id already holding these bytes, if any."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id FROM documents WHERE corpus_id = %s AND sha256 = %s",
                (corpus_id, sha256),
            )
            row = cur.fetchone()
        return row[0] if row else None

    # --- extraction cache -------------------------------------------------

    def is_extracted(self, doc_sha256: str, extractor_version: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM extracted_docs WHERE doc_sha256 = %s AND extractor_version = %s",
                (doc_sha256, extractor_version),
            )
            return cur.fetchone() is not None

    def mark_extracted(self, doc_sha256: str, extractor_version: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO extracted_docs (doc_sha256, extractor_version) VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (doc_sha256, extractor_version),
            )

    def save_facts(self, doc_sha256: str, facts, unsupported) -> None:
        with self._conn.cursor() as cur:
            for f in facts:
                cur.execute(
                    """
                    INSERT INTO facts
                        (fact_id, doc_sha256, doc_id, field, value, char_start, char_end, quoted_text)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (f.fact_id, doc_sha256, f.doc_id, f.field, f.value,
                     f.citation.char_start, f.citation.char_end, f.citation.quoted_text),
                )
            for u in unsupported:
                cur.execute(
                    """
                    INSERT INTO unsupported_facts
                        (doc_sha256, doc_id, field, value, char_start, char_end, quoted_text, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (doc_sha256, u.doc_id, u.field, u.value, u.citation.char_start,
                     u.citation.char_end, u.citation.quoted_text, u.status.value),
                )

    def facts_for(self, doc_sha256: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT fact_id, doc_id, field, value, char_start, char_end, quoted_text
                FROM facts WHERE doc_sha256 = %s ORDER BY fact_id
                """,
                (doc_sha256,),
            )
            return [
                {"fact_id": r[0], "doc_id": r[1], "field": r[2], "value": r[3],
                 "char_start": r[4], "char_end": r[5], "quoted_text": r[6]}
                for r in cur.fetchall()
            ]

    def unsupported_for(self, doc_sha256: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, field, value, char_start, char_end, quoted_text, status
                FROM unsupported_facts WHERE doc_sha256 = %s ORDER BY id
                """,
                (doc_sha256,),
            )
            return [
                {"doc_id": r[0], "field": r[1], "value": r[2], "char_start": r[3],
                 "char_end": r[4], "quoted_text": r[5], "status": r[6]}
                for r in cur.fetchall()
            ]

    # --- the deliverable --------------------------------------------------

    def latest_version(self, corpus_id: str) -> int | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT max(version) FROM deliverables WHERE corpus_id = %s", (corpus_id,))
            return cur.fetchone()[0]

    def load_deliverable(self, corpus_id: str, version: int | None = None) -> list[dict] | None:
        with self._conn.cursor() as cur:
            if version is None:
                cur.execute(
                    """
                    SELECT entries FROM deliverables WHERE corpus_id = %s
                    ORDER BY version DESC LIMIT 1
                    """,
                    (corpus_id,),
                )
            else:
                cur.execute(
                    "SELECT entries FROM deliverables WHERE corpus_id = %s AND version = %s",
                    (corpus_id, version),
                )
            row = cur.fetchone()
        return row[0] if row else None

    def save_deliverable(self, corpus_id: str, version: int, entries: list[dict],
                         run_id: str, drop_entries: int = 0) -> None:
        """Write a new version of the deliverable.

        The primary key on (corpus_id, version) is the concurrency control: a
        second run racing to write the same version loses here rather than
        silently overwriting the first.

        `drop_entries` exists so a test can simulate a store that loses rows,
        which is how the post-commit verification in N5 is proven to be a real
        check rather than an assumption.
        """
        import json as _json

        payload = entries[: len(entries) - drop_entries] if drop_entries else entries
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO deliverables (corpus_id, version, entries, run_id)
                VALUES (%s, %s, %s, %s)
                """,
                (corpus_id, version, _json.dumps(payload), run_id),
            )

    # --- proposals and the gate -------------------------------------------

    def save_proposal(self, run_id: str, *, entries, conflicts, findings,
                      unsupported, impact_set, base_version) -> None:
        import json as _json

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_proposals
                    (run_id, entries, conflicts, findings, unsupported, impact_set, base_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    entries = EXCLUDED.entries, conflicts = EXCLUDED.conflicts,
                    findings = EXCLUDED.findings, unsupported = EXCLUDED.unsupported,
                    impact_set = EXCLUDED.impact_set, base_version = EXCLUDED.base_version
                """,
                (run_id, _json.dumps(entries), _json.dumps(conflicts), _json.dumps(findings),
                 _json.dumps(unsupported), _json.dumps(sorted(impact_set)), base_version),
            )

    def load_proposal(self, run_id: str) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT entries, conflicts, findings, unsupported, impact_set, base_version
                FROM run_proposals WHERE run_id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"entries": row[0], "conflicts": row[1], "findings": row[2],
                "unsupported": row[3], "impact_set": row[4], "base_version": row[5]}

    def save_review_items(self, run_id: str, items) -> None:
        import json as _json

        with self._conn.cursor() as cur:
            for position, item in enumerate(items):
                cur.execute(
                    """
                    INSERT INTO review_items
                        (item_id, run_id, kind, summary, payload, decision, reason,
                         resolution, applied, position)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, item_id) DO UPDATE SET
                        decision = EXCLUDED.decision, reason = EXCLUDED.reason,
                        resolution = EXCLUDED.resolution, applied = EXCLUDED.applied
                    """,
                    (item.item_id, run_id, item.kind.value, item.summary,
                     _json.dumps(item.payload),
                     item.decision.value if item.decision else None,
                     item.reason, item.resolution, item.applied, position),
                )

    def load_review_items(self, run_id: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT item_id, kind, summary, payload, decision, reason, resolution, applied
                FROM review_items WHERE run_id = %s ORDER BY position
                """,
                (run_id,),
            )
            return [
                {"item_id": r[0], "kind": r[1], "summary": r[2], "payload": r[3],
                 "decision": r[4], "reason": r[5], "resolution": r[6], "applied": r[7]}
                for r in cur.fetchall()
            ]

    # --- advisory risk assessments ----------------------------------------

    def get_risk_assessment(self, assessment_key: str) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT model_version, risk_level, risk_probabilities, escalation_probability,
                       finding_category, category_probabilities, assessed_at
                FROM risk_assessments WHERE assessment_key = %s
                """,
                (assessment_key,),
            )
            r = cur.fetchone()
        if r is None:
            return None
        return {"model_version": r[0], "risk_level": r[1], "risk_probabilities": r[2],
                "escalation_probability": r[3], "finding_category": r[4],
                "category_probabilities": r[5], "assessed_at": r[6].isoformat()}

    def save_risk_assessment(self, assessment_key: str, *, item_kind: str,
                             model_requested: str, model_version: str, risk_level: str,
                             risk_probabilities: dict, escalation_probability: float,
                             finding_category: str, category_probabilities: dict,
                             input_tokens: int, output_tokens: int) -> None:
        """First writer wins: two concurrent assessments of the same content
        were asked the same question, so either answer is the answer."""
        import json as _json

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO risk_assessments
                    (assessment_key, item_kind, model_requested, model_version, risk_level,
                     risk_probabilities, escalation_probability, finding_category,
                     category_probabilities, input_tokens, output_tokens)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (assessment_key) DO NOTHING
                """,
                (assessment_key, item_kind, model_requested, model_version, risk_level,
                 _json.dumps(risk_probabilities), escalation_probability, finding_category,
                 _json.dumps(category_probabilities), input_tokens, output_tokens),
            )
