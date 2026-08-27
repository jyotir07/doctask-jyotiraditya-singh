"""Durable step journal and deterministic fault injection.

The journal is the source of truth for what a run has already done. LangGraph
resumes graph state; the journal is what makes an interrupted step exactly
once rather than at-least-once, which is the difference between resuming and
re-paying.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultPlan:
    """Deterministic failure injection, so crash tests are not races.

    die_at                  step id to abort the process at, hard
    fail_once_at            step id to fail once, then succeed on retry
    provider_error_at       step id to raise a ProviderError at
    drop_entries_on_persist rows to silently lose while persisting, to prove
                            the commit verifies its post-state rather than
                            assuming it
    """

    die_at: str | None = None
    fail_once_at: str | None = None
    provider_error_at: str | None = None
    drop_entries_on_persist: int = 0


@dataclass(frozen=True)
class StepRecord:
    step_id: str
    stage: str
    key: str | None
    succeeded: bool
    attempts: int
    provider_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    error: str | None = None


@dataclass(frozen=True)
class JournalView:
    run_id: str
    steps: tuple[StepRecord, ...]


class Journal:
    """Append-only record of every step a run executed."""

    def __init__(self, store, run_id: str) -> None:
        self._store = store
        self._run_id = run_id

    def record(self, record: StepRecord) -> None:
        with self._store.raw_cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_steps (
                    run_id, step_id, stage, "key", succeeded, attempts, provider_calls,
                    input_tokens, output_tokens, duration_ms, error
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._run_id,
                    record.step_id,
                    record.stage,
                    record.key,
                    record.succeeded,
                    record.attempts,
                    record.provider_calls,
                    record.input_tokens,
                    record.output_tokens,
                    record.duration_ms,
                    record.error,
                ),
            )

    def completed_step_ids(self) -> set[str]:
        """Steps this run has already finished, and must never redo."""
        with self._store.raw_cursor() as cur:
            cur.execute(
                "SELECT step_id FROM run_steps WHERE run_id = %s AND succeeded",
                (self._run_id,),
            )
            return {row[0] for row in cur.fetchall()}

    def view(self) -> JournalView:
        with self._store.raw_cursor() as cur:
            cur.execute(
                """
                SELECT step_id, stage, "key", succeeded, attempts, provider_calls,
                       input_tokens, output_tokens, duration_ms, error
                FROM run_steps
                WHERE run_id = %s
                ORDER BY id
                """,
                (self._run_id,),
            )
            steps = tuple(
                StepRecord(
                    step_id=r[0],
                    stage=r[1],
                    key=r[2],
                    succeeded=r[3],
                    attempts=r[4],
                    provider_calls=r[5],
                    input_tokens=r[6],
                    output_tokens=r[7],
                    duration_ms=r[8],
                    error=r[9],
                )
                for r in cur.fetchall()
            )
        return JournalView(run_id=self._run_id, steps=steps)
