"""Mistral providers — concrete implementations of the gateway's interfaces.

These plug into ``cloud.SanitizingGateway`` as its ``embeddings`` and ``llm``
backends. They are EU-native Mistral (CLAUDE.md): embeddings via Mistral Embed,
extraction via Mistral Small. Configure the account for the EU endpoint with zero
data retention / no training — that is account-level config, not an SDK flag.

By contract they receive ONLY already-sanitized text: the gateway sanitizes before
it ever calls ``embed`` / ``complete``. Construct them only behind the gateway;
nothing should call a provider directly.

Transient API failures (HTTP 429 and 5xx, plus transient network errors) are retried
with exponential backoff + full jitter via ``RetryPolicy`` (stdlib only — no new
dependency). Non-retryable errors fail fast with a clear message.

``mistralai`` is lazy-imported so the stdlib-only core stays importable offline.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from .cloud import EmbeddingProvider, LLMProvider
from .config import load_settings

T = TypeVar("T")

# Network error class names worth retrying. Matched by name so we don't have to
# import httpx at module load (keeps the core importable offline).
_RETRYABLE_NETWORK_ERRORS = frozenset({
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "ReadError", "WriteError", "TimeoutException", "RemoteProtocolError",
    "TransportError", "NoResponseError",
})


class MistralProviderError(RuntimeError):
    """Raised when a Mistral call fails fast (non-retryable) or after retries are
    exhausted. The original SDK error is chained as ``__cause__``."""


def _is_retryable(exc: Exception) -> bool:
    """Retry on HTTP 429 / 5xx (the SDK's errors expose ``status_code``) and on
    transient network errors (matched by class name)."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    return type(exc).__name__ in _RETRYABLE_NETWORK_ERRORS


@dataclass
class RetryPolicy:
    """Exponential backoff with full jitter, capped delay, bounded attempts.

    ``sleep`` and ``rng`` are injectable so tests run instantly and deterministically.
    """

    max_attempts: int = 5            # up to 4 retries after the first attempt
    base_delay: float = 0.5          # seconds
    max_delay: float = 8.0           # cap per-attempt delay
    jitter: bool = True
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    def call(self, fn: Callable[[], T]) -> T:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except MistralProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 — classified by _is_retryable
                if not _is_retryable(exc):
                    raise MistralProviderError(
                        f"Mistral request failed (non-retryable): {exc}"
                    ) from exc
                if attempt == self.max_attempts:
                    raise MistralProviderError(
                        f"Mistral request failed after {self.max_attempts} attempts: {exc}"
                    ) from exc
                self.sleep(self._delay(attempt))
        raise AssertionError("unreachable")  # pragma: no cover

    def _delay(self, attempt: int) -> float:
        ceiling = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return self.rng.uniform(0, ceiling) if self.jitter else ceiling


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

    def __init__(self, api_key: str | None = None, batch_size: int = 64,
                 client=None, retry: RetryPolicy | None = None):
        self._client = client if client is not None else _mistral_client(api_key)
        self._batch_size = batch_size
        self._retry = retry or RetryPolicy()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one 1024-dim vector per input, order preserved. Receives
        ALREADY-SANITIZED text by gateway contract."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start:start + self._batch_size]
            resp = self._retry.call(
                lambda chunk=chunk: self._client.embeddings.create(
                    model=self.MODEL, inputs=chunk)
            )
            # Order by the response index so vectors line up with `chunk` exactly.
            for item in sorted(resp.data, key=lambda d: getattr(d, "index", 0)):
                vectors.append(list(item.embedding))
        return vectors


class MistralLLMProvider(LLMProvider):
    """Text completion via Mistral Small (`mistral-small-latest`). Thin by design —
    no prompt engineering here (that belongs to the extraction step)."""

    MODEL = "mistral-small-latest"

    def __init__(self, api_key: str | None = None, client=None,
                 retry: RetryPolicy | None = None):
        self._client = client if client is not None else _mistral_client(api_key)
        self._retry = retry or RetryPolicy()

    def complete(self, prompt: str) -> str:
        """Return the model's text. Receives ALREADY-SANITIZED text by contract."""
        resp = self._retry.call(
            lambda: self._client.chat.complete(
                model=self.MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
        )
        content = resp.choices[0].message.content
        if isinstance(content, list):
            # Some SDK versions return a list of content chunks; join their text.
            content = "".join(getattr(c, "text", "") or "" for c in content)
        return content or ""
