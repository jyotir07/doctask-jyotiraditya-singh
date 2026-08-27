"""Grouping facts into obligations, and noticing where sources disagree.

This module enforces N6: a contradiction between two sources is surfaced,
never silently resolved. That holds even when the answer looks obvious. An
amendment almost certainly does supersede the MSA it amends -- but "almost
certainly" is a suggestion for a person to accept, not a licence for the
register to pick a winner and move on.

The system may suggest. It may not apply. Everything below keeps that line:
`suggested_resolution` is computed and handed to the reviewer, and no code
path here reads it back to decide anything.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from doctask.domain import (
    Citation,
    Conflict,
    ConflictKind,
    DocType,
    EntryState,
    Fact,
    RegisterEntry,
)
from doctask.register import build_entry, entry_id_for

# Which document types speak with more authority about a term than which
# others. Data rather than branching, so adding a document type is a change
# here and nowhere else.
SUPERSEDES: dict[DocType, frozenset[DocType]] = {
    DocType.AMENDMENT: frozenset({DocType.MSA, DocType.SOW}),
    DocType.CHANGE_ORDER: frozenset({DocType.SOW, DocType.PURCHASE_ORDER}),
}


@dataclass(frozen=True)
class DocContext:
    """What reconciliation needs to know about a document beyond its facts."""

    doc_id: str
    vendor: str
    doc_type: DocType


@dataclass(frozen=True)
class Reconciliation:
    entries: tuple[RegisterEntry, ...]
    conflicts: tuple[Conflict, ...]


def _conflict_id_for(entry_id: str, values: Iterable[str]) -> str:
    """Stable across runs and independent of the order facts arrived in.

    A conflict that changed identity between runs would be re-surfaced to a
    reviewer who had already decided it.
    """
    material = entry_id + "\x1f" + "\x1f".join(sorted(values))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _suggest(facts: Sequence[Fact], contexts: Mapping[str, DocContext]) -> str | None:
    """The value a superseding document gives, if exactly one does.

    Returns None whenever the answer is not clear-cut -- two invoices
    disagreeing has no obvious winner, and inventing one would be exactly the
    silent resolution N6 forbids.
    """
    winners = []
    for candidate in facts:
        candidate_type = contexts[candidate.doc_id].doc_type
        outranked = SUPERSEDES.get(candidate_type, frozenset())
        others = [f for f in facts if f.value != candidate.value]
        if others and all(contexts[o.doc_id].doc_type in outranked for o in others):
            winners.append(candidate.value)

    unique = set(winners)
    return unique.pop() if len(unique) == 1 else None


def reconcile(
    facts: Sequence[Fact], contexts: Mapping[str, DocContext]
) -> Reconciliation:
    """Fold verified facts into register entries, surfacing disagreements."""
    grouped: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for fact in facts:
        vendor = contexts[fact.doc_id].vendor
        grouped[(vendor, fact.field)].append(fact)

    entries: list[RegisterEntry] = []
    conflicts: list[Conflict] = []

    for (vendor, field), group in sorted(grouped.items()):
        citations: tuple[Citation, ...] = tuple(f.citation for f in group)
        values = {f.value for f in group}

        if len(values) == 1:
            entries.append(
                build_entry(
                    vendor=vendor,
                    field=field,
                    value=group[0].value,
                    state=EntryState.ASSERTED,
                    citations=citations,
                )
            )
            continue

        entry = build_entry(
            vendor=vendor,
            field=field,
            value=None,
            state=EntryState.DISPUTED,
            citations=citations,
            candidate_values=tuple(values),
        )
        entries.append(entry)
        conflicts.append(
            Conflict(
                conflict_id=_conflict_id_for(entry.entry_id, values),
                entry_id=entry.entry_id,
                field=field,
                kind=ConflictKind.VALUE_MISMATCH,
                citations=tuple(sorted(citations, key=lambda c: (c.doc_id, c.char_start))),
                values=tuple(sorted(values)),
                suggested_resolution=_suggest(group, contexts),
            )
        )

    return Reconciliation(entries=tuple(entries), conflicts=tuple(conflicts))


def contexts_from(
    documents: Iterable, vendor_of: Mapping[str, str], type_of: Mapping[str, DocType]
) -> dict[str, DocContext]:
    """Assemble the per-document context reconciliation needs."""
    return {
        doc.doc_id: DocContext(
            doc_id=doc.doc_id,
            vendor=vendor_of[doc.doc_id],
            doc_type=type_of[doc.doc_id],
        )
        for doc in documents
    }
