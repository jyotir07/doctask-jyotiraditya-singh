"""Checking the sources and the deliverable against the rules a user cares about.

Rules are data. A new clause, a changed threshold, a new client's playbook --
all of it is a YAML edit. Nothing in this module knows what "Net 30" means;
it knows how to apply a `numeric_range` check to a field, and the pack says
which field and which bounds.

Evaluation is staged by cost. Deterministic checks run first and spend
nothing; only rules that genuinely need judgement reach a model, and only when
they are in scope. A rule arithmetic can settle is never paid for.

The report distinguishes four outcomes -- satisfied, violated, undecided, and
not applicable -- because collapsing them is how "we checked everything and
found nothing" comes to mean "we checked almost nothing".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Sequence

import yaml

from doctask.domain import Citation, EntryState, Finding, FindingKind, RegisterEntry
from doctask.errors import RulePackInvalid

SEVERITIES = frozenset({"low", "medium", "high", "critical"})
CHECK_KINDS = frozenset({"required", "numeric_range", "not_disputed", "forbidden_value", "model"})


class Verdict(Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNDECIDED = "undecided"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    severity: str
    description: str
    applies_to: dict
    check: dict

    @property
    def needs_model(self) -> bool:
        return self.check["kind"] == "model"

    @property
    def target_field(self) -> str | None:
        return self.applies_to.get("field")

    @property
    def target_doc_type(self) -> str | None:
        return self.applies_to.get("doc_type")


@dataclass(frozen=True)
class RulePack:
    name: str
    version: int
    description: str
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class AuditReport:
    findings: tuple[Finding, ...]
    rules_checked: int
    rules_satisfied: int
    rules_violated: int
    rules_undecided: int
    rules_not_applicable: int
    model_calls: int

    @property
    def is_clean(self) -> bool:
        """True only when something was actually checked and nothing failed."""
        return self.rules_checked > 0 and not self.findings


def load_rule_pack(raw: dict) -> RulePack:
    """Validate a rule pack, refusing anything that would half-load.

    A pack that silently drops an unparseable rule would stop checking
    something nobody realised had stopped being checked.
    """
    if not isinstance(raw, dict):
        raise RulePackInvalid("rule pack is not a mapping")
    for required in ("name", "version", "rules"):
        if required not in raw:
            raise RulePackInvalid(f"rule pack has no {required!r}")
    if not isinstance(raw["rules"], list):
        raise RulePackInvalid("'rules' is not a list")

    rules = []
    seen: set[str] = set()
    for entry in raw["rules"]:
        if not isinstance(entry, dict):
            raise RulePackInvalid("a rule is not a mapping")
        for required in ("id", "severity", "description", "applies_to", "check"):
            if required not in entry:
                raise RulePackInvalid(f"rule {entry.get('id', '<unnamed>')!r} has no {required!r}")

        rule_id = entry["id"]
        if rule_id in seen:
            raise RulePackInvalid(f"duplicate rule id {rule_id!r}")
        seen.add(rule_id)

        if entry["severity"] not in SEVERITIES:
            raise RulePackInvalid(
                f"rule {rule_id!r} has severity {entry['severity']!r}; "
                f"expected one of {sorted(SEVERITIES)}"
            )
        kind = entry["check"].get("kind")
        if kind not in CHECK_KINDS:
            raise RulePackInvalid(
                f"rule {rule_id!r} has check kind {kind!r}; "
                f"expected one of {sorted(CHECK_KINDS)}"
            )

        rules.append(Rule(
            rule_id=rule_id, severity=entry["severity"], description=entry["description"],
            applies_to=entry["applies_to"], check=entry["check"],
        ))

    return RulePack(
        name=raw["name"], version=raw["version"],
        description=raw.get("description", ""), rules=tuple(rules),
    )


def load_rule_pack_file(path: str | Path) -> RulePack:
    return load_rule_pack(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def _number(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None


def _check_entry(rule: Rule, entry: RegisterEntry) -> tuple[Verdict, str | None]:
    """Apply one deterministic check to one entry."""
    kind = rule.check["kind"]

    if kind == "not_disputed":
        if entry.state is EntryState.DISPUTED:
            return Verdict.VIOLATED, (
                f"{entry.field} is disputed between sources "
                f"({', '.join(entry.candidate_values)}) and has not been resolved"
            )
        return Verdict.SATISFIED, None

    if kind == "forbidden_value":
        if entry.value and re.search(rule.check["pattern"], entry.value):
            return Verdict.VIOLATED, f"{entry.field} is {entry.value!r}, which is not permitted"
        return Verdict.SATISFIED, None

    if kind == "numeric_range":
        if entry.value is None:
            # A disputed entry has no single value to bound. Undecided rather
            # than satisfied: the not_disputed rule is what speaks to this.
            return Verdict.UNDECIDED, f"{entry.field} has no settled value to check"
        found = _number(entry.value, rule.check["pattern"])
        if found is None:
            return Verdict.UNDECIDED, f"could not read a number from {entry.value!r}"
        minimum, maximum = rule.check.get("minimum"), rule.check.get("maximum")
        if minimum is not None and found < minimum:
            return Verdict.VIOLATED, (
                f"{entry.field} is {entry.value!r}; policy requires at least {minimum:g}"
            )
        if maximum is not None and found > maximum:
            return Verdict.VIOLATED, (
                f"{entry.field} is {entry.value!r}; policy allows at most {maximum:g}"
            )
        return Verdict.SATISFIED, None

    return Verdict.SATISFIED, None


def _finding(rule: Rule, message: str, citation: Citation, index: int) -> Finding:
    return Finding(
        finding_id=f"{rule.rule_id}:{index}",
        rule_id=rule.rule_id,
        kind=FindingKind.RULE_VIOLATION,
        severity=rule.severity,
        message=message,
        citation=citation,
    )


def evaluate(
    pack: RulePack,
    entries: Sequence[RegisterEntry],
    *,
    judge: Callable[[Rule, Sequence], tuple[Verdict, Citation | None]] | None = None,
    doc_types: Iterable[str] | None = None,
) -> AuditReport:
    """Run a pack against a register, cheapest checks first.

    `judge` is how a model-backed rule gets answered. It is injected rather
    than reached for, so the deterministic stage is provably free: a test can
    pass a judge that records every call and assert it was never used.
    """
    findings: list[Finding] = []
    satisfied = violated = undecided = not_applicable = 0
    model_calls = 0
    doc_types = set(doc_types) if doc_types is not None else None

    for rule in pack.rules:
        # --- stage 1: deterministic, over structured fields ---------------
        if not rule.needs_model:
            if rule.target_field is None:
                not_applicable += 1
                continue

            matching = [e for e in entries if e.field == rule.target_field]

            if rule.check["kind"] == "required":
                if matching:
                    satisfied += 1
                else:
                    violated += 1
                    findings.append(_finding(
                        rule,
                        f"no {rule.target_field} is recorded for any vendor",
                        Citation(doc_id="", char_start=0, char_end=0, quoted_text=""),
                        len(findings),
                    ))
                continue

            if not matching:
                not_applicable += 1
                continue

            rule_violated = False
            rule_undecided = False
            for entry in matching:
                verdict, message = _check_entry(rule, entry)
                if verdict is Verdict.VIOLATED:
                    rule_violated = True
                    findings.append(_finding(
                        rule, message, entry.citations[0], len(findings)
                    ))
                elif verdict is Verdict.UNDECIDED:
                    rule_undecided = True

            if rule_violated:
                violated += 1
            elif rule_undecided:
                undecided += 1
            else:
                satisfied += 1
            continue

        # --- stage 2: judgement, only where it is genuinely needed --------
        in_scope = (
            doc_types is None
            or rule.target_doc_type is None
            or rule.target_doc_type in doc_types
        )
        if not in_scope or judge is None:
            not_applicable += 1
            continue

        model_calls += 1
        verdict, citation = judge(rule, entries)
        if verdict is Verdict.VIOLATED:
            violated += 1
            findings.append(_finding(
                rule, rule.description,
                citation or Citation(doc_id="", char_start=0, char_end=0, quoted_text=""),
                len(findings),
            ))
        elif verdict is Verdict.UNDECIDED:
            undecided += 1
        else:
            satisfied += 1

    return AuditReport(
        findings=tuple(findings),
        rules_checked=satisfied + violated + undecided,
        rules_satisfied=satisfied,
        rules_violated=violated,
        rules_undecided=undecided,
        rules_not_applicable=not_applicable,
        model_calls=model_calls,
    )
