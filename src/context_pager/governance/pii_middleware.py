from __future__ import annotations

import re
from typing import Any

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from context_pager.deps import Dependencies


# Initialize Presidio
_analyzer = AnalyzerEngine()
_anonymizer = AnonymizerEngine()

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


class PIIRedactionMiddleware:
    """
    FastMCP middleware that redacts PII from tool results before they reach the agent.
    Runs AFTER tool execution but BEFORE response is returned.
    """

    def __init__(self):
        self.analyzer = _analyzer
        self.anonymizer = _anonymizer

    async def __call__(self, request, call_next):
        # This is a tool call middleware - we need to hook into the tool result
        response = await call_next(request)
        return response

    async def redact_text(self, text: str) -> tuple[str, dict[str, int]]:
        """Redact PII from text, return redacted text and count by type."""
        if not text or not text.strip():
            return text, {}

        results = self.analyzer.analyze(text=text, entities=PII_ENTITIES, language="en")
        if not results:
            return text, {}

        anonymized = self.anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators=OPERATORS,
        )

        # Count by type
        counts: dict[str, int] = {}
        for r in results:
            counts[r.entity_type] = counts.get(r.entity_type, 0) + 1

        return anonymized.text, counts

    async def redact_json_envelope(self, envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        """Redact PII from all text fields in the JSON envelope."""
        total_counts: dict[str, int] = {}

        def redact_value(obj: Any, path: str = "") -> Any:
            if isinstance(obj, str):
                redacted, counts = await self.redact_text(obj)
                for k, v in counts.items():
                    total_counts[k] = total_counts.get(k, 0) + v
                return redacted
            elif isinstance(obj, dict):
                return {k: redact_value(v, f"{path}.{k}") for k, v in obj.items()}
            elif isinstance(obj, list):
                return [redact_value(item, f"{path}[{i}]") for i, item in enumerate(obj)]
            return obj

        redacted = redact_value(envelope)
        return redacted, total_counts


# MCP middleware function
async def pii_middleware(request, call_next):
    """FastMCP middleware hook for PII redaction on tool results."""
    response = await call_next(request)

    # Only process tool call responses (CallToolResult with TextContent)
    if hasattr(response, "content") and response.content:
        for content in response.content:
            if content.type == "text":
                try:
                    import json
                    envelope = json.loads(content.text)
                    middleware = PIIRedactionMiddleware()
                    redacted_envelope, counts = await middleware.redact_json_envelope(envelope)
                    if counts:
                        # Add PII redaction metadata
                        redacted_envelope.setdefault("metadata", {})["pii_redacted"] = counts
                    content.text = json.dumps(redacted_envelope)
                except (json.JSONDecodeError, AttributeError):
                    pass  # Not a JSON envelope, skip

    return response