"""Finding the passages a judged rule needs to see.

Vector search belongs here rather than in entity resolution. "Does this
agreement renew automatically" is a genuinely semantic question over prose,
and a long contract cannot be handed to a model whole.

Every chunk carries the character offsets it was cut from. That is not
bookkeeping -- it is what keeps N1 intact through retrieval. A model reads a
retrieved passage and cites a span inside it; adding the chunk's offset maps
that span back onto the original document, where the verifier can re-read it.
A retrieval layer that lost those offsets would silently make every
model-backed finding uncheckable.
"""

from __future__ import annotations

from dataclasses import dataclass

from doctask.llm import EMBEDDING_DIM

CHUNK_SIZE = 700
CHUNK_OVERLAP = 120


@dataclass(frozen=True)
class IndexResult:
    """What indexing a document cost, for the run's cost line."""

    chunks: int
    provider_calls: int
    input_tokens: int


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    char_start: int
    char_end: int
    text: str


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP,
               doc_id: str = "") -> list[Chunk]:
    """Cut `text` into overlapping windows that remember where they came from.

    Overlap exists so a clause landing on a boundary is whole in at least one
    chunk; without it a payment term could be split across two passages and
    found in neither.
    """
    if size <= overlap:
        raise ValueError("chunk size must exceed the overlap")

    if len(text) <= size:
        return [Chunk(doc_id=doc_id, char_start=0, char_end=len(text), text=text)]

    chunks: list[Chunk] = []
    start = 0
    stride = size - overlap
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(Chunk(doc_id=doc_id, char_start=start, char_end=end,
                            text=text[start:end]))
        if end == len(text):
            break
        start += stride
    return chunks


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


class VectorIndex:
    """Document chunks in PostgreSQL, searched by cosine distance."""

    def __init__(self, store, provider) -> None:
        self._store = store
        self._provider = provider

    def index_document(self, document, size: int = CHUNK_SIZE,
                       overlap: int = CHUNK_OVERLAP) -> IndexResult:
        """Chunk, embed, and store. Idempotent by content.

        Re-indexing the same bytes is free: the chunks are already there, and
        embedding them again would be a bill for nothing.
        """
        if self.chunk_count(document.sha256) > 0:
            return IndexResult(chunks=0, provider_calls=0, input_tokens=0)

        chunks = chunk_text(document.text, size=size, overlap=overlap,
                            doc_id=document.doc_id)
        embedded = self._provider.embed([c.text for c in chunks])
        vectors = embedded.vectors

        with self._store.raw_cursor() as cur:
            for chunk, vector in zip(chunks, vectors):
                cur.execute(
                    """
                    INSERT INTO chunks
                        (doc_sha256, doc_id, char_start, char_end, body, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (document.sha256, chunk.doc_id, chunk.char_start, chunk.char_end,
                     chunk.text, _vector_literal(vector)),
                )
        return IndexResult(chunks=len(chunks), provider_calls=1,
                           input_tokens=embedded.input_tokens)

    def chunk_count(self, doc_sha256: str) -> int:
        with self._store.raw_cursor() as cur:
            cur.execute("SELECT count(*) FROM chunks WHERE doc_sha256 = %s", (doc_sha256,))
            return cur.fetchone()[0]

    def search(self, query: str, limit: int = 5,
               doc_ids: list[str] | None = None) -> list[Chunk]:
        """The passages most likely to answer `query`."""
        vector = _vector_literal(self._provider.embed([query]).vectors[0])

        sql = """
            SELECT doc_id, char_start, char_end, body
            FROM chunks
        """
        params: list = []
        if doc_ids:
            sql += " WHERE doc_id = ANY(%s)"
            params.append(list(doc_ids))
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params.extend([vector, limit])

        with self._store.raw_cursor() as cur:
            cur.execute(sql, tuple(params))
            return [
                Chunk(doc_id=r[0], char_start=r[1], char_end=r[2], text=r[3])
                for r in cur.fetchall()
            ]
