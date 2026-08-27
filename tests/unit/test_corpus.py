"""A corpus is a pile of related documents, addressed by content.

Content addressing is what makes an update cost like an update: a document
already seen is recognised by its bytes, not by its filename, so renaming a
file never buys a second extraction.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from doctask.corpus import Corpus
from doctask.errors import UnsupportedFormat
from tests.support import MSA_TEXT, text_doc


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_documents_are_kept_in_the_order_given():
    corpus = Corpus.from_documents(
        [text_doc("a", "first"), text_doc("b", "second"), text_doc("c", "third")]
    )

    assert [d.doc_id for d in corpus.documents] == ["a", "b", "c"]


def test_a_corpus_id_can_be_given():
    corpus = Corpus.from_documents([text_doc("a", "x")], corpus_id="acme")

    assert corpus.corpus_id == "acme"


def test_a_corpus_without_an_id_gets_a_stable_default():
    """Two corpora built the same way must address the same pile, or an
    incremental ingest would silently start a second deliverable."""
    first = Corpus.from_documents([text_doc("a", "x")])
    second = Corpus.from_documents([text_doc("a", "x")])

    assert first.corpus_id == second.corpus_id


def test_the_same_content_under_two_filenames_hashes_identically(tmp_path):
    """The property content addressing rests on: renaming a file must never
    buy a second extraction."""
    _write(tmp_path, "msa.txt", MSA_TEXT)
    _write(tmp_path, "msa_final_v2_FINAL.txt", MSA_TEXT)

    docs = Corpus.from_dir(str(tmp_path)).documents

    assert docs[0].filename != docs[1].filename
    assert docs[0].sha256 == docs[1].sha256


def test_different_content_hashes_differently(tmp_path):
    _write(tmp_path, "a.txt", "Net 30")
    _write(tmp_path, "b.txt", "Net 45")

    docs = Corpus.from_dir(str(tmp_path)).documents

    assert docs[0].sha256 != docs[1].sha256


# --- reading a directory ---------------------------------------------------


def test_from_dir_reads_the_declared_formats(tmp_path):
    _write(tmp_path, "msa.txt", "Net 30")
    _write(tmp_path, "notes.md", "# Notes")
    _write(tmp_path, "lines.csv", "a,b\n1,2")

    corpus = Corpus.from_dir(str(tmp_path))

    assert sorted(d.filename for d in corpus.documents) == ["lines.csv", "msa.txt", "notes.md"]


def test_from_dir_orders_documents_deterministically(tmp_path, monkeypatch):
    """Two runs over the same directory must see the same pile in the same
    order, or "the second run reproduces the first" stops being checkable.

    The filesystem is forced to hand entries back in the wrong order. NTFS
    happens to return them already sorted, so without this the test would
    pass on Windows whether or not the code sorts anything.
    """
    for name in ["c.txt", "a.txt", "b.txt"]:
        _write(tmp_path, name, name)

    real_iterdir = pathlib.Path.iterdir
    monkeypatch.setattr(
        pathlib.Path,
        "iterdir",
        lambda self: iter(sorted(real_iterdir(self), key=lambda p: p.name, reverse=True)),
    )

    documents = Corpus.from_dir(str(tmp_path)).documents

    assert [d.filename for d in documents] == ["a.txt", "b.txt", "c.txt"]


def test_from_dir_rejects_a_format_it_cannot_read(tmp_path):
    """Silently skipping an unreadable file would make the deliverable
    quietly incomplete, which is the failure mode the brief cares most about."""
    _write(tmp_path, "good.txt", "Net 30")
    (tmp_path / "mystery.xyz").write_bytes(b"\x00\x01\x02")

    with pytest.raises(UnsupportedFormat) as raised:
        Corpus.from_dir(str(tmp_path))

    assert "mystery.xyz" in str(raised.value)


def test_from_dir_computes_content_hashes(tmp_path):
    _write(tmp_path, "msa.txt", "Net 30")

    corpus = Corpus.from_dir(str(tmp_path))

    assert corpus.documents[0].sha256 == hashlib.sha256(b"Net 30").hexdigest()


def test_from_dir_takes_its_corpus_id_from_the_directory_name(tmp_path):
    target = tmp_path / "acme-v1"
    target.mkdir()
    (target / "msa.txt").write_text("Net 30", encoding="utf-8")

    corpus = Corpus.from_dir(str(target))

    assert corpus.corpus_id == "acme-v1"
