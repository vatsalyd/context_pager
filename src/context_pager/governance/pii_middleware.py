from __future__ import annotations

import json
from typing import Any

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig


# Shared Presidio singletons (lazy-initialized to avoid double spaCy load)
_analyzer: AnalyzerEngine | None = None
_anonymizer: AnonymizerEngine | None = None


def _get_analyzer() -> AnalyzerEngine:
    global _analyzer
    if _analyzer is None:
        _analyzer = AnalyzerEngine()
    return _analyzer


def _get_anonymizer() -> AnonymizerEngine:
    global _anonymizer
    if _anonymizer is None:
        _anonymizer = AnonymizerEngine()
    return _anonymizer


# PII entity types we care about
PII_ENTITIES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "CREDIT_CARD",
    "IBAN_CODE",
    "IP_ADDRESS",
    "PERSON",
    "LOCATION",
    "ORGANIZATION",
]

# Operator configs for anonymization
OPERATORS = {
    "DEFAULT": OperatorConfig("replace", {"new_value": "[REDACTED]"}),
    "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": "[REDACTED_EMAIL]"}),
    "PHONE_NUMBER": OperatorConfig("replace", {"new_value": "[REDACTED_PHONE]"}),
    "US_SSN": OperatorConfig("replace", {"new_value": "[REDACTED_SSN]"}),
    "CREDIT_CARD": OperatorConfig("replace", {"new_value": "[REDACTED_CARD]"}),
    "IBAN_CODE": OperatorConfig("replace", {"new_value": "[REDACTED_IBAN]"}),
}


async def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Redact PII from text. Returns (redacted_text, counts_by_type)."""
    if not text or not text.strip():
        return text, {}

    analyzer = _get_analyzer()
    anonymizer = _get_anonymizer()

    # Presidio's analyze/anonymize are sync + CPU-bound; offload to executor
    import asyncio
    results = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: analyzer.analyze(text=text, entities=PII_ENTITIES, language="en"),
    )
    if not results:
        return text, {}

    anonymized = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators=OPERATORS,
        ),
    )

    counts: dict[str, int] = {}
    for r in results:
        counts[r.entity_type] = counts.get(r.entity_type, 0) + 1

    return anonymized.text, counts


async def redact_json_envelope(envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Recursively redact PII from all string fields in the JSON envelope."""
    total_counts: dict[str, int] = {}

    async def redact_value(obj: Any) -> Any:
        if isinstance(obj, str):
            redacted, counts = await redact_text(obj)
            for k, v in counts.items():
                total_counts[k] = total_counts.get(k, 0) + v
            return redacted
        elif isinstance(obj, dict):
            return {k: await redact_value(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [await redact_value(item) for item in obj]
        return obj

    redacted = await redact_value(envelope)
    return redacted, total_counts
