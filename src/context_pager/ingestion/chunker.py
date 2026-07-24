from __future__ import annotations

import tiktoken


_encoding: tiktoken.Encoding | None = None


def _get_encoding() -> tiktoken.Encoding:
    global _encoding
    if _encoding is None:
        _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding


def count_tokens(text: str) -> int:
    return len(_get_encoding().encode(text))


def chunk_text(
    text: str,
    target_tokens: int = 512,
    overlap_tokens: int = 50,
) -> list[str]:
    """Split text into chunks of approximately target_tokens with overlap.

    Uses sentence boundaries when possible to avoid mid-sentence breaks.
    """
    enc = _get_encoding()
    tokens = enc.encode(text)

    if len(tokens) <= target_tokens:
        return [text]

    stride = target_tokens - overlap_tokens
    chunks: list[str] = []
    start = 0

    while start < len(tokens):
        end = min(start + target_tokens, len(tokens))
        chunk_tokens = tokens[start:end]

        # Try to break at sentence boundary near the end of chunk
        if end < len(tokens):
            # Look back up to 50 tokens for a sentence break
            search_start = max(0, len(chunk_tokens) - 50)
            best_break = len(chunk_tokens)
            sent_breaks = {enc.encode(s)[0] for s in (".", "!", "?", "\n")}
            for i in range(len(chunk_tokens) - 1, search_start - 1, -1):
                if chunk_tokens[i] in sent_breaks:
                    best_break = i + 1
                    break
            chunk_tokens = chunk_tokens[:best_break]

        chunks.append(enc.decode(chunk_tokens))
        start += stride

    return [c for c in chunks if c.strip()]
