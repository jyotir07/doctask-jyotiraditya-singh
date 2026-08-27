"""Deciding which vendor a document is about.

Vendor names arrive spelled differently in every document -- "Acme Industrial
Supply Ltd" in the MSA, "ACME INDUSTRIAL SUPPLY LIMITED" in the invoice
header, "Acme Industrial Supply, Ltd." in an email. Getting this wrong splits
one obligation across two entries, or worse, merges two vendors into one.

Resolution is deterministic on purpose. A legal-suffix difference is a string
problem, not a semantic one, and routing it through a model would make entity
identity vary between runs -- which would in turn make entry ids vary, and
non-modification stop being provable.

The third outcome is the important one: when a name is genuinely ambiguous the
system escalates rather than guessing.
"""

from __future__ import annotations

import pytest

from doctask.entities import MatchOutcome, VendorRegistry, normalize_vendor

KNOWN = ["Acme Industrial Supply Ltd", "Globex Fabrication Inc"]


@pytest.fixture
def registry():
    return VendorRegistry(KNOWN)


# --- normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme Industrial Supply Ltd", "acme industrial supply"),
        ("ACME INDUSTRIAL SUPPLY LIMITED", "acme industrial supply"),
        ("Acme Industrial Supply, Ltd.", "acme industrial supply"),
        ("  Acme   Industrial   Supply Ltd  ", "acme industrial supply"),
        ("Globex Fabrication Inc", "globex fabrication"),
        ("Globex Fabrication, Incorporated", "globex fabrication"),
        ("Northwind Trading Co.", "northwind trading"),
        ("Southgate Logistics LLC", "southgate logistics"),
        # Legal-form words that are part of the actual name. Stripping must
        # work from the end only: a company really can be called "Company
        # Shop" or "Co-operative Supply", and eating those words leaves
        # nothing to match on.
        ("Company Shop Group Ltd", "company shop group"),
        ("Co-operative Supply Ltd", "co operative supply"),
        ("Inc Magazine Holdings Ltd", "inc magazine holdings"),
    ],
)
def test_names_normalise_to_the_same_key(raw, expected):
    assert normalize_vendor(raw) == expected


def test_normalisation_is_idempotent():
    once = normalize_vendor("Acme Industrial Supply Ltd")

    assert normalize_vendor(once) == once


def test_normalisation_does_not_erase_the_distinguishing_word():
    """Supply and Services are different companies. Suffix stripping must not
    be greedy enough to lose that."""
    assert normalize_vendor("Acme Industrial Supply Ltd") != normalize_vendor(
        "Acme Industrial Services Ltd"
    )


# --- resolution -------------------------------------------------------------


def test_an_exact_name_resolves(registry):
    match = registry.resolve("Acme Industrial Supply Ltd")

    assert match.outcome is MatchOutcome.RESOLVED
    assert match.canonical == "Acme Industrial Supply Ltd"


def test_a_differently_spelled_name_resolves_to_the_canonical_one(registry):
    """The property that keeps one obligation in one entry."""
    match = registry.resolve("ACME INDUSTRIAL SUPPLY, LIMITED")

    assert match.outcome is MatchOutcome.RESOLVED
    assert match.canonical == "Acme Industrial Supply Ltd"


def test_a_near_miss_resolves(registry):
    """A typo or a dropped word should still find its vendor."""
    match = registry.resolve("Acme Industrial Suply Ltd")

    assert match.outcome is MatchOutcome.RESOLVED
    assert match.canonical == "Acme Industrial Supply Ltd"


def test_an_unrelated_name_is_a_new_vendor(registry):
    match = registry.resolve("Initech Systems Ltd")

    assert match.outcome is MatchOutcome.NEW
    assert match.canonical == "Initech Systems Ltd"


def test_a_new_vendor_is_not_forced_onto_an_existing_one(registry):
    """Merging two vendors is worse than splitting one: it invents agreement
    between documents that describe different companies."""
    match = registry.resolve("Globex Logistics Inc")

    assert match.canonical != "Acme Industrial Supply Ltd"


# --- ambiguity --------------------------------------------------------------


def test_an_ambiguous_name_escalates():
    """Two plausible matches is not a coin to flip."""
    registry = VendorRegistry(["Acme Industrial Supply Ltd", "Acme Industrial Services Ltd"])

    match = registry.resolve("Acme Industrial Ltd")

    assert match.outcome is MatchOutcome.ESCALATE


def test_the_escalation_names_the_candidates():
    """A person picking this up needs to see what it was torn between."""
    registry = VendorRegistry(["Acme Industrial Supply Ltd", "Acme Industrial Services Ltd"])

    match = registry.resolve("Acme Industrial Ltd")

    assert set(match.candidates) == {
        "Acme Industrial Supply Ltd",
        "Acme Industrial Services Ltd",
    }
    assert "Acme Industrial Ltd" in match.reason


def test_an_exact_match_wins_over_ambiguity():
    """An exact hit is never ambiguous, however close its neighbours are.

    The two registry names here are 0.977 similar, well inside the decisive
    margin. Scoring alone would call this too close to decide; only the
    exact-match shortcut settles it, which is exactly what it is for.
    """
    registry = VendorRegistry(["Acme Industrial Supply Ltd", "Acme Industrial Suppl Ltd"])

    match = registry.resolve("Acme Industrial Supply Ltd")

    assert match.outcome is MatchOutcome.RESOLVED
    assert match.canonical == "Acme Industrial Supply Ltd"


# --- growing the registry ---------------------------------------------------


def test_a_resolved_vendor_stays_resolved_after_learning_a_new_one(registry):
    registry.add("Initech Systems Ltd")

    assert registry.resolve("Acme Industrial Supply Ltd").canonical == "Acme Industrial Supply Ltd"


def test_a_learned_vendor_resolves_afterwards(registry):
    registry.add("Initech Systems Ltd")

    match = registry.resolve("INITECH SYSTEMS LIMITED")

    assert match.outcome is MatchOutcome.RESOLVED
    assert match.canonical == "Initech Systems Ltd"


def test_an_empty_name_is_refused(registry):
    with pytest.raises(ValueError):
        registry.resolve("   ")
