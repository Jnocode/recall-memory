# recall. — Embedding Layer
# Uses nomic-embed-text-v1.5 via LM Studio (port 1234)
# Falls back gracefully when LM Studio is unavailable.

import json
import urllib.request
import os

EMBED_PORT = int(os.environ.get("EMBED_PORT", "1234"))
"""Port where LM Studio serves the embedding model.
Override with EMBED_PORT env var."""

EMBED_BASE_URL = os.environ.get(
    "EMBED_BASE_URL",
    f"http://127.0.0.1:{EMBED_PORT}"
)
"""Base URL for OpenAI-compatible embedding API.
Override with EMBED_BASE_URL env var (e.g. http://host.docker.internal:1234)."""

EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text-v1.5")
EMBED_URL = f"{EMBED_BASE_URL}/v1/embeddings"
EMBED_TIMEOUT = 15  # seconds per request

_EMBEDDING_CACHE: dict[str, list[float]] = {}
"""In-memory cache: query text → embedding vector.
Cleared on process restart."""


def embed(text: str) -> list[float] | None:
    """Convert text to 768-dim embedding vector.

    Returns None if LM Studio is unreachable (caller should degrade gracefully).
    """
    if text in _EMBEDDING_CACHE:
        return _EMBEDDING_CACHE[text]

    try:
        body = json.dumps({
            "model": EMBED_MODEL,
            "input": [text],
            "encoding_format": "float"
        }).encode()
        req = urllib.request.Request(EMBED_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=EMBED_TIMEOUT)
        data = json.loads(resp.read())
        vec = data["data"][0]["embedding"]
        _EMBEDDING_CACHE[text] = vec
        return vec
    except Exception:
        return None


def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Convert multiple texts to embedding vectors in one API call.

    Returns a list of vectors (or None for texts that failed), in the same
    order as the input. Skips texts already in cache.
    """
    result = [None] * len(texts)
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    for i, t in enumerate(texts):
        if t in _EMBEDDING_CACHE:
            result[i] = _EMBEDDING_CACHE[t]
        else:
            uncached_indices.append(i)
            uncached_texts.append(t)

    if not uncached_texts:
        return result

    try:
        body = json.dumps({
            "model": EMBED_MODEL,
            "input": uncached_texts,
            "encoding_format": "float"
        }).encode()
        req = urllib.request.Request(EMBED_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=EMBED_TIMEOUT * 2)
        data = json.loads(resp.read())
        for entry in data["data"]:
            idx = entry["index"]
            orig_idx = uncached_indices[idx]
            vec = entry["embedding"]
            result[orig_idx] = vec
            _EMBEDDING_CACHE[uncached_texts[idx]] = vec
    except Exception:
        pass

    return result


def is_loaded() -> bool:
    """Check if LM Studio is reachable."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{EMBED_PORT}/v1/models")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False
