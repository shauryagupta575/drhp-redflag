"""Local embeddings with BAAI/bge-small-en-v1.5 (384 dimensions, zero API cost)."""

from functools import lru_cache
from typing import Any

MODEL_NAME = "BAAI/bge-small-en-v1.5"
# bge models expect this instruction in front of short retrieval queries (not passages).
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def _model() -> Any:
    from sentence_transformers import SentenceTransformer
    from transformers.utils import logging as hf_logging

    # Offsets are computed over whole pages, longer than the model's 512-token limit;
    # that is intended (chunks are cut afterwards), so silence the length warning.
    hf_logging.set_verbosity_error()
    return SentenceTransformer(MODEL_NAME, device="cpu")


def token_offsets(text: str) -> list[tuple[int, int]]:
    """Character offsets of the model's tokens in `text` (special tokens excluded)."""
    encoded = _model().tokenizer(
        text, add_special_tokens=False, return_offsets_mapping=True, truncation=False
    )
    return [(int(a), int(b)) for a, b in encoded["offset_mapping"]]


def embed_passages(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    vectors = _model().encode(texts, batch_size=batch_size, normalize_embeddings=True)
    return [[float(x) for x in v] for v in vectors]


def embed_query(text: str) -> list[float]:
    vector = _model().encode([QUERY_PREFIX + text], normalize_embeddings=True)[0]
    return [float(x) for x in vector]
