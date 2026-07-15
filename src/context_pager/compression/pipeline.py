from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from context_pager.config import settings
from context_pager.deps import Dependencies


@dataclass
class Page:
    page: int
    content: str
    token_count: int


@dataclass
class CompressionMetadata:
    original_tokens: int
    compressed_tokens: int
    compression_ratio: str
    cost_saved_usd: float = 0.0
    pii_redacted: dict[str, int] = field(default_factory=dict)
    cache_hit: bool = False
    skipped_compression: bool = False
    elapsed_ms: int = 0


@dataclass
class CompressedResult:
    pages: list[Page]
    summary: str
    metadata: CompressionMetadata


# Presidio setup
_analyzer = AnalyzerEngine()
_anonymizer = AnonymizerEngine()

PII_ENTITIES = [
    "EMAIL_ADDRESS", "PHONE_NUMBER", "US_SSN", "CREDIT_CARD",
    "IBAN_CODE", "IP_ADDRESS", "PERSON", "LOCATION", "ORGANIZATION",
]

OPERATORS = {
    "DEFAULT": OperatorConfig("replace", {"new_value": "[REDACTED]"}),
    "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": "[REDACTED_EMAIL]"}),
    "PHONE_NUMBER": OperatorConfig("replace", {"new_value": "[REDACTED_PHONE]"}),
    "US_SSN": OperatorConfig("replace", {"new_value": "[REDACTED_SSN]"}),
    "CREDIT_CARD": OperatorConfig("replace", {"new_value": "[REDACTED_CARD]"}),
    "IBAN_CODE": OperatorConfig("replace", {"new_value": "[REDACTED_IBAN]"}),
}


async def pii_redact_with_counts(text: str) -> tuple[str, dict[str, int]]:
    """Redact PII and return redacted text + counts by type."""
    if not text:
        return text, {}

    results = _analyzer.analyze(text=text, entities=PII_ENTITIES, language="en")
    if not results:
        return text, {}

    anonymized = _anonymizer.anonymize(
        text=text,
        analyzer_results=results,
        operators=OPERATORS,
    )

    counts: dict[str, int] = {}
    for r in results:
        counts[r.entity_type] = counts.get(r.entity_type, 0) + 1

    return anonymized.text, counts


async def pii_redact(text: str) -> str:
    """Simple redact without counts."""
    redacted, _ = await pii_redact_with_counts(text)
    return redacted


def count_tokens(text: str) -> int:
    """Approximate token count (4 chars ~= 1 token)."""
    return len(text) // 4


def generate_summary(text: str, max_chars: int = 500) -> str:
    """Generate summary from first N chars of compressed text."""
    return text[:max_chars].strip()


async def llmlingua_compress(text: str, target_tokens: int) -> str:
    """Run LLMLingua-2 compression."""
    compressor = await Dependencies.compressor()
    # LLMLingua-2 expects rate, not target_tokens
    rate = min(target_tokens / max(count_tokens(text), 1), 1.0)
    result = compressor.compress_prompt(
        text,
        rate=rate,
        force_tokens=["\n", "?", "!"],
        use_llmlingua2=True,
    )
    return result["compressed_prompt"]


async def ollama_extract(text: str, focus_area: str, target_tokens: int) -> str:
    """Run Ollama Llama 3 8B for query-focused extraction."""
    ollama = await Dependencies.ollama()
    prompt = f"""Extract sentences relevant to: "{focus_area}" from the following text.
Keep under {target_tokens} tokens. Preserve specific numbers, names, decisions.
Output only the extracted text, no commentary.

Text:
{text}"""
    response = await ollama.generate(
        model="llama3:8b-q4_K_M",
        prompt=prompt,
        options={"temperature": 0.1, "num_predict": target_tokens * 2},
    )
    return response["response"]


async def compress_pipeline(
    raw_text: str,
    focus_area: Optional[str],
    max_return_tokens: int,
    use_ollama: bool,
) -> CompressedResult:
    """Full compression pipeline: redact -> compress -> redact check."""
    original_tokens = count_tokens(raw_text)

    # Short-circuit for small docs
    if original_tokens <= max_return_tokens:
        redacted, pii_counts = await pii_redact_with_counts(raw_text)
        return CompressedResult(
            pages=[Page(page=1, content=redacted, token_count=original_tokens)],
            summary=generate_summary(redacted),
            metadata=CompressionMetadata(
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio="1.0x",
                skipped_compression=True,
                pii_redacted=pii_counts,
            ),
        )

    # Step 1: Pre-compression PII redaction
    redacted_text, pii_counts = await pii_redact_with_counts(raw_text)

    # Step 2: Compression
    if use_ollama and focus_area:
        # Two-stage: LLMLingua density reduction then Ollama query-focused
        stage1 = await llmlingua_compress(redacted_text, max_return_tokens * 3)
        compressed = await ollama_extract(stage1, focus_area, max_return_tokens)
    else:
        # Free tier: LLMLingua-2 only (focus_area ignored)
        compressed = await llmlingua_compress(redacted_text, max_return_tokens)

    # Step 3: Defense-in-depth PII scan on compressed output
    final_text, pii_counts_2 = await pii_redact_with_counts(compressed)
    # Merge PII counts
    for k, v in pii_counts_2.items():
        pii_counts[k] = pii_counts.get(k, 0) + v

    compressed_tokens = count_tokens(final_text)

    return CompressedResult(
        pages=[Page(page=1, content=final_text, token_count=compressed_tokens)],
        summary=generate_summary(final_text),
        metadata=CompressionMetadata(
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=f"{original_tokens / max(compressed_tokens, 1):.1f}x",
            skipped_compression=False,
            pii_redacted=pii_counts,
        ),
    )