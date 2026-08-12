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
) -> list[str]:
    """Split text into non-overlapping chunks of ~target_tokens (Q12: zero overlap).

    Breaks at sentence boundaries when possible; a chunk larger than the target
    is never emitted, so a doc that is a single long run-on collapses to one chunk.
    """
    enc = _get_encoding()
    tokens = enc.encode(text)

    if len(tokens) <= target_tokens:
        return [text]

    chunks: list[str] = []
    start = 0

    while start < len(tokens):
        end = min(start + target_tokens, len(tokens))

        # Try to break at a sentence boundary near the end of the chunk.
        if end < len(tokens):
            search_start = max(start, end - 50)
            sent_breaks = {enc.encode(s)[0] for s in (".", "!", "?", "\n")}
            for i in range(end - 1, search_start - 1, -1):
                if tokens[i] in sent_breaks:
                    end = i + 1
                    break

        chunk = enc.decode(tokens[start:end])
        if chunk.strip():
            chunks.append(chunk)
        start = end

    return [c for c in chunks if c.strip()]
