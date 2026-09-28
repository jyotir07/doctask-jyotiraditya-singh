"""The scripted demo corpora, shared by the CLI and the browser demo.

A demo corpus is a directory of real documents plus a `script.yaml` that says
what the model would have answered about each one. Quotes are resolved to
character offsets against the *parsed* document text at load time, never
hand-written, so the citation a reviewer clicks is a real span in real bytes,
and the same verifier that guards a live run guards this one.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from doctask.corpus import Corpus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPORA_ROOT = PROJECT_ROOT / "corpora"
RULE_PACK = PROJECT_ROOT / "rulepacks" / "contract-playbook.yaml"


class ScriptError(ValueError):
    """A demo script that does not match its documents."""


class UnknownCorpus(LookupError):
    pass


def available_corpora(root: Path = CORPORA_ROOT) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if (p / "docs").is_dir() and (p / "script.yaml").is_file()
    )


def load_demo(name: str, root: Path = CORPORA_ROOT) -> tuple[Corpus, dict]:
    """The corpus and its fake-provider script."""
    if name not in available_corpora(root):
        raise UnknownCorpus(f"no demo corpus named {name!r}")
    corpus = Corpus.from_dir(str(root / name / "docs"), corpus_id=name)
    spec = yaml.safe_load((root / name / "script.yaml").read_text(encoding="utf-8"))
    return corpus, build_script(corpus, spec)


def _resolve(text: str, quote: str, where: str) -> tuple[int, int]:
    """Locate `quote` in `text`, or fail loudly.

    If a scripted quote is not in its document the demo stops here rather than
    emitting a citation that would fail verification later and read like a
    system bug.
    """
    start = text.find(quote)
    if start < 0:
        raise ScriptError(
            f"script error in {where}: quote {quote!r} does not appear in the "
            f"parsed text of that document"
        )
    return start, start + len(quote)


def build_script(corpus: Corpus, spec: dict) -> dict:
    """Turn a corpus script into fake-provider responses with real offsets.

    `fabricated` entries are the one exception to resolving quotes: they model
    a hallucination -- the model points at a real clause (`anchor`) but quotes
    words that are not there. They exist so the demo shows the citation
    verifier rejecting a claim, not just accepting honest ones.
    """
    by_id = {d.doc_id: d for d in corpus.documents}
    script: dict[tuple[str, str], dict] = {}

    for doc_id, entry in (spec.get("documents") or {}).items():
        if doc_id not in by_id:
            raise ScriptError(f"script names {doc_id!r}, which is not in the corpus")
        text = by_id[doc_id].text
        script[("classify", doc_id)] = {
            "doc_type": entry["doc_type"],
            "confidence": entry.get("confidence", 0.97),
        }
        facts = []
        for f in entry.get("facts", []):
            start, end = _resolve(text, f["quote"], f"{doc_id}.{f['field']}")
            facts.append({
                "field": f["field"], "value": f["value"],
                "char_start": start, "char_end": end, "quoted_text": f["quote"],
            })
        for f in entry.get("fabricated", []):
            if f["quote"] in text:
                raise ScriptError(
                    f"fabricated fact {doc_id}.{f['field']} quotes text that is "
                    f"really in the document, so it would verify"
                )
            start, end = _resolve(text, f["anchor"], f"{doc_id}.{f['field']} anchor")
            facts.append({
                "field": f["field"], "value": f["value"],
                "char_start": start, "char_end": end, "quoted_text": f["quote"],
            })
        script[("extract", doc_id)] = {"facts": facts}

    for rule_id, answer in (spec.get("audit") or {}).items():
        payload = {"verdict": answer["verdict"]}
        if answer.get("quote"):
            doc = by_id[answer["doc_id"]]
            start, end = _resolve(doc.text, answer["quote"], f"audit.{rule_id}")
            payload.update({
                "doc_id": doc.doc_id, "char_start": start,
                "char_end": end, "quoted_text": answer["quote"],
            })
        script[("audit", rule_id)] = payload

    return script
