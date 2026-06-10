"""Cloud egress — the single chokepoint.

Every call that sends user text off the device (embeddings for clustering, the
LLM for refinement extraction) goes through SanitizingGateway. There is no method
on the gateway that sends un-sanitized text, so sanitization cannot be forgotten
or bypassed.

Providers are EU-native Mistral for everything: extraction runs on Mistral Small
and embeddings on Mistral Embed (`mistral-embed`, vector(1024)), both on the EU
endpoint with zero data retention enabled. Both sit behind these interfaces, so
swapping providers stays a one-line change. Sanitization lowers risk, but the
ZDR / no-training provider agreement is what makes egress defensible.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .sanitize import Sanitizer


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, prompt: str) -> str:
        """Receives ALREADY-SANITIZED text only."""


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Receives ALREADY-SANITIZED text only."""


class SanitizingGateway:
    """Wraps providers and sanitizes on the way out. The only public surface for
    reaching the cloud."""

    def __init__(self, sanitizer: Sanitizer, llm: LLMProvider, embeddings: EmbeddingProvider,
                 language: str = "en"):
        self._sanitizer = sanitizer
        self._llm = llm
        self._embeddings = embeddings
        self._language = language

    def extract(self, prompt: str) -> str:
        return self._llm.complete(self._sanitizer.sanitize(prompt, self._language))

    def embed(self, texts: list[str]) -> list[list[float]]:
        clean = [self._sanitizer.sanitize(t, self._language) for t in texts]
        return self._embeddings.embed(clean)


class MockProvider(LLMProvider, EmbeddingProvider):
    """Test double. Records exactly what it received so tests can assert that raw
    PII never reaches a provider."""

    def __init__(self):
        self.received: list[str] = []

    def complete(self, prompt: str) -> str:
        self.received.append(prompt)
        return "ok"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.received.extend(texts)
        return [[0.0] for _ in texts]
