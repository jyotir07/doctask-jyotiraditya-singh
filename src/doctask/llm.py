"""The provider seam.

Provider-agnostic by design: orchestration never imports a vendor SDK. The
fake is not a convenience for tests, it is the instrument the cost invariants
are measured with, which is why it records every call it is asked to make.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from doctask.errors import MissingScriptEntry


@dataclass(frozen=True)
class ProviderCall:
    stage: str
    key: str | None
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    input_tokens: int


@dataclass(frozen=True)
class StructuredResult:
    data: dict
    input_tokens: int
    output_tokens: int


class Provider(Protocol):
    """What orchestration is allowed to know about a model."""

    def complete(self, *, stage: str, key: str | None, instructions: str,
                 data: str, schema: dict) -> StructuredResult:
        ...


def estimate_tokens(text: str) -> int:
    """Rough token count, deterministic by construction.

    Four characters per token is the usual approximation. Exactness does not
    matter here; stability does, because the cost invariants compare counts
    across runs and a jittery estimate would make them flaky.
    """
    return max(1, len(text) // 4)


class FakeProvider:
    """Deterministic provider driven by a script, keyed on (stage, key).

    Returns raw model-shaped JSON, exactly what a real provider returns over
    the wire. Everything downstream of the script -- parsing, citation
    construction, verification, composition -- is real production code, so
    these tests are not testing their own mocks.

    Ships in the package rather than in the test tree so that a stranger can
    run the whole demo without a key.
    """

    def __init__(self, script: dict[tuple[str, str], dict]) -> None:
        self._script = dict(script)
        self._calls: list[ProviderCall] = []

    @property
    def calls(self) -> list[ProviderCall]:
        return list(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def complete(self, *, stage: str, key: str | None, instructions: str,
                 data: str, schema: dict) -> StructuredResult:
        try:
            payload = self._script[(stage, key)]
        except KeyError:
            raise MissingScriptEntry(
                f"no scripted response for stage={stage!r} key={key!r}"
            ) from None

        input_tokens = estimate_tokens(instructions) + estimate_tokens(data)
        output_tokens = estimate_tokens(json.dumps(payload, sort_keys=True))
        self._calls.append(
            ProviderCall(
                stage=stage,
                key=key,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        )
        return StructuredResult(
            data=payload, input_tokens=input_tokens, output_tokens=output_tokens
        )

    def embed(self, texts: list[str]) -> EmbeddingResult:
        """Deterministic embeddings, reporting cost like any other call.

        Embedding is not free, so it reports tokens: a cost line that counted
        only completions would under-report every run that indexed anything.
        """
        input_tokens = sum(estimate_tokens(t) for t in texts)
        self._calls.append(
            ProviderCall(stage="embed", key=None, input_tokens=input_tokens, output_tokens=0)
        )
        return EmbeddingResult(vectors=[fake_embedding(t) for t in texts],
                               input_tokens=input_tokens)


class AnthropicProvider:
    """The live path. Schema-constrained via tool use."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-5") -> None:
        raise NotImplementedError

    def complete(self, *, stage: str, key: str | None, instructions: str,
                 data: str, schema: dict) -> StructuredResult:
        raise NotImplementedError


EMBEDDING_DIM = 64


def fake_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """A deterministic embedding with real lexical similarity.

    The hashing trick: each word lands in a bucket chosen by its sha256, and
    the vector is the normalised bucket count. sha256 rather than Python's
    hash() because the latter is randomised per process, and an embedding that
    changed between processes would make stored vectors meaningless.

    A pure content hash would be deterministic too, and useless -- every
    passage would be equidistant from every query, and a retrieval test would
    pass while returning an arbitrary chunk. This carries enough real
    similarity that searching for "renews automatically" finds the renewal
    clause and not the insurance one.
    """
    import math
    import re as _re

    vector = [0.0] * dim
    for word in _re.findall(r"\w+", text.lower()):
        bucket = int(hashlib.sha256(word.encode("utf-8")).hexdigest()[:8], 16) % dim
        vector[bucket] += 1.0

    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]
