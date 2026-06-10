"""Live egress safety test — proves sanitization happens before the real network call.

SKIPPED unless MISTRAL_API_KEY is set (conftest loads repo-root .env first). When it
runs, it builds the production gateway (default_sanitizer + the real
MistralEmbeddingProvider), but wraps the provider to capture the EXACT strings handed
to the SDK. It then embeds a synthetic prompt containing a name, email, IBAN, and
phone, and asserts the captured text holds the typed placeholders and NONE of the raw
PII — i.e. the gateway sanitized before anything left for the network.

Synthetic data only. The input is tiny to keep the embedding call's cost negligible.
"""
from __future__ import annotations

import os

import pytest

from promptdict.cloud import EmbeddingProvider, MockProvider, SanitizingGateway
from promptdict.sanitize import default_sanitizer

pytestmark = pytest.mark.skipif(
    not os.environ.get("MISTRAL_API_KEY"),
    reason="live egress test needs MISTRAL_API_KEY (set it in the environment or .env)",
)

# --- synthetic PII (not real people / accounts) -----------------------------
NAME = "Pinky Featherstone"
EMAIL = "pinky.featherstone@example.org"
IBAN = "NL91ABNA0417164300"
PHONE = "+31 20 123 4567"
PROMPT = f"I'm {NAME}; email {EMAIL}, phone {PHONE}, IBAN {IBAN}."

RAW_PII = [NAME, EMAIL, IBAN, PHONE]
PLACEHOLDERS = ["[PERSON]", "[EMAIL]", "[IBAN]", "[PHONE]"]


class _CapturingEmbeddingProvider(EmbeddingProvider):
    """Wraps a real provider, recording exactly what is passed to it (== what the
    SDK receives) before delegating to the real network call."""

    def __init__(self, inner: EmbeddingProvider):
        self._inner = inner
        self.received: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.received.extend(texts)
        return self._inner.embed(texts)


def test_sanitization_happens_before_real_embed_call():
    pytest.importorskip("mistralai")
    from promptdict.providers import MistralEmbeddingProvider

    capturing = _CapturingEmbeddingProvider(MistralEmbeddingProvider())
    gateway = SanitizingGateway(
        default_sanitizer("en", deny_terms=[NAME]),
        llm=MockProvider(),          # unused by embed(); satisfies the constructor
        embeddings=capturing,
    )

    vectors = gateway.embed([PROMPT])

    # Real call really happened and returned a 1024-dim vector.
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024

    sent = "\n".join(capturing.received)
    for raw in RAW_PII:
        assert raw not in sent, f"raw PII reached the SDK: {raw!r}"
    for placeholder in PLACEHOLDERS:
        assert placeholder in sent, f"missing placeholder before egress: {placeholder}"
