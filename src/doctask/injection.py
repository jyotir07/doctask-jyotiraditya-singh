"""Detecting a document that tries to give orders.

The real defence against injection is structural and lives elsewhere: source
text is confined to a delimited data region and never joins the instruction
region, and the extraction schema is closed, so a document can emit facts but
has no vocabulary in which to emit control flow. That is what makes the
attempt *ineffective*.

This module does the other half -- making the attempt *visible*. A source
document containing instructions aimed at the system is data to report on, so
it becomes a finding with a citation, exactly like any other claim.

Deliberately deterministic. A model asked "is this an injection?" is itself
reading attacker-controlled text to decide something, which is the shape of
problem we are trying to avoid. Hard-coded patterns layered alongside the
structural defence are a legitimate fix, not a patch: neither one is load
bearing on its own.
"""

from __future__ import annotations

import re

RULE_ID = "no-orders-from-documents"

# Ordered by how unambiguous each phrase is. Anything here is imperative
# prose aimed at a reader-that-acts; none of it belongs in a contract.
PATTERNS: tuple[str, ...] = (
    r"SYSTEM\s+INSTRUCTION",
    r"SYSTEM\s+PROMPT",
    r"ignore\s+(?:all\s+)?(?:previous|prior|above|preceding)\s+instructions",
    r"disregard\s+(?:all\s+)?(?:previous|prior|above|preceding)",
    r"you\s+are\s+now\s+(?:a|an)\b",
    r"new\s+instructions\s*:",
    r"approve\s+all\s+(?:findings|items|changes)",
    r"mark\s+(?:every|all)\s+\w+\s+as\s+compliant",
    r"do\s+not\s+report\s+any",
)

_COMPILED = tuple(re.compile(p, re.IGNORECASE) for p in PATTERNS)


def detect_injection(text: str) -> tuple[int, int, str] | None:
    """The earliest imperative span in `text`, or None.

    Returns `(char_start, char_end, quoted_text)` where `quoted_text` is
    exactly `text[char_start:char_end]`, so the citation built from it
    verifies under the same verifier every other claim faces. Reporting an
    injection with a citation that did not hold would be its own small bluff.

    Earliest rather than most-severe: the span is evidence a reviewer reads in
    context, and the top of the injected passage is where that context starts.
    """
    earliest: tuple[int, int] | None = None
    for pattern in _COMPILED:
        match = pattern.search(text)
        if match is None:
            continue
        if earliest is None or match.start() < earliest[0]:
            earliest = (match.start(), match.end())

    if earliest is None:
        return None

    start, end = earliest
    return start, end, text[start:end]
