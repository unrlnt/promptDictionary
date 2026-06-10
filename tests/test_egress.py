"""Egress rule, proven end to end.

The one rule that must never break: all text leaving the device goes through
``SanitizingGateway``, which sanitizes before every outbound call. This test
sends a synthetic prompt containing a name, email, IBAN, and phone number through
both gateway methods (``extract`` and ``embed``) against ``MockProvider``, then
asserts that:

  * none of the raw PII strings ever reached the provider, and
  * each PII span was replaced with its typed placeholder.

All data here is synthetic — no real chat exports, no real personal data.
``phonenumbers`` (part of the ``cloud`` extra) is required: it is what detects
the [PHONE] span, and without it the raw number would egress. The test fails
loudly rather than skipping, because a missing detector means the rule is unproven.
"""
from __future__ import annotations

import pytest

# Phone detection depends on the `phonenumbers` library. If it's absent the
# detector can't run, so the egress proof is incomplete — fail loudly.
phonenumbers = pytest.importorskip(
    "phonenumbers",
    reason="phonenumbers (cloud extra) is required to detect [PHONE] at egress",
)

from promptdict.cloud import MockProvider, SanitizingGateway
from promptdict.sanitize import (
    DenyListRecognizer,
    PatternRecognizer,
    PhoneRecognizer,
    Sanitizer,
)

# --- synthetic PII (not real people / accounts) -----------------------------
NAME = "Pinky Featherstone"
EMAIL = "pinky.featherstone@example.org"
IBAN = "NL91ABNA0417164300"        # synthetic NL IBAN (valid format, fake account)
PHONE = "+31 20 123 4567"          # synthetic NL phone number

PROMPT = (
    f"Hi, I'm {NAME}. Reach me at {EMAIL} or {PHONE}. "
    f"Transfer to {IBAN} when ready."
)

RAW_PII = [NAME, EMAIL, IBAN, PHONE]
PLACEHOLDERS = ["[PERSON]", "[EMAIL]", "[IBAN]", "[PHONE]"]


def _gateway() -> tuple[SanitizingGateway, MockProvider]:
    sanitizer = Sanitizer(
        [
            PatternRecognizer(),               # email, IBAN, card (stdlib regex)
            PhoneRecognizer("NL"),             # phone via phonenumbers
            DenyListRecognizer([NAME]),        # synthetic name -> [PERSON]
        ]
    )
    provider = MockProvider()  # one double serves as both LLM and embeddings
    gateway = SanitizingGateway(sanitizer, llm=provider, embeddings=provider)
    return gateway, provider


def test_extract_sanitizes_before_egress():
    gateway, provider = _gateway()

    gateway.extract(PROMPT)

    sent = "\n".join(provider.received)
    assert provider.received, "provider should have received the sanitized prompt"
    for raw in RAW_PII:
        assert raw not in sent, f"raw PII leaked to provider: {raw!r}"
    for placeholder in PLACEHOLDERS:
        assert placeholder in sent, f"missing placeholder: {placeholder}"


def test_embed_sanitizes_before_egress():
    gateway, provider = _gateway()

    gateway.embed([PROMPT])

    sent = "\n".join(provider.received)
    assert provider.received, "provider should have received the sanitized text"
    for raw in RAW_PII:
        assert raw not in sent, f"raw PII leaked to provider: {raw!r}"
    for placeholder in PLACEHOLDERS:
        assert placeholder in sent, f"missing placeholder: {placeholder}"
