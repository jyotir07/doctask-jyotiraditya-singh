"""Domain vocabulary.

Declarations only: the shapes the invariant tests are written against. No
behaviour lives here, and none should -- every method that decides something
belongs to the module that owns the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DocType(Enum):
    """The document types this system declares support for.

    Anything outside this set is skipped rather than guessed at: extracting
    contract fields from a payslip would produce confident nonsense.
    """

    MSA = "MSA"
    SOW = "SOW"
    AMENDMENT = "AMENDMENT"
    INVOICE = "INVOICE"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    CHANGE_ORDER = "CHANGE_ORDER"
    CORRESPONDENCE = "CORRESPONDENCE"
    UNKNOWN = "UNKNOWN"


class ClassificationOutcome(Enum):
    ACCEPTED = "accepted"
    ESCALATE = "escalate"
    SKIP = "skip"


class CitationStatus(Enum):
    VERIFIED = "verified"
    SPAN_MISMATCH = "span_mismatch"
    OUT_OF_BOUNDS = "out_of_bounds"
    UNKNOWN_DOCUMENT = "unknown_document"


class EntryState(Enum):
    ASSERTED = "asserted"
    DISPUTED = "disputed"
    UNSUPPORTED = "unsupported"


class ConflictKind(Enum):
    VALUE_MISMATCH = "value_mismatch"
    DATE_MISMATCH = "date_mismatch"
    SUPERSEDED = "superseded"
    TERM_BREACH = "term_breach"


class FindingKind(Enum):
    RULE_VIOLATION = "rule_violation"
    INJECTION_ATTEMPT = "injection_attempt"
    MISSING_EVIDENCE = "missing_evidence"


class ReviewItemKind(Enum):
    ENTRY = "entry"
    CONFLICT = "conflict"
    FINDING = "finding"


class Decision(Enum):
    APPROVE = "approve"
    REJECT = "reject"


class RunStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMMITTED = "committed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class Document:
    doc_id: str
    filename: str
    media_type: str
    text: str
    sha256: str


@dataclass(frozen=True)
class Citation:
    doc_id: str
    char_start: int
    char_end: int
    quoted_text: str


@dataclass(frozen=True)
class Fact:
    fact_id: str
    doc_id: str
    field: str
    value: str
    citation: Citation


@dataclass(frozen=True)
class UnsupportedFact:
    doc_id: str
    field: str
    value: str
    citation: Citation
    status: CitationStatus


@dataclass(frozen=True)
class RegisterEntry:
    entry_id: str
    vendor: str
    field: str
    value: str | None
    state: EntryState
    citations: tuple[Citation, ...]
    content_hash: str
    candidate_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conflict:
    conflict_id: str
    entry_id: str
    field: str
    kind: ConflictKind
    citations: tuple[Citation, ...]
    values: tuple[str, ...]
    # What the system would pick if it were allowed to pick. It is not: this
    # rides along to help a reviewer decide, and nothing reads it to act.
    suggested_resolution: str | None = None


@dataclass(frozen=True)
class Finding:
    finding_id: str
    rule_id: str
    kind: FindingKind
    severity: str
    message: str
    citation: Citation


@dataclass
class ReviewItem:
    item_id: str
    kind: ReviewItemKind
    summary: str
    payload: dict
    decision: Decision | None = None
    reason: str | None = None
    resolution: str | None = None
    applied: bool = False


@dataclass(frozen=True)
class ReviewDecision:
    item_id: str
    decision: Decision
    reason: str | None = None
    resolution: str | None = None


@dataclass
class ReviewBundle:
    bundle_id: str
    run_id: str
    items: list[ReviewItem] = field(default_factory=list)


@dataclass(frozen=True)
class StageCost:
    calls: int
    input_tokens: int
    output_tokens: int
    duration_ms: int


@dataclass(frozen=True)
class RunCost:
    calls: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    by_stage: dict[str, StageCost]
