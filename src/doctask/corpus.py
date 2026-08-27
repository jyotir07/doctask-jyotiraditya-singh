"""A pile of related documents, addressed by content.

Content addressing is what lets an update cost like an update: a document is
recognised by its bytes, so renaming a file never buys a second extraction.
"""

from __future__ import annotations

from pathlib import Path

from doctask.domain import Document
from doctask.ingest import parse_file, sha256_bytes

DEFAULT_CORPUS_ID = "default"


class Corpus:
    """Documents that describe the same reality and never quite agree."""

    def __init__(self, corpus_id: str, documents: list[Document]) -> None:
        self._corpus_id = corpus_id
        self._documents = list(documents)

    @classmethod
    def from_documents(
        cls, documents: list[Document], corpus_id: str | None = None
    ) -> "Corpus":
        return cls(corpus_id or DEFAULT_CORPUS_ID, documents)

    @classmethod
    def from_dir(cls, path: str, corpus_id: str | None = None) -> "Corpus":
        """Read every file in `path`, in sorted order.

        Sorted rather than filesystem order so that two runs over the same
        directory see the same pile in the same sequence -- otherwise "the
        second run reproduces the first" stops being a checkable claim.
        """
        directory = Path(path)
        documents = []
        for file_path in sorted(directory.iterdir()):
            if not file_path.is_file():
                continue
            parsed = parse_file(file_path)
            documents.append(
                Document(
                    doc_id=file_path.stem,
                    filename=file_path.name,
                    media_type=parsed.media_type,
                    text=parsed.text,
                    sha256=sha256_bytes(file_path.read_bytes()),
                )
            )
        return cls(corpus_id or directory.name, documents)

    @property
    def corpus_id(self) -> str:
        return self._corpus_id

    @property
    def documents(self) -> list[Document]:
        return list(self._documents)
