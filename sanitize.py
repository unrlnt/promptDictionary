"""Sanitization — the egress safety layer.

Production posture
------------------
Raw text lives only in the user's own RLS-protected rows. The ONE place it may
cross a boundary (a cloud LLM / embedding API, or team sharing) it must pass
through a Sanitizer first. This module is that layer.

Recognizers find PII spans; the Sanitizer replaces them with typed placeholders
(`[PERSON]`, `[EMAIL]`, ...). Two recognizers ship:

  - PatternRecognizer  : regex for email / phone / card / IBAN. Zero dependencies,
                         always available, deterministic.
  - PresidioRecognizer : Microsoft Presidio + spaCy/transformer NER for names,
                         locations, organisations, etc. Multilingual (en, nl, ...).
                         This is the production default and is what actually catches
                         the personal names that regex never will.

Honest limitation: automated NER reduces risk but is not airtight. For genuinely
sensitive material the production design pairs this with (a) a zero-data-retention
provider agreement and (b) minimising how much raw text is sent at all. See cloud.py.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Entity:
    type: str        # PERSON, EMAIL, PHONE, IBAN, CARD, LOCATION, ORG, ...
    start: int
    end: int


class Recognizer(ABC):
    @abstractmethod
    def analyze(self, text: str, language: str = "en") -> list[Entity]:
        ...


class PatternRecognizer(Recognizer):
    """Dependency-free, deterministic. Errs toward over-redaction at egress."""

    PATTERNS: list[tuple[str, re.Pattern]] = [
        ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
        ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
        ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ]

    def analyze(self, text: str, language: str = "en") -> list[Entity]:
        out: list[Entity] = []
        for etype, pattern in self.PATTERNS:
            for m in pattern.finditer(text):
                out.append(Entity(etype, m.start(), m.end()))
        return out


class DenyListRecognizer(Recognizer):
    """Exact-match names/terms a user explicitly wants scrubbed. Also used in tests
    to exercise PERSON handling where the NER model is unavailable."""

    def __init__(self, terms: list[str], etype: str = "PERSON"):
        self._etype = etype
        self._terms = sorted(set(terms), key=len, reverse=True)

    def analyze(self, text: str, language: str = "en") -> list[Entity]:
        out: list[Entity] = []
        for term in self._terms:
            for m in re.finditer(re.escape(term), text):
                out.append(Entity(self._etype, m.start(), m.end()))
        return out


class PhoneRecognizer(Recognizer):
    """Accurate phone detection via the `phonenumbers` library (same approach
    Presidio uses). A default region lets it catch national-format numbers that
    have no country code; set it from the user's locale (e.g. "NL")."""

    def __init__(self, default_region: str | None = "NL"):
        self._region = default_region
        try:
            import phonenumbers  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False

    def analyze(self, text: str, language: str = "en") -> list[Entity]:
        if not self._available:
            return []
        import phonenumbers
        out: list[Entity] = []
        for region in {self._region, None}:
            for match in phonenumbers.PhoneNumberMatcher(text, region):
                out.append(Entity("PHONE", match.start, match.end))
        return out


class PresidioRecognizer(Recognizer):
    """Production NER recognizer. Catches names/locations/orgs across languages.

    Requires `presidio-analyzer` plus a spaCy model per language (e.g.
    `en_core_web_lg`, `nl_core_news_lg`). Constructing this without the model
    available raises RuntimeError with install guidance, so failures are loud
    rather than silently leaking PII.
    """

    DEFAULT_ENTITIES = ["PERSON", "LOCATION", "ORGANIZATION", "EMAIL_ADDRESS",
                        "PHONE_NUMBER", "IBAN_CODE", "CREDIT_CARD"]

    def __init__(self, languages: tuple[str, ...] = ("en", "nl")):
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Install presidio-analyzer to use PresidioRecognizer.") from e

        models = [{"lang_code": l, "model_name": self._model_for(l)} for l in languages]
        try:
            nlp_engine = NlpEngineProvider(
                nlp_configuration={"nlp_engine_name": "spacy", "models": models}
            ).create_engine()
        except Exception as e:  # model not downloaded, etc.
            wanted = ", ".join(self._model_for(l) for l in languages)
            raise RuntimeError(
                f"spaCy model(s) not available ({wanted}). Install with e.g. "
                f"`python -m spacy download {self._model_for(languages[0])}`."
            ) from e
        self._engine = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=list(languages))

    @staticmethod
    def _model_for(lang: str) -> str:
        return {"en": "en_core_web_lg", "nl": "nl_core_news_lg"}.get(lang, "xx_ent_wiki_sm")

    def analyze(self, text: str, language: str = "en") -> list[Entity]:
        results = self._engine.analyze(text=text, language=language, entities=self.DEFAULT_ENTITIES)
        norm = {"EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE",
                "IBAN_CODE": "IBAN", "CREDIT_CARD": "CARD"}
        return [Entity(norm.get(r.entity_type, r.entity_type), r.start, r.end) for r in results]


class Sanitizer:
    """Runs recognizers and replaces every detected span with a typed placeholder."""

    def __init__(self, recognizers: list[Recognizer]):
        self._recognizers = recognizers

    def sanitize(self, text: str, language: str = "en") -> str:
        spans: list[Entity] = []
        for r in self._recognizers:
            spans.extend(r.analyze(text, language))
        if not spans:
            return text
        # Resolve overlaps: keep the widest span, replace right-to-left so offsets stay valid.
        spans = self._dedupe(spans)
        result = text
        for e in sorted(spans, key=lambda s: s.start, reverse=True):
            result = result[: e.start] + f"[{e.type}]" + result[e.end :]
        return result

    @staticmethod
    def _dedupe(spans: list[Entity]) -> list[Entity]:
        spans = sorted(spans, key=lambda s: (s.start, -(s.end - s.start)))
        kept: list[Entity] = []
        last_end = -1
        for e in spans:
            if e.start >= last_end:
                kept.append(e)
                last_end = e.end
        return kept


def default_sanitizer(language: str = "en", deny_terms: list[str] | None = None) -> Sanitizer:
    """Build the production sanitizer: Presidio NER if available, otherwise the
    pattern recognizer alone (with a loud note), plus any user deny-list."""
    recognizers: list[Recognizer] = [PatternRecognizer(), PhoneRecognizer()]
    if deny_terms:
        recognizers.append(DenyListRecognizer(deny_terms))
    try:
        recognizers.insert(0, PresidioRecognizer(languages=(language,) if language != "en" else ("en",)))
    except RuntimeError:
        # NER model unavailable in this environment; pattern + deny-list still apply.
        pass
    return Sanitizer(recognizers)
