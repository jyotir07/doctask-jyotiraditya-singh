"""Errors the system raises deliberately.

Each one names a cause a caller can act on. None of them ever carries
credential material in its message (N10).
"""


class DoctaskError(Exception):
    """Base for every error this system raises on purpose."""


class UnknownReviewItem(DoctaskError):
    """A decision referenced an item that was never proposed (N4)."""


class CommitVerificationFailed(DoctaskError):
    """The post-commit read did not match what was meant to be written (N5)."""


class StaleBaseVersion(DoctaskError):
    """A commit was built on a deliverable version that has since moved (N8)."""


class ProviderError(DoctaskError):
    """A model provider call failed."""


class CitationUnverifiable(DoctaskError):
    """A cited span does not hold the quoted text (N1)."""


class MissingScriptEntry(DoctaskError):
    """The fake provider was asked for a completion it has no script for."""


class UnsupportedFormat(DoctaskError):
    """A source file is in a format this system does not declare support for.

    Raised rather than skipped: a silently omitted document makes the
    deliverable quietly incomplete, which is worse than refusing to start.
    """


class ExtractionMalformed(DoctaskError):
    """Model output did not match the extraction schema.

    Distinct from a wrong answer on purpose: the repair loop retries a
    malformed response, while a wrong-but-well-formed one is handled by
    citation verification instead.
    """


class ClassificationMalformed(DoctaskError):
    """Classifier output did not match the expected shape."""


class RulePackInvalid(DoctaskError):
    """A rule pack could not be loaded.

    Raised rather than skipping the bad rule: a pack that half-loads stops
    checking something nobody realised had stopped being checked.
    """
