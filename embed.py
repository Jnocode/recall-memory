# recall. — Embedding Layer
# Cached sentence-transformers wrapper with lazy loading

from typing import Optional
from sentence_transformers import SentenceTransformer

_model: Optional[SentenceTransformer] = None
_MODEL_NAME = "all-MiniLM-L6-v2"
_LOADED: bool = False


def get_embedder() -> SentenceTransformer:
    global _model, _LOADED
    if _model is None:
        import sys
        if not _LOADED:
            print("⏳ Loading embedding model (~10s first time)...", file=sys.stderr)
            sys.stderr.flush()
            _LOADED = True
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed(text: str) -> list[float]:
    return get_embedder().encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    return get_embedder().encode(texts, normalize_embeddings=True).tolist()


def is_loaded() -> bool:
    return _model is not None
