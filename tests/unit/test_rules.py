"""Rule packs are data, and the audit is staged by cost.

Two claims are under test here.

**Configuration over code.** A new rule, a changed threshold, a new document
type -- all of it is an edit to a YAML file. If any of these tests needed a
Python change to pass, the design would have failed.

**Staged evaluation.** Deterministic checks run first and cost nothing. Only
what genuinely needs judgement reaches a model, and a rule already settled by
arithmetic must never be paid for twice.

And the outcome the brief calls the rarest in the industry: a clean corpus
producing an honest report of no findings, with the count of what was checked
so that "no findings" is distinguishable from "nothing ran".
"""

from __future__ import annotations

import pytest

from doctask.domain import Citation, EntryState, FindingKind
from doctask.errors import RulePackInvalid
from doctask.register import build_entry
from doctask.rules import Verdict, evaluate, load_rule_pack, load_rule_pack_file

PACK = "rulepacks/contract-playbook.yaml"


def entry(field="payment_terms", value="Net 30", state=EntryState.ASSERTED,
          candidates=(), vendor="Acme Industrial Supply Ltd"):
    return build_entry(
        vendor=vendor, field=field, value=value, state=state,
        citations=(Citation(doc_id="msa-001", char_start=120, char_end=126,
                            quoted_text=value or "disputed"),),
        candidate_values=tuple(candidates),
    )


# --- the pack is data -------------------------------------------------------


def test_the_reference_pack_loads():
    pack = load_rule_pack_file(PACK)

    assert pack.name == "contract-playbook"
    assert len(pack.rules) == 5


def test_rules_keep_their_severity_and_description():
    pack = load_rule_pack_file(PACK)

    rule = next(r for r in pack.rules if r.rule_id == "payment-terms-documented")
    assert rule.severity == "high"
    assert "payment terms" in rule.description.lower()


def test_a_new_rule_needs_no_code(tmp_path):
    """The configuration-over-code claim, tested directly."""
    pack = load_rule_pack({
        "name": "client-specific",
        "version": 1,
        "rules": [{
            "id": "termination-notice-minimum",
            "severity": "high",
            "description": "Termination notice must be at least 30 days.",
            "applies_to": {"field": "termination_notice"},
            "check": {"kind": "numeric_range", "pattern": r"(\d+)\s+days", "minimum": 30},
        }],
    })

    report = evaluate(pack, [entry(field="termination_notice", value="14 days")])

    assert [f.rule_id for f in report.findings] == ["termination-notice-minimum"]


@pytest.mark.parametrize(
    "broken,because",
    [
        ({"version": 1, "rules": []}, "no name"),
        ({"name": "x", "rules": []}, "no version"),
        ({"name": "x", "version": 1}, "no rules key"),
        ({"name": "x", "version": 1, "rules": [{"severity": "high"}]}, "rule has no id"),
        ({"name": "x", "version": 1,
          "rules": [{"id": "r", "severity": "urgent", "description": "d",
                     "applies_to": {"field": "f"}, "check": {"kind": "required"}}]},
         "unknown severity"),
        ({"name": "x", "version": 1,
          "rules": [{"id": "r", "severity": "high", "description": "d",
                     "applies_to": {"field": "f"}, "check": {"kind": "vibes"}}]},
         "unknown check kind"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_malformed_pack_is_refused(broken, because):
    """Loudly, and at load time. A rule pack that half-loads would silently
    stop checking things nobody noticed were unchecked."""
    with pytest.raises(RulePackInvalid):
        load_rule_pack(broken)


# --- stage 1: deterministic, and free --------------------------------------


def test_a_compliant_register_yields_no_findings():
    """The rarest output in this industry, and it has to be honest."""
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(value="Net 30"), entry(field="hourly_rate", value="USD 145")])

    assert report.findings == ()


def test_a_clean_report_still_says_what_it_checked():
    """"No findings" and "nothing ran" must not look alike."""
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(value="Net 30"), entry(field="hourly_rate", value="USD 145")])

    assert report.rules_checked > 0
    assert report.rules_satisfied == report.rules_checked
    assert report.model_calls == 0


def test_a_value_outside_policy_is_a_finding():
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(value="Net 90")])

    assert [f.rule_id for f in report.findings] == ["payment-terms-within-policy"]


def test_a_value_below_the_floor_is_a_finding():
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(value="Net 7")])

    assert [f.rule_id for f in report.findings] == ["payment-terms-within-policy"]


def test_the_boundary_values_are_inside_policy():
    pack = load_rule_pack_file(PACK)

    assert evaluate(pack, [entry(value="Net 15")]).findings == ()
    assert evaluate(pack, [entry(value="Net 60")]).findings == ()


def test_a_missing_required_field_is_a_finding():
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(field="hourly_rate", value="USD 145")])

    assert "payment-terms-documented" in [f.rule_id for f in report.findings]


def test_a_disputed_value_is_a_finding():
    """An unresolved disagreement is itself a compliance problem."""
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(value=None, state=EntryState.DISPUTED,
                                   candidates=("Net 30", "Net 45"))])

    assert "payment-terms-undisputed" in [f.rule_id for f in report.findings]


def test_a_finding_carries_the_evidence_behind_it():
    """Findings obey N1 too: each points at the exact place it came from."""
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(value="Net 90")])

    finding = report.findings[0]
    assert finding.citation.doc_id == "msa-001"
    assert finding.kind is FindingKind.RULE_VIOLATION
    assert finding.severity == "medium"


def test_a_finding_explains_itself():
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(value="Net 90")])

    assert "90" in report.findings[0].message


def test_rules_that_do_not_apply_are_not_counted_as_satisfied():
    """Counting a rule nobody could have broken as "satisfied" inflates the
    report into something meaningless."""
    pack = load_rule_pack_file(PACK)

    report = evaluate(pack, [entry(value="Net 30")])

    assert report.rules_not_applicable > 0
    assert report.rules_checked + report.rules_not_applicable >= len(pack.rules)


# --- staging ---------------------------------------------------------------


def test_deterministic_checks_never_reach_a_model():
    """Stage 1 costs nothing. If arithmetic can settle it, arithmetic settles
    it -- and the violation is still found."""
    pack = load_rule_pack_file(PACK)
    calls = []

    def judge(rule, passages):
        calls.append(rule.rule_id)
        return Verdict.SATISFIED, None

    # No MSA in scope, so nothing here legitimately needs judgement. Any call
    # the judge receives is a deterministic rule that went to a model.
    report = evaluate(pack, [entry(value="Net 90")], judge=judge, doc_types=set())

    assert calls == []
    assert [f.rule_id for f in report.findings] == ["payment-terms-within-policy"]
    assert report.model_calls == 0


def test_a_model_rule_is_only_asked_when_it_applies():
    pack = load_rule_pack_file(PACK)
    asked = []

    def judge(rule, passages):
        asked.append(rule.rule_id)
        return Verdict.SATISFIED, None

    # No MSA in scope, so the auto-renewal rule has nothing to judge.
    evaluate(pack, [entry(value="Net 30")], judge=judge, doc_types=set())

    assert asked == []


def test_a_model_rule_in_scope_is_asked_once():
    pack = load_rule_pack_file(PACK)
    asked = []

    def judge(rule, passages):
        asked.append(rule.rule_id)
        return Verdict.SATISFIED, None

    evaluate(pack, [entry(value="Net 30")], judge=judge, doc_types={"MSA"})

    assert asked == ["no-automatic-renewal"]


def test_a_model_violation_becomes_a_finding_with_its_citation():
    pack = load_rule_pack_file(PACK)
    citation = Citation(doc_id="msa-001", char_start=400, char_end=430,
                        quoted_text="renews automatically each year")

    def judge(rule, passages):
        return Verdict.VIOLATED, citation

    report = evaluate(pack, [entry(value="Net 30")], judge=judge, doc_types={"MSA"})

    finding = next(f for f in report.findings if f.rule_id == "no-automatic-renewal")
    assert finding.citation == citation
    assert report.model_calls == 1


def test_a_model_rule_that_cannot_be_judged_is_reported_as_undecided():
    """Not silently satisfied. An unanswerable check is a gap in the audit,
    and the report has to say so rather than imply a clean bill."""
    pack = load_rule_pack_file(PACK)

    report = evaluate(
        pack, [entry(value="Net 30")],
        judge=lambda rule, passages: (Verdict.UNDECIDED, None),
        doc_types={"MSA"},
    )

    assert report.rules_undecided == 1
    assert report.rules_satisfied != report.rules_checked
