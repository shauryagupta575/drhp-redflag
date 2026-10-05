"""Split page text into overlapping token windows (build guide Phase 2 step 4).

The guide suggests ~800-token chunks; bge-small embeds at most 512 tokens, so chunks are
~500 tokens with ~100 tokens of overlap so that every token is embedded. Token counts use
the embedding model's own tokenizer; chunk text is the original page text (not decoded
tokens), and each chunk records the PDF pages it spans.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

CHUNK_TOKENS = 500
OVERLAP_TOKENS = 100

# text -> list of (char_start, char_end) offsets, one per token
Offsets = Callable[[str], list[tuple[int, int]]]


class PageText(Protocol):
    page_no: int
    text: str


@dataclass(frozen=True)
class TextChunk:
    page_start: int
    page_end: int
    text: str


def chunk_pages(
    pages: Sequence[PageText],
    offsets: Offsets,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[TextChunk]:
    if not 0 <= overlap_tokens < chunk_tokens:
        raise ValueError("overlap must be smaller than the chunk size")
    # One entry per token across the whole document: (page_no, char_start, char_end).
    stream: list[tuple[int, int, int]] = []
    texts: dict[int, str] = {}
    for page in pages:
        texts[page.page_no] = page.text
        stream.extend((page.page_no, a, b) for a, b in offsets(page.text))
    chunks: list[TextChunk] = []
    step = chunk_tokens - overlap_tokens
    for start in range(0, len(stream), step):
        window = stream[start : start + chunk_tokens]
        parts: list[str] = []
        for page_no in dict.fromkeys(t[0] for t in window):
            spans = [t for t in window if t[0] == page_no]
            parts.append(texts[page_no][spans[0][1] : spans[-1][2]])
        chunks.append(TextChunk(window[0][0], window[-1][0], "\n".join(parts)))
        if start + chunk_tokens >= len(stream):
            break
    return chunks
