"""Break the code on purpose and check the tests notice.

A green suite proves the code passes its tests. It does not prove the tests
would fail if the code were wrong -- and a test that cannot fail is worse than
no test, because it reads as coverage.

Each entry below is a realistic defect: a dropped check, an inverted
comparison, a silent skip. Every one must turn the suite red. A survivor names
a behaviour nothing is currently protecting.

    python scripts/mutation_check.py [-v]

Exits non-zero if any mutation survives, so CI can hold the line.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "doctask"

# Scope deliberately includes the invariant tests that already pass. Leaving
# them out once let a caught mutation look like a survivor.
SCOPE = [
    "tests/unit",
    "tests/invariants",
    "tests/integration",
    # N2's injection defence is not built yet, so its reds are expected and
    # would otherwise make every mutation look "caught".
    "--deselect", "tests/invariants/test_n2_no_orders_from_documents.py::test_injection_attempt_is_reported_as_a_finding",
    "--deselect", "tests/invariants/test_n2_no_orders_from_documents.py::test_injection_finding_cites_the_offending_span",
    "--deselect", "tests/invariants/test_n2_no_orders_from_documents.py::test_injection_does_not_shrink_the_gate",
]

# (file, original, mutated, what the defect would mean in production)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # --- provenance: the anti-hallucination mechanism ---
    ("provenance.py", "    if normalize(source_text[start:end]) != quote:", "    if False:",
     "verifier stops comparing the span to the quote"),
    ("provenance.py", "    if start < 0 or end > len(source_text) or start > end:", "    if False:",
     "verifier stops checking bounds"),
    ("provenance.py", '    return " ".join(text.split())', "    return text",
     "whitespace normalisation dropped, breaking honest citations"),
    ("provenance.py", "    if not quote:", "    if False:",
     "an empty quote is allowed to verify"),

    # --- extraction ---
    ("extract.py", "        if status is CitationStatus.VERIFIED:", "        if True:",
     "extraction stops rejecting unverified facts"),
    ("extract.py", '    material = f"{doc_id}|{field}|{value}|{start}|{end}"',
     '    material = f"{doc_id}|{field}"',
     "fact ids stop distinguishing value and span"),
    ("extract.py",
     '        if not isinstance(entry["char_start"], int) or not isinstance(entry["char_end"], int):',
     "        if False:",
     "offset type checking dropped"),

    # --- classification: the first decision point ---
    ("classify.py", "    if confidence < confidence_floor:", "    if confidence <= confidence_floor:",
     "confidence floor becomes exclusive"),
    ("classify.py", "            outcome=ClassificationOutcome.SKIP,\n            reason=f\"{raw_type} is outside the declared document set\",",
     "            outcome=ClassificationOutcome.ACCEPTED,\n            reason=None,",
     "unknown document types proceed instead of skipping"),

    # --- ingest ---
    ("ingest.py", '        raise UnsupportedFormat(f"cannot read {path.name}: no parser for {suffix!r}")',
     '        return ParsedFile(text="", media_type="text/plain", page_starts=(0,))',
     "unreadable files are silently skipped"),
    ("ingest.py", "            offset += len(text) + 1  # +1 for the newline that joins pages",
     "            offset += len(text)",
     "pdf page offsets drift by one per page"),
    ("corpus.py", "        for file_path in sorted(directory.iterdir()):", "        for file_path in directory.iterdir():",
     "directory ordering becomes non-deterministic"),

    # --- register: the byte-identical proof ---
    ("register.py", '        "state": entry.state.value,', '        "state": "",',
     "entry state stops affecting the content hash"),
    ("register.py",
     '        "citations": [list(_citation_key(c)) for c in sorted(entry.citations, key=_citation_key)],',
     '        "citations": [],',
     "evidence stops affecting the content hash"),
    ("register.py", '        "candidate_values": sorted(entry.candidate_values),', '        "candidate_values": [],',
     "disputed candidates stop affecting the content hash"),
    ("register.py", '    material = f"{vendor}\\x1f{field}"', '    material = f"{vendor}\\x1f{field}\\x1f"',
     "entry identity gains a component (harmless-looking drift)"),
    ("register.py", "        changed=frozenset(i for i in common if before_hashes[i] != after_hashes[i]),",
     "        changed=frozenset(),",
     "diff stops reporting changed entries"),

    # --- reconciliation: surfacing rather than resolving ---
    ("reconcile.py", "        if len(values) == 1:", "        if True:",
     "disagreements are silently collapsed to one value"),
    ("reconcile.py", "            value=None,\n            state=EntryState.DISPUTED,",
     "            value=sorted(values)[0],\n            state=EntryState.ASSERTED,",
     "the register picks a winner on its own"),
    ("reconcile.py", '    material = entry_id + "\\x1f" + "\\x1f".join(sorted(values))',
     '    material = entry_id + "\\x1f" + "\\x1f".join(values)',
     "conflict ids depend on the order facts arrived in"),

    # --- entity resolution ---
    ("entities.py", "DECISIVE_MARGIN = 0.10", "DECISIVE_MARGIN = 0.0",
     "ambiguous vendor names are resolved by coin flip"),
    ("entities.py", "    while words and words[-1] in LEGAL_SUFFIXES:",
     "    while words and any(w in LEGAL_SUFFIXES for w in words):",
     "suffix stripping becomes greedy and eats real words"),
    ("entities.py", "        if key in self._canonical:", "        if False:",
     "exact vendor matches lose their precedence over fuzzy ones"),

    # --- the engine: gate, commit, resume ---
    ("engine.py", "                applied=bool(decision and decision.decision is Decision.APPROVE),",
     "                applied=True,",
     "silence at the gate becomes approval"),
    ("engine.py", "        self._verify_commit(corpus_id, version, entries)", "        pass",
     "commit stops verifying its own post-state"),
    ("engine.py", "        if self._store.is_extracted(document.sha256, EXTRACTOR_VERSION):",
     "        if False:",
     "already-extracted documents are extracted again"),
    ("engine.py", "        if existing is not None and existing != document.doc_id:",
     "        if False:",
     "the same bytes under a new filename are treated as a new document"),
    ("engine.py",
     "            if committed_hashes.get(entry.entry_id) == entry.content_hash:",
     "            if False:",
     "unchanged entries are re-proposed to the reviewer"),
    ("engine.py", "        if status in (RunStatus.COMMITTED, RunStatus.AWAITING_REVIEW):",
     "        if status is RunStatus.COMMITTED:",
     "resuming a parked run re-runs it instead of no-opping"),
    ("engine.py", '        return f"<Engine provider={self._config.provider_name!r} store=Store>"',
     '        return f"<Engine key={self.__api_key!r}>"',
     "the engine repr leaks its api key"),
]


def run_suite(python: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [python, "-m", "pytest", *SCOPE, "-q", "--no-header"],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT),
    )
    lines = [l for l in proc.stdout.strip().splitlines() if "passed" in l or "failed" in l]
    return proc.returncode != 0, (lines[-1].strip() if lines else "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    python = sys.executable
    survivors: list[tuple[str, str]] = []
    skipped: list[str] = []

    print(f"Running {len(MUTATIONS)} mutations against {len(SCOPE)} test targets\n")

    for filename, original, mutated, description in MUTATIONS:
        path = SRC / filename
        backup = path.read_text(encoding="utf-8")

        if original not in backup:
            skipped.append(description)
            print(f"  SKIP    {description}")
            print(f"          (pattern not found in {filename} - mutation is stale)")
            continue

        path.write_text(backup.replace(original, mutated, 1), encoding="utf-8")
        try:
            caught, summary = run_suite(python)
        finally:
            path.write_text(backup, encoding="utf-8")

        print(f"  {'CAUGHT ' if caught else 'SURVIVED'} {description}")
        if args.verbose or not caught:
            print(f"          {filename}: {summary}")
        if not caught:
            survivors.append((filename, description))

    total = len(MUTATIONS) - len(skipped)
    print(f"\n{total - len(survivors)}/{total} mutations caught")

    if skipped:
        print(f"{len(skipped)} stale mutation(s) skipped - update the catalogue")
    for filename, description in survivors:
        print(f"  UNPROTECTED  {filename}: {description}")

    return 1 if survivors or skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
