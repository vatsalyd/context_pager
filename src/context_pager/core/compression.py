from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from context_pager.config import get_bridge_settings


class Compressor(Protocol):
    """Compression interface. Both real and fake compressors implement it."""

    async def compress(self, text: str, target_tokens: int) -> str:
        ...


class LLMLinguaCompressor:
    """Full mode: LLMLingua-2 prompt compression. ~1-2 GB RAM."""

    def __init__(self, model_name: str):
        from llmlingua import PromptCompressor

        self._model = PromptCompressor(model_name=model_name, use_llmlingua2=True)

    async def compress(self, text: str, target_tokens: int) -> str:
        rate = min(target_tokens / max(count_tokens(text), 1), 1.0)
        return await _run_in_executor(
            lambda: self._model.compress_prompt(
                text,
                rate=rate,
                force_tokens=["\n", "?", "!"],
                use_llmlingua2=True,
            )["compressed_prompt"]
        )


class TruncationCompressor:
    """Lite mode: truncate to target tokens. No model, fully reliable."""

    async def compress(self, text: str, target_tokens: int) -> str:
        return text[: target_tokens * 4]


def build_compressor(settings=None) -> Compressor:
    settings = settings or get_bridge_settings()
    if settings.lite:
        return TruncationCompressor()
    return LLMLinguaCompressor(settings.llmlingua_model)


def count_tokens(text: str) -> int:
    """Approximate token count (4 chars ~= 1 token)."""
    return len(text) // 4


def generate_summary(text: str, max_chars: int = 500) -> str:
    """Summary from first N chars of compressed text."""
    return text[:max_chars].strip()


async def _run_in_executor(fn):
    import asyncio

    return await asyncio.get_event_loop().run_in_executor(None, fn)


@dataclass
class CompressedPage:
    content: str
    token_count: int
    original_tokens: int
    compression_ratio: str
    cost_saved_usd: float = 0.0
    pii_redacted: dict = None
    skipped_compression: bool = False

    def __post_init__(self):
        if self.pii_redacted is None:
            self.pii_redacted = {}
