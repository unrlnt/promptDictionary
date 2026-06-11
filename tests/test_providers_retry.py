"""Retry/backoff behavior of the Mistral providers — offline (no SDK, no network).

A fake client is injected so these run in the offline suite. Backoff sleeps are
stubbed out so the tests are instant and deterministic.
"""
from __future__ import annotations

import pytest

from promptdict.providers import (
    MistralEmbeddingProvider,
    MistralLLMProvider,
    MistralProviderError,
    RetryPolicy,
)

EMBED_DIM = 1024


def _no_wait_policy() -> RetryPolicy:
    return RetryPolicy(base_delay=0.0, jitter=False, sleep=lambda _delay: None)


class _RateLimited(Exception):
    """Looks like the SDK's MistralError: carries a 429 status_code."""
    status_code = 429


class _BadRequest(Exception):
    """A non-retryable 400."""
    status_code = 400


class _EmbItem:
    def __init__(self, index: int):
        self.index = index
        self.embedding = [0.0] * EMBED_DIM


class _EmbResp:
    def __init__(self, n: int):
        self.data = [_EmbItem(i) for i in range(n)]


class _FlakyEmbeddings:
    """Fails `fail_times` with 429, then returns a valid response."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def create(self, model, inputs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _RateLimited("rate limited")
        return _EmbResp(len(inputs))


class _FakeClient:
    def __init__(self, embeddings):
        self.embeddings = embeddings


def test_embed_retries_two_429s_then_succeeds():
    embeddings = _FlakyEmbeddings(fail_times=2)
    provider = MistralEmbeddingProvider(
        client=_FakeClient(embeddings), retry=_no_wait_policy()
    )

    vectors = provider.embed(["one synthetic input"])

    assert embeddings.calls == 3            # 2 failures + 1 success
    assert len(vectors) == 1
    assert len(vectors[0]) == EMBED_DIM


def test_embed_fails_fast_on_non_retryable():
    class _Embeddings:
        calls = 0

        def create(self, model, inputs):
            type(self).calls += 1
            raise _BadRequest("bad request")

    embeddings = _Embeddings()
    provider = MistralEmbeddingProvider(
        client=_FakeClient(embeddings), retry=_no_wait_policy()
    )

    with pytest.raises(MistralProviderError):
        provider.embed(["x"])
    assert embeddings.calls == 1            # no retries on a 4xx (non-429)


def test_embed_gives_up_after_max_attempts():
    embeddings = _FlakyEmbeddings(fail_times=99)  # always fails
    policy = RetryPolicy(max_attempts=4, base_delay=0.0, jitter=False,
                         sleep=lambda _delay: None)
    provider = MistralEmbeddingProvider(client=_FakeClient(embeddings), retry=policy)

    with pytest.raises(MistralProviderError):
        provider.embed(["x"])
    assert embeddings.calls == 4            # exhausted all attempts
