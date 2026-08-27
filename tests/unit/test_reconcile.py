"""Grouping facts into obligations, and noticing where the sources disagree.

The rule this module exists to enforce is N6: a contradiction between two
sources is surfaced, never silently resolved. That holds even when the system
can guess the answer. An amendment almost certainly does supersede the MSA it
amends -- but "almost certainly" is a suggestion for a person to accept, not a
licence for the register to pick a winner and move on.

So the system is allowed to *suggest*. It is never allowed to *apply*.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

from doctask.domain import Citation, ConflictKind, DocType, EntryState, Fact
from doctask.reconcile import DocContext, reconcile


def ctx(doc_id, doc_type=DocType.MSA, vendor="Acme"):
    return DocContext(doc_id=doc_id, vendor=vendor, doc_type=doc_type)


def fact(doc_id, field, value, start=100):
    return Fact(
        fact_id=f"{doc_id}-{field}-{value}",
        doc_id=doc_id,
        field=field,
        value=value,
        citation=Citation(
            doc_id=doc_id, char_start=start, char_end=start + len(value), quoted_text=value
        ),
    )


# --- agreement --------------------------------------------------------------


def test_a_single_fact_becomes_an_asserted_entry():
    result = reconcile([fact("msa-001", "payment_terms", "Net 30")], {"msa-001": ctx("msa-001")})

    assert len(result.entries) == 1
    assert result.entries[0].state is EntryState.ASSERTED
    assert result.entries[0].value == "Net 30"
    assert result.conflicts == ()


def test_two_sources_that_agree_produce_one_entry_with_both_citations():
    """Corroboration, not contradiction. Both sources are worth keeping."""
    facts = [
        fact("msa-001", "payment_terms", "Net 30"),
        fact("inv-001", "payment_terms", "Net 30"),
    ]
    contexts = {"msa-001": ctx("msa-001"), "inv-001": ctx("inv-001", DocType.INVOICE)}

    result = reconcile(facts, contexts)

    assert len(result.entries) == 1
    assert result.conflicts == ()
    assert {c.doc_id for c in result.entries[0].citations} == {"msa-001", "inv-001"}


def test_different_fields_are_separate_entries():
    facts = [
        fact("msa-001", "payment_terms", "Net 30"),
        fact("msa-001", "hourly_rate", "USD 145"),
    ]

    result = reconcile(facts, {"msa-001": ctx("msa-001")})

    assert {e.field for e in result.entries} == {"payment_terms", "hourly_rate"}
    assert result.conflicts == ()


def test_different_vendors_do_not_conflict_with_each_other():
    """Two vendors having different payment terms is not a disagreement."""
    facts = [
        fact("msa-acme", "payment_terms", "Net 30"),
        fact("msa-globex", "payment_terms", "Net 60"),
    ]
    contexts = {
        "msa-acme": ctx("msa-acme", vendor="Acme"),
        "msa-globex": ctx("msa-globex", vendor="Globex"),
    }

    result = reconcile(facts, contexts)

    assert len(result.entries) == 2
    assert result.conflicts == ()


# --- disagreement -----------------------------------------------------------


def test_two_sources_that_disagree_produce_a_conflict():
    facts = [
        fact("msa-001", "payment_terms", "Net 30"),
        fact("amd-001", "payment_terms", "Net 45"),
    ]
    contexts = {"msa-001": ctx("msa-001"), "amd-001": ctx("amd-001", DocType.AMENDMENT)}

    result = reconcile(facts, contexts)

    assert len(result.conflicts) == 1
    assert result.conflicts[0].kind is ConflictKind.VALUE_MISMATCH
    assert result.conflicts[0].field == "payment_terms"


def test_the_disputed_entry_holds_no_single_value():
    """The register must not answer a question the sources do not settle."""
    facts = [
        fact("msa-001", "payment_terms", "Net 30"),
        fact("amd-001", "payment_terms", "Net 45"),
    ]
    contexts = {"msa-001": ctx("msa-001"), "amd-001": ctx("amd-001", DocType.AMENDMENT)}

    result = reconcile(facts, contexts)

    entry = result.entries[0]
    assert entry.state is EntryState.DISPUTED
    assert entry.value is None
    assert sorted(entry.candidate_values) == ["Net 30", "Net 45"]


def test_the_conflict_cites_every_side():
    facts = [
        fact("msa-001", "payment_terms", "Net 30", start=100),
        fact("amd-001", "payment_terms", "Net 45", start=200),
    ]
    contexts = {"msa-001": ctx("msa-001"), "amd-001": ctx("amd-001", DocType.AMENDMENT)}

    result = reconcile(facts, contexts)

    cited = {(c.doc_id, c.char_start) for c in result.conflicts[0].citations}
    assert cited == {("msa-001", 100), ("amd-001", 200)}


def test_a_three_way_disagreement_is_one_conflict_with_three_candidates():
    facts = [
        fact("msa-001", "payment_terms", "Net 30"),
        fact("amd-001", "payment_terms", "Net 45"),
        fact("inv-001", "payment_terms", "Net 60"),
    ]
    contexts = {
        "msa-001": ctx("msa-001"),
        "amd-001": ctx("amd-001", DocType.AMENDMENT),
        "inv-001": ctx("inv-001", DocType.INVOICE),
    }

    result = reconcile(facts, contexts)

    assert len(result.conflicts) == 1
    assert sorted(result.entries[0].candidate_values) == ["Net 30", "Net 45", "Net 60"]


def test_the_conflict_id_does_not_depend_on_fact_order():
    facts = [
        fact("msa-001", "payment_terms", "Net 30"),
        fact("amd-001", "payment_terms", "Net 45"),
    ]
    contexts = {"msa-001": ctx("msa-001"), "amd-001": ctx("amd-001", DocType.AMENDMENT)}

    first = reconcile(facts, contexts)
    second = reconcile(list(reversed(facts)), contexts)

    assert first.conflicts[0].conflict_id == second.conflicts[0].conflict_id


def test_the_conflict_id_is_stable_across_processes():
    """Conflict ids are persisted and compared against later runs, so the
    property that matters is stability across *processes*, not within one.

    A single-process test cannot see this. Values are collected into a set,
    and a set iterates in the same order all through one process however it
    was built -- but that order derives from string hashing, which Python
    randomises per process. Without an explicit sort the id would be stable
    all day in the test suite and drift the moment a real deployment restarted,
    re-surfacing conflicts a reviewer had already decided.
    """
    program = textwrap.dedent(
        """
        from doctask.domain import Citation, DocType, Fact
        from doctask.reconcile import DocContext, reconcile

        def f(doc, value, start):
            return Fact(fact_id=doc, doc_id=doc, field="payment_terms", value=value,
                        citation=Citation(doc_id=doc, char_start=start,
                                          char_end=start + len(value), quoted_text=value))

        contexts = {
            "msa-001": DocContext("msa-001", "Acme", DocType.MSA),
            "amd-001": DocContext("amd-001", "Acme", DocType.AMENDMENT),
        }
        result = reconcile([f("msa-001", "Net 30", 100), f("amd-001", "Net 45", 200)], contexts)
        print(result.conflicts[0].conflict_id)
        """
    )

    ids = set()
    for seed in ("0", "1", "42", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "src"}
        proc = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True, text=True, env=env, check=True,
        )
        ids.add(proc.stdout.strip())

    assert len(ids) == 1, f"conflict id drifted across processes: {ids}"


# --- suggesting without deciding -------------------------------------------


def test_an_amendment_is_suggested_but_not_applied():
    """The heart of N6. The system may believe the amendment wins; it may not
    act on that belief."""
    facts = [
        fact("msa-001", "payment_terms", "Net 30"),
        fact("amd-001", "payment_terms", "Net 45"),
    ]
    contexts = {"msa-001": ctx("msa-001"), "amd-001": ctx("amd-001", DocType.AMENDMENT)}

    result = reconcile(facts, contexts)

    assert result.conflicts[0].suggested_resolution == "Net 45"
    assert result.entries[0].state is EntryState.DISPUTED
    assert result.entries[0].value is None


def test_no_suggestion_when_nothing_supersedes_anything():
    """Two invoices disagreeing has no obvious winner, and the system must not
    invent one."""
    facts = [
        fact("inv-001", "payment_terms", "Net 30"),
        fact("inv-002", "payment_terms", "Net 45"),
    ]
    contexts = {
        "inv-001": ctx("inv-001", DocType.INVOICE),
        "inv-002": ctx("inv-002", DocType.INVOICE),
    }

    result = reconcile(facts, contexts)

    assert result.conflicts[0].suggested_resolution is None


def test_an_empty_pile_reconciles_to_nothing():
    result = reconcile([], {})

    assert result.entries == ()
    assert result.conflicts == ()
