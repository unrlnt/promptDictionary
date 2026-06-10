"""Mistral providers — concrete implementations of the gateway's interfaces.

These plug into ``cloud.SanitizingGateway`` as its ``embeddings`` and ``llm``
backends. They are EU-native Mistral (CLAUDE.md): embeddings via Mistral Embed,
extraction via Mistral Small. Configure the account for the EU endpoint with zero
data retention / no training — that is account-level config, not an SDK flag.

By contract they receive ONLY already-sanitized text: the gateway sanitizes before
it ever calls ``embed`` / ``complete``. Construct them only behind the gateway;
nothing should call a provider directly.

``mistralai`` is lazy-imported so the stdlib-only core stays importable offline.
"""
from __future__ import annotations

from .cloud import EmbeddingProvider, LLMProvider
from .config import load_settings


def _mistral_client(api_key: str | None):
    """Construct a Mistral SDK client. Lazy-imported so the core stays importable
    offline; tolerant of both SDK layouts (`mistralai.Mistral` and the
    Speakeasy-generated `mistralai.client.Mistral`)."""
    try:
        from mistralai import Mistral
    except ImportError:
        from mistralai.client import Mistral
    return Mistral(api_key=api_key or load_settings().require("mistral_api_key"))


class MistralEmbeddingProvider(EmbeddingProvider):
    """Embeddings via Mistral Embed (`mistral-embed`, 1024-dim)."""

    MODEL = "mistral-embed"

    def __init__(self, api_key: str | None = None, batch_size: int = 64):
        self._client = _mistral_client(api_key)
        self._batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one 1024-dim vector per input, order preserved. Receives
        ALREADY-SANITIZED text by gateway contract."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start:start + self._batch_size]
            resp = self._client.embeddings.create(model=self.MODEL, inputs=chunk)
            # Order by the response index so vectors line up with `chunk` exactly.
            for item in sorted(resp.data, key=lambda d: getattr(d, "index", 0)):
                vectors.append(list(item.embedding))
        return vectors


class MistralLLMProvider(LLMProvider):
    """Text completion via Mistral Small (`mistral-small-latest`). Thin by design —
    no prompt engineering here (that belongs to the extraction step)."""

    MODEL = "mistral-small-latest"

    def __init__(self, api_key: str | None = None):
        self._client = _mistral_client(api_key)

    def complete(self, prompt: str) -> str:
        """Return the model's text. Receives ALREADY-SANITIZED text by contract."""
        resp = self._client.chat.complete(
            model=self.MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp.choices[0].message.content
        if isinstance(content, list):
            # Some SDK versions return a list of content chunks; join their text.
            content = "".join(getattr(c, "text", "") or "" for c in content)
        return content or ""
