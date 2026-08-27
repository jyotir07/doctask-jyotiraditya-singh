"""Cost is derived from the journal rather than accumulated alongside it.

One source of truth, and a useful consequence: a run that crashed can still
say what it spent before it died, because the spending is already durable.

The honesty requirement here is that a failed attempt still costs money. A
meter that counted only successful calls would under-report exactly when a
run was behaving worst.
"""

from __future__ import annotations

from doctask.cost import aggregate
from doctask.kernel import JournalView, StepRecord


def step(step_id, stage, *, succeeded=True, provider_calls=1, tokens_in=100,
         tokens_out=50, duration_ms=10) -> StepRecord:
    return StepRecord(
        step_id=step_id,
        stage=stage,
        key=None,
        succeeded=succeeded,
        attempts=1,
        provider_calls=provider_calls,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        duration_ms=duration_ms,
        error=None if succeeded else "boom",
    )


def view(*steps) -> JournalView:
    return JournalView(run_id="run-1", steps=tuple(steps))


def test_an_empty_run_costs_nothing():
    cost = aggregate(view())

    assert cost.calls == 0
    assert cost.input_tokens == 0
    assert cost.by_stage == {}


def test_calls_are_counted_per_stage():
    cost = aggregate(
        view(
            step("classify.a", "classify"),
            step("extract.a", "extract"),
            step("extract.b", "extract"),
        )
    )

    assert cost.by_stage["classify"].calls == 1
    assert cost.by_stage["extract"].calls == 2


def test_tokens_are_summed_per_stage():
    cost = aggregate(
        view(
            step("extract.a", "extract", tokens_in=100, tokens_out=50),
            step("extract.b", "extract", tokens_in=250, tokens_out=75),
        )
    )

    assert cost.by_stage["extract"].input_tokens == 350
    assert cost.by_stage["extract"].output_tokens == 125


def test_the_total_is_the_sum_of_the_stages():
    cost = aggregate(
        view(step("classify.a", "classify"), step("extract.a", "extract"))
    )

    assert cost.calls == sum(s.calls for s in cost.by_stage.values())
    assert cost.input_tokens == sum(s.input_tokens for s in cost.by_stage.values())


def test_a_failed_attempt_still_costs_what_it_cost():
    """You paid for the call that failed. Reporting otherwise would flatter
    the run precisely when it went worst."""
    cost = aggregate(
        view(
            step("extract.a", "extract", succeeded=False, tokens_in=100),
            step("extract.a", "extract", succeeded=True, tokens_in=100),
        )
    )

    assert cost.by_stage["extract"].calls == 2
    assert cost.by_stage["extract"].input_tokens == 200


def test_a_step_that_never_reached_the_provider_is_not_a_call():
    """Deterministic stages appear in the timing breakdown but must not
    inflate the call count."""
    cost = aggregate(
        view(
            step("extract.a", "extract"),
            step("verify_citations", "verify_citations", provider_calls=0,
                 tokens_in=0, tokens_out=0, duration_ms=3),
        )
    )

    assert cost.calls == 1
    assert cost.by_stage["verify_citations"].calls == 0
    assert cost.by_stage["verify_citations"].duration_ms == 3


def test_time_is_reported_per_stage_and_in_total():
    cost = aggregate(
        view(
            step("classify.a", "classify", duration_ms=10),
            step("extract.a", "extract", duration_ms=40),
        )
    )

    assert cost.by_stage["classify"].duration_ms == 10
    assert cost.by_stage["extract"].duration_ms == 40
    assert cost.duration_ms == 50
