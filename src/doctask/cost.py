"""What a run spent, and where the time went.

Derived from the journal rather than accumulated beside it, so there is one
source of truth -- and a crashed run can still report what it spent before it
died, because the spending was already durable.
"""

from __future__ import annotations

from collections import defaultdict

from doctask.domain import RunCost, StageCost
from doctask.kernel import JournalView


def aggregate(view: JournalView) -> RunCost:
    """Roll a run's journal up into per-stage and total cost.

    Failed attempts count. You paid for the call that failed, and a meter that
    hid it would flatter the run precisely when it went worst.
    """
    calls: dict[str, int] = defaultdict(int)
    tokens_in: dict[str, int] = defaultdict(int)
    tokens_out: dict[str, int] = defaultdict(int)
    duration: dict[str, int] = defaultdict(int)

    for step in view.steps:
        calls[step.stage] += step.provider_calls
        tokens_in[step.stage] += step.input_tokens
        tokens_out[step.stage] += step.output_tokens
        duration[step.stage] += step.duration_ms

    by_stage = {
        stage: StageCost(
            calls=calls[stage],
            input_tokens=tokens_in[stage],
            output_tokens=tokens_out[stage],
            duration_ms=duration[stage],
        )
        for stage in duration
    }

    return RunCost(
        calls=sum(calls.values()),
        input_tokens=sum(tokens_in.values()),
        output_tokens=sum(tokens_out.values()),
        duration_ms=sum(duration.values()),
        by_stage=by_stage,
    )
