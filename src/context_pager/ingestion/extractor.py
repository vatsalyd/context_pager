from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider

import spacy


# Lazy singletons
_analyzer: AnalyzerEngine | None = None
_spacy_nlp: spacy.Language | None = None


def _get_analyzer() -> AnalyzerEngine:
    global _analyzer
    if _analyzer is None:
        configuration = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
        provider = NlpEngineProvider(nlp_configuration=configuration)
        nlp_engine = provider.create_engine()
        _analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
    return _analyzer


def _get_spacy() -> spacy.Language:
    global _spacy_nlp
    if _spacy_nlp is None:
        _spacy_nlp = spacy.load("en_core_web_sm")
    return _spacy_nlp


# Regex patterns for entity types Presidio doesn't cover well
_ORG_PATTERN = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:\s+(?:Corp|Inc|LLC|Ltd|Co|Group|Company|Bank|University"
    r"|Institute|Foundation|Association|Council|Agency|Department|Ministry))\.?)\b"
)
_MONEY_PATTERN = re.compile(
    r"(?:\$|€|£|¥)\s*[\d,]+(?:\.\d{2})?(?:\s*(?:million|billion|M|B))?"
    r"|\b[\d,]+(?:\.\d{2})?\s*(?:USD|EUR|GBP|JPY)\b"
)
_DATE_PATTERN = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
)


# Map Presidio entity types to our graph entity types
PRESIDIO_TYPE_MAP = {
    "PERSON": "person",
    "EMAIL_ADDRESS": "email",
    "PHONE_NUMBER": "phone",
    "US_SSN": "ssn",
    "URL": "url",
    "CREDIT_CARD": "credit_card",
    "IP_ADDRESS": "ip_address",
    "IBAN": "iban",
}


@dataclass
class ExtractedEntity:
    name: str
    entity_type: str  # 'person', 'org', 'gpe', 'email', 'phone', 'ssn', 'url', 'date', 'money'
    start: int
    end: int
    confidence: float = 0.9


@dataclass
class ExtractionResult:
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)  # (from_name, to_name, relation_type)


def extract_entities(text: str, tenant_id: str = "") -> ExtractionResult:
    """Extract entities from text using Presidio + spaCy NER + regex patterns."""
    result = ExtractionResult()
    seen: set[tuple[str, str]] = set()  # (name_lower, type) dedup

    def _add(name: str, etype: str, start: int, end: int, confidence: float = 0.8):
        key = (name.lower(), etype)
        if key not in seen:
            seen.add(key)
            result.entities.append(ExtractedEntity(
                name=name, entity_type=etype,
                start=start, end=end, confidence=confidence,
            ))

    # 1. spaCy NER for PERSON, ORG, GPE, DATE, MONEY
    nlp = _get_spacy()
    doc = nlp(text)
    spacy_type_map = {
        "PERSON": "person",
        "ORG": "org",
        "GPE": "gpe",
        "LOC": "gpe",
        "DATE": "date",
        "MONEY": "money",
    }
    for ent in doc.ents:
        etype = spacy_type_map.get(ent.label_)
        if etype:
            _add(ent.text.strip(), etype, ent.start_char, ent.end_char, 0.75)

    # 2. Presidio for PII: EMAIL, PHONE, SSN, URL, etc.
    analyzer = _get_analyzer()
    presidio_results = analyzer.analyze(
        text=text,
        entities=list(PRESIDIO_TYPE_MAP.keys()),
        language="en",
    )
    for pr in presidio_results:
        name = text[pr.start:pr.end].strip()
        etype = PRESIDIO_TYPE_MAP.get(pr.entity_type, pr.entity_type.lower())
        _add(name, etype, pr.start, pr.end, pr.score)

    # 3. Regex: ORG (company suffixes)
    for m in _ORG_PATTERN.finditer(text):
        _add(m.group(1).strip(), "org", m.start(), m.end(), 0.7)

    # 4. Regex: MONEY (if spaCy missed any)
    for m in _MONEY_PATTERN.finditer(text):
        _add(m.group(0).strip(), "money", m.start(), m.end(), 0.8)

    # 5. Regex: DATE (if spaCy missed any)
    for m in _DATE_PATTERN.finditer(text):
        _add(m.group(0).strip(), "date", m.start(), m.end(), 0.7)

    # 6. Generate MENTIONED_WITH relations between entities in same text
    entity_names = [(e.name, e.entity_type) for e in result.entities]
    for i, (name_a, _) in enumerate(entity_names):
        for name_b, _ in entity_names[i + 1:]:
            result.relations.append((name_a, name_b, "MENTIONED_WITH"))

    return result
