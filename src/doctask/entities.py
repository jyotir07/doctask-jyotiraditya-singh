"""Deciding which vendor a document is about.

Names arrive spelled differently in every document. Getting this wrong splits
one obligation across two entries, or -- worse -- merges two companies into
one and invents agreement between documents that describe different parties.

Resolution is deterministic on purpose. A legal-suffix difference is a string
problem, not a semantic one. Routing it through an embedding model would make
entity identity vary between runs, which would make entry ids vary, which
would make non-modification unprovable. Vector search earns its place in
retrieval, where the question really is semantic.

Three outcomes, and the third is the one that matters: when a name is
genuinely ambiguous the system escalates rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

# Legal-form suffixes carry no identifying information. Stripped as whole
# trailing words only, so "Supply" survives while "Ltd" does not.
LEGAL_SUFFIXES = frozenset(
    {
        "ltd", "limited", "inc", "incorporated", "llc", "llp", "plc",
        "corp", "corporation", "co", "company", "gmbh", "ag", "sa", "nv",
        "bv", "pvt", "private", "pte", "pty",
    }
)

# Below this a name is not a plausible match at all, and the vendor is new.
MATCH_THRESHOLD = 0.75

# How far ahead the best match must be to count as a decision. This is the
# ratio test, and it is the honest way to express ambiguity: two candidates
# neck and neck is not a coin to flip, however high they both score. A typo
# leaves its vendor far ahead of the field; a genuinely under-specified name
# like "Acme Industrial Ltd" leaves Supply and Services within a whisker of
# each other, and that goes to a person.
DECISIVE_MARGIN = 0.10


class MatchOutcome(Enum):
    RESOLVED = "resolved"
    ESCALATE = "escalate"
    NEW = "new"


@dataclass(frozen=True)
class VendorMatch:
    canonical: str
    outcome: MatchOutcome
    candidates: tuple[str, ...] = ()
    reason: str | None = None


def normalize_vendor(raw: str) -> str:
    """Reduce a vendor name to its identifying words."""
    lowered = raw.lower()
    cleaned = re.sub(r"[^\w\s]", " ", lowered)
    words = cleaned.split()

    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()

    return " ".join(words)


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


class VendorRegistry:
    """The canonical vendor names seen so far, and how to match into them."""

    def __init__(self, known: list[str] | None = None) -> None:
        self._canonical: dict[str, str] = {}
        for name in known or []:
            self.add(name)

    def add(self, name: str) -> None:
        self._canonical[normalize_vendor(name)] = name

    @property
    def names(self) -> list[str]:
        return list(self._canonical.values())

    def resolve(self, raw_name: str) -> VendorMatch:
        """Match `raw_name` against what is already known."""
        if not raw_name or not raw_name.strip():
            raise ValueError("vendor name is empty")

        key = normalize_vendor(raw_name)

        # An exact hit is never ambiguous, however many near neighbours exist.
        if key in self._canonical:
            return VendorMatch(canonical=self._canonical[key], outcome=MatchOutcome.RESOLVED)

        scored = sorted(
            (
                (_similarity(key, known_key), canonical)
                for known_key, canonical in self._canonical.items()
            ),
            reverse=True,
        )
        plausible = [(score, name) for score, name in scored if score >= MATCH_THRESHOLD]

        if not plausible:
            return VendorMatch(canonical=raw_name, outcome=MatchOutcome.NEW)

        best_score, best_name = plausible[0]
        contenders = [
            (score, name)
            for score, name in plausible
            if best_score - score < DECISIVE_MARGIN
        ]

        if len(contenders) > 1:
            return VendorMatch(
                canonical=raw_name,
                outcome=MatchOutcome.ESCALATE,
                candidates=tuple(name for _, name in contenders),
                reason=(
                    f"{raw_name!r} matches more than one known vendor with no clear "
                    "winner: " + ", ".join(repr(name) for _, name in contenders)
                ),
            )

        return VendorMatch(canonical=best_name, outcome=MatchOutcome.RESOLVED)
