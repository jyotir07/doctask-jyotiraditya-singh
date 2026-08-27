"""Citation verification -- the mechanism behind N1.

Re-reads the cited span from the source and compares it to the quote. This is
byte comparison, never a model call: whether a claim is supported by a
document is not a question that needs judgement, and routing it through a
model would make the anti-hallucination check itself hallucinate.
"""

from __future__ import annotations

from doctask.domain import Citation, CitationStatus


def normalize(text: str) -> str:
    """Collapse runs of whitespace and strip the ends.

    A PDF extractor can break a line in the middle of a quoted phrase, and an
    honest citation must survive that. Changing the words must not survive it,
    which is why this normalises whitespace only.
    """
    return " ".join(text.split())


def verify_citation(source_text: str, citation: Citation) -> CitationStatus:
    """Does `citation` hold up when the source bytes are re-read?"""
    start, end = citation.char_start, citation.char_end

    if start < 0 or end > len(source_text) or start > end:
        return CitationStatus.OUT_OF_BOUNDS

    quote = normalize(citation.quoted_text)
    if not quote:
        # An empty quote supports nothing, so it can never verify.
        return CitationStatus.SPAN_MISMATCH

    if normalize(source_text[start:end]) != quote:
        return CitationStatus.SPAN_MISMATCH

    return CitationStatus.VERIFIED
