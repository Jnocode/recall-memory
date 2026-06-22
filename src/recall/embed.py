# recall. — Embedding Layer
# Uses nomic-embed-text-v1.5 via LM Studio (port 1234)

import json
import urllib.request

_MODEL = "nomic-embed-text-v1.5"


def embed(text: str) -> list[float]:
    body = json.dumps({
        "model": _MODEL,
        "input": [text],
        "encoding_format": "float"
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:1234/v1/embeddings",
        data=body,
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())
    return data["data"][0]["embedding"]


def embed_batch(texts: list[str]) -> list[list[float]]:
    body = json.dumps({
        "model": _MODEL,
        "input": texts,
        "encoding_format": "float"
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:1234/v1/embeddings",
        data=body,
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    data["data"].sort(key=lambda x: x["index"])
    return [d["embedding"] for d in data["data"]]


def is_loaded() -> bool:
    try:
        req = urllib.request.Request("http://127.0.0.1:1234/v1/models")
        resp = urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False
