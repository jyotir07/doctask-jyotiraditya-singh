"""The Obligation Register -- the deliverable.

Not a blob of prose. A set of independently addressable, content-hashed
entries, rendered by a pure function of that set. This is what makes a focused
update possible at all, and what lets non-modification be proven by equality
rather than asserted.

Two rules govern everything here:

An entry's identity is the *obligation* it describes, never the answer. When
an amendment changes the payment terms, that updates the existing entry rather
than appending a second one beside it.

An entry's content hash depends on everything that is part of its meaning and
on nothing else. A clock, a run id, or an accident of ordering leaking into
the digest would make every re-run look like a change, and the whole
"nothing else moved" claim would collapse.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from doctask.domain import Citation, EntryState, RegisterEntry


def entry_id_for(vendor: str, field: str) -> str:
    """Stable identity for an obligation.

    Deliberately excludes the value: an entry is the question, not the answer.
    """
    material = f"{vendor}\x1f{field}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _citation_key(citation: Citation) -> tuple:
    return (citation.doc_id, citation.char_start, citation.char_end, citation.quoted_text)


def content_hash(entry: RegisterEntry) -> str:
    """Digest over an entry's meaning.

    Serialised canonically -- sorted keys, sorted citations -- so that two
    entries which mean the same thing hash the same regardless of the order
    the evidence happened to arrive in.
    """
    material = {
        "vendor": entry.vendor,
        "field": entry.field,
        "value": entry.value,
        "state": entry.state.value,
        "citations": [list(_citation_key(c)) for c in sorted(entry.citations, key=_citation_key)],
        "candidate_values": sorted(entry.candidate_values),
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_entry(
    *,
    vendor: str,
    field: str,
    value: str | None,
    state: EntryState,
    citations: tuple[Citation, ...],
    candidate_values: tuple[str, ...] = (),
) -> RegisterEntry:
    """Assemble an entry with its identity and digest filled in.

    Citations and candidates are sorted here, once, so that everything
    downstream -- hashing, rendering, diffing -- is deterministic without
    having to remember to sort again.
    """
    ordered_citations = tuple(sorted(citations, key=_citation_key))
    ordered_candidates = tuple(sorted(candidate_values))

    entry = RegisterEntry(
        entry_id=entry_id_for(vendor, field),
        vendor=vendor,
        field=field,
        value=value,
        state=state,
        citations=ordered_citations,
        content_hash="",
        candidate_values=ordered_candidates,
    )
    return RegisterEntry(
        entry_id=entry.entry_id,
        vendor=entry.vendor,
        field=entry.field,
        value=entry.value,
        state=entry.state,
        citations=entry.citations,
        content_hash=content_hash(entry),
        candidate_values=entry.candidate_values,
    )


def render_entry_text(entry: RegisterEntry) -> str:
    """One entry's bytes.

    Depends on the entry alone -- not on the register that holds it, its
    version, or its position -- so an untouched entry can be compared byte for
    byte across two versions of the deliverable.
    """
    lines = [f"### {entry.field}", ""]

    if entry.state is EntryState.DISPUTED:
        lines.append("DISPUTED - the sources do not agree:")
        lines.append("")
        for candidate in entry.candidate_values:
            lines.append(f"- {candidate}")
    else:
        lines.append(str(entry.value))

    lines.extend(["", "Sources:"])
    for citation in entry.citations:
        lines.append(
            f'- {citation.doc_id} [{citation.char_start}:{citation.char_end}] '
            f'"{citation.quoted_text}"'
        )

    return "\n".join(lines)


@dataclass(frozen=True)
class RegisterDiff:
    added: frozenset[str]
    removed: frozenset[str]
    changed: frozenset[str]
    unchanged: frozenset[str]


class Register:
    """A versioned set of addressable entries."""

    def __init__(self, corpus_id: str, version: int, entries: list[RegisterEntry]) -> None:
        self._corpus_id = corpus_id
        self._version = version
        self._entries = {e.entry_id: e for e in entries}

    @property
    def corpus_id(self) -> str:
        return self._corpus_id

    @property
    def version(self) -> int:
        return self._version

    @property
    def entries(self) -> list[RegisterEntry]:
        """Sorted by vendor then field, so the deliverable reads the same way
        every time regardless of the order entries were composed in."""
        return sorted(self._entries.values(), key=lambda e: (e.vendor, e.field))

    def entry(self, entry_id: str) -> RegisterEntry:
        return self._entries[entry_id]

    def render_entry(self, entry_id: str) -> str:
        return render_entry_text(self._entries[entry_id])

    def render(self) -> str:
        """The whole deliverable, as a human reads it."""
        blocks: list[str] = []
        current_vendor = None
        for entry in self.entries:
            if entry.vendor != current_vendor:
                blocks.append(f"## {entry.vendor}")
                current_vendor = entry.vendor
            blocks.append(self.render_entry(entry.entry_id))
        return "\n\n".join(blocks) + "\n"


def diff(before: Register, after: Register) -> RegisterDiff:
    """What moved between two versions of the deliverable."""
    before_hashes = {e.entry_id: e.content_hash for e in before.entries}
    after_hashes = {e.entry_id: e.content_hash for e in after.entries}

    before_ids, after_ids = set(before_hashes), set(after_hashes)
    common = before_ids & after_ids

    return RegisterDiff(
        added=frozenset(after_ids - before_ids),
        removed=frozenset(before_ids - after_ids),
        changed=frozenset(i for i in common if before_hashes[i] != after_hashes[i]),
        unchanged=frozenset(i for i in common if before_hashes[i] == after_hashes[i]),
    )
