"""ASGI entry point for a deployed instance.

`build_app` is a factory taking a provider and a DSN, which is what makes the
API testable without a database or a key. A process manager needs something it
can import by name instead, so this module does the wiring from environment.

    uvicorn doctask.asgi:app --host 0.0.0.0 --port 8000

Honest note on what a hosted instance can do. `AnthropicProvider` is a stub, so
the only working provider is the deterministic fake, and the fake answers from
a script keyed on `(stage, doc_id)`. A deployed instance therefore serves every
read path and the whole gate, but `POST /runs` fails with `MissingScriptEntry`
unless the documents you submit match a script this process was given. That is
a real limitation, not a configuration mistake, and it disappears the moment
the live adapter exists.
"""

from __future__ import annotations

import os

from doctask.api import build_app
from doctask.llm import AnthropicProvider, FakeProvider
from doctask.store import Store

DSN = os.environ.get("DOCTASK_DSN")
PROVIDER = os.environ.get("DOCTASK_PROVIDER", "fake").lower()
API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not DSN:
    raise RuntimeError(
        "DOCTASK_DSN is not set. Point it at a PostgreSQL instance with the "
        "pgvector extension available, e.g. a Neon or Supabase connection "
        "string. The schema creates the extension itself."
    )


def _provider_factory():
    if PROVIDER == "anthropic":
        if not API_KEY:
            raise RuntimeError("DOCTASK_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset")
        return AnthropicProvider(API_KEY)
    return FakeProvider({})


# Apply the schema once at import, so the first request does not pay for it and
# a bad DSN fails at boot rather than on someone's first call.
Store.connect(DSN).close()

app = build_app(provider_factory=_provider_factory, dsn=DSN, api_key=API_KEY)


@app.get("/health")
def health() -> dict:
    """Liveness plus a real database round trip.

    A health check that only proves the process is running would report green
    while every request failed on a dead connection.
    """
    store = Store.connect(DSN)
    try:
        with store.raw_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return {"status": "ok", "provider": PROVIDER}
    finally:
        store.close()
