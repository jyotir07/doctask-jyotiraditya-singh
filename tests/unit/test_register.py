"""The Obligation Register: a set of independently addressable entries.

This is the design decision the whole "stays alive" movement rests on. The
deliverable is not a blob of prose that gets regenerated; it is a set of
content-hashed entries rendered by a pure function. That is what makes
"nothing else changed" a thing you can prove by equality rather than assert
in a README.

Two properties carry the weight. An entry's hash must depend on everything
that is part of its meaning and on nothing else -- a timestamp or run id
leaking in would make every re-run look like a change. And an entry must be
renderable on its own, so an untouched entry's bytes can be compared directly
across two versions of the deliverable.
"""

from __future__ import annotations

import pytest

from doctask.domain import Citation, EntryState
from doctask.register import Register, build_entry, content_hash, diff


def cite(doc_id="msa-001", start=120, end=126, quote="Net 30"):
    return Citation(doc_id=doc_id, char_start=start, char_end=end, quoted_text=quote)


def entry(vendor="Acme", field="payment_terms", value="Net 30",
          state=EntryState.ASSERTED, citations=None, candidates=()):
    return build_entry(
        vendor=vendor,
        field=field,
        value=value,
        state=state,
        citations=tuple(citations if citations is not None else [cite()]),
        candidate_values=tuple(candidates),
    )


# --- identity ---------------------------------------------------------------


def test_the_same_obligation_always_gets_the_same_entry_id():
    """Stable across runs, or an incremental update would append a duplicate
    entry instead of recognising the one it already has."""
    assert entry().entry_id == entry().entry_id


def test_different_fields_are_different_entries():
    assert entry(field="payment_terms").entry_id != entry(field="hourly_rate").entry_id


def test_different_vendors_are_different_entries():
    assert entry(vendor="Acme").entry_id != entry(vendor="Globex").entry_id


def test_the_entry_id_does_not_depend_on_the_value():
    """An entry is the obligation, not the answer. When an amendment changes
    the payment terms, that must update the existing entry rather than create
    a second one alongside it."""
    assert entry(value="Net 30").entry_id == entry(value="Net 45").entry_id


# --- content hashing --------------------------------------------------------


def test_identical_entries_hash_identically():
    assert content_hash(entry()) == content_hash(entry())


def test_the_hash_changes_when_the_value_changes():
    assert content_hash(entry(value="Net 30")) != content_hash(entry(value="Net 45"))


def test_the_hash_changes_when_the_state_changes():
    asserted = entry(state=EntryState.ASSERTED)
    disputed = entry(state=EntryState.DISPUTED)

    assert content_hash(asserted) != content_hash(disputed)


def test_the_hash_changes_when_the_evidence_changes():
    """Same claim, different source. The deliverable's meaning includes where
    each claim came from, so this must register as a change."""
    from_msa = entry(citations=[cite(doc_id="msa-001")])
    from_amendment = entry(citations=[cite(doc_id="amd-001")])

    assert content_hash(from_msa) != content_hash(from_amendment)


def test_the_hash_changes_when_a_citation_span_moves():
    assert content_hash(entry(citations=[cite(start=120, end=126)])) != content_hash(
        entry(citations=[cite(start=300, end=306)])
    )


def test_the_hash_changes_when_candidate_values_change():
    assert content_hash(entry(candidates=("Net 30", "Net 45"))) != content_hash(
        entry(candidates=("Net 30", "Net 60"))
    )


def test_the_hash_survives_rebuilding_the_same_entry_twice():
    """No clock, no run id, no ordering accident may leak into the digest."""
    digests = {content_hash(entry()) for _ in range(5)}

    assert len(digests) == 1


# --- rendering --------------------------------------------------------------


def test_an_entry_renders_its_value():
    register = Register(corpus_id="acme", version=1, entries=[entry()])

    assert "Net 30" in register.render_entry(entry().entry_id)


def test_an_entry_renders_its_sources():
    """Every claim traces to the exact place it came from."""
    register = Register(corpus_id="acme", version=1, entries=[entry()])

    rendered = register.render_entry(entry().entry_id)
    assert "msa-001" in rendered
    assert "120" in rendered and "126" in rendered


def test_a_disputed_entry_renders_both_candidates():
    """Rendering only the newer value would hide the disagreement."""
    disputed = entry(value=None, state=EntryState.DISPUTED, candidates=("Net 30", "Net 45"))
    register = Register(corpus_id="acme", version=1, entries=[disputed])

    rendered = register.render_entry(disputed.entry_id)
    assert "Net 30" in rendered
    assert "Net 45" in rendered


def test_rendering_is_a_pure_function_of_the_entries():
    first = Register(corpus_id="acme", version=1, entries=[entry()])
    second = Register(corpus_id="acme", version=1, entries=[entry()])

    assert first.render() == second.render()


def test_the_version_does_not_leak_into_an_entry_rendering():
    """The load-bearing property for N3. Bumping the deliverable's version
    must not perturb the bytes of an entry nobody touched."""
    v1 = Register(corpus_id="acme", version=1, entries=[entry()])
    v7 = Register(corpus_id="acme", version=7, entries=[entry()])

    assert v1.render_entry(entry().entry_id) == v7.render_entry(entry().entry_id)


def test_adding_an_entry_does_not_change_the_others_bytes():
    """The proof N3 asks for, at register level."""
    original = entry()
    before = Register(corpus_id="acme", version=1, entries=[original])
    after = Register(
        corpus_id="acme",
        version=2,
        entries=[original, entry(field="hourly_rate", value="USD 145")],
    )

    assert after.render_entry(original.entry_id) == before.render_entry(original.entry_id)


def test_entry_order_does_not_change_an_entry_rendering():
    """Entries must render the same regardless of position, or a reordering
    would masquerade as a content change."""
    a, b = entry(), entry(field="hourly_rate", value="USD 145")
    forwards = Register(corpus_id="acme", version=1, entries=[a, b])
    backwards = Register(corpus_id="acme", version=1, entries=[b, a])

    assert forwards.render_entry(a.entry_id) == backwards.render_entry(a.entry_id)


def test_the_whole_deliverable_contains_every_entry():
    register = Register(
        corpus_id="acme",
        version=1,
        entries=[entry(), entry(field="hourly_rate", value="USD 145")],
    )

    rendered = register.render()
    assert "Net 30" in rendered
    assert "USD 145" in rendered


def test_asking_for_an_unknown_entry_is_an_error():
    register = Register(corpus_id="acme", version=1, entries=[entry()])

    with pytest.raises(KeyError):
        register.render_entry("no-such-entry")


# --- diffing ----------------------------------------------------------------


def test_diff_reports_nothing_changed_between_identical_registers():
    before = Register(corpus_id="acme", version=1, entries=[entry()])
    after = Register(corpus_id="acme", version=2, entries=[entry()])

    result = diff(before, after)

    assert result.changed == frozenset()
    assert result.added == frozenset()
    assert result.removed == frozenset()
    assert result.unchanged == frozenset({entry().entry_id})


def test_diff_spots_an_added_entry():
    added = entry(field="hourly_rate", value="USD 145")
    before = Register(corpus_id="acme", version=1, entries=[entry()])
    after = Register(corpus_id="acme", version=2, entries=[entry(), added])

    result = diff(before, after)

    assert result.added == frozenset({added.entry_id})
    assert result.unchanged == frozenset({entry().entry_id})


def test_diff_spots_a_changed_value():
    before = Register(corpus_id="acme", version=1, entries=[entry(value="Net 30")])
    after = Register(corpus_id="acme", version=2, entries=[entry(value="Net 45")])

    result = diff(before, after)

    assert result.changed == frozenset({entry().entry_id})
    assert result.unchanged == frozenset()


def test_diff_spots_a_removed_entry():
    removed = entry(field="hourly_rate", value="USD 145")
    before = Register(corpus_id="acme", version=1, entries=[entry(), removed])
    after = Register(corpus_id="acme", version=2, entries=[entry()])

    result = diff(before, after)

    assert result.removed == frozenset({removed.entry_id})


# --- persisted format stability --------------------------------------------


def test_entry_identity_is_stable_across_code_versions():
    """A deliberate golden-value test, and the one place a change detector
    earns its keep.

    Entry ids and content hashes are written to the database and compared
    against later runs. If the derivation changes, every stored entry silently
    stops matching itself: an upgrade would read as "everything was removed and
    re-added", and the non-modification proof would be worthless.

    Changing these values is allowed. Doing it by accident is not -- so when
    this test fails, the question to answer is how existing deliverables get
    migrated, not what number to paste in.
    """
    pinned = build_entry(
        vendor="Acme Industrial Supply Ltd",
        field="payment_terms",
        value="Net 30",
        state=EntryState.ASSERTED,
        citations=(Citation(doc_id="msa-001", char_start=120, char_end=126, quoted_text="Net 30"),),
    )

    assert pinned.entry_id == "e2432e79ecb180c6"
    assert pinned.content_hash == (
        "56aa0aa5295aa784e3cdf323f152188ab27728f5a6ad8499e303d28d4f4cc430"
    )
