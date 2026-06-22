"""
Ablation test: Does nomic-embed automatically bridge the "deploy" semantic gap?
Compares pure vector search using MiniLM (384-dim) vs nomic-embed (768-dim)
on the 5 real-world scenarios, WITHOUT any domain vocab or entity hybrid.
"""
import sys, re, json, numpy as np
import urllib.request

# ── 5 scenarios (same as _experiments.py) ──
scenarios = [
    {"name":"1. Deploy method","session_a":"User prefers docker-compose over Dockerfile","session_b":"How should I deploy the app?","should_find":"docker-compose"},
    {"name":"2. Database setup","session_a":"User uses PostgreSQL with asyncpg for production","session_b":"What database should I use?","should_find":"PostgreSQL"},
    {"name":"3. API structure","session_a":"FastAPI project structure: routers/services/models/schemas/","session_b":"Where should I put the new API endpoint?","should_find":"routers"},
    {"name":"4. Code quality","session_a":"All PRs must have complete type hints before merging","session_b":"What are the code review rules?","should_find":"type hints"},
    {"name":"5. Infrastructure","session_a":"We migrated from EC2 to ECS Fargate for cost savings","session_b":"What platform are we using for hosting?","should_find":"ECS Fargate"},
]

def cos_sim(a, b):
    return float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b)+1e-10))

# ── LM Studio API (nomic-embed, 768-dim) ──
def nomic_embed(text: str) -> list[float]:
    data = json.dumps({"model":"text-embedding-nomic-embed-text-v1.5","input":text}).encode()
    req = urllib.request.Request(
        "http://localhost:1234/v1/embeddings",
        data=data,
        headers={"Content-Type":"application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["data"][0]["embedding"]

# ── Local MiniLM (384-dim) ──
from sentence_transformers import SentenceTransformer
minilm_model = SentenceTransformer("all-MiniLM-L6-v2")
def minilm_embed(text: str) -> list[float]:
    return minilm_model.encode(text, normalize_embeddings=True).tolist()

def run_test(name, embed_fn):
    """Pure vector search (no hybrid, no domain vocab)."""
    # Encode all memories
    memories = [embed_fn(s["session_a"]) for s in scenarios]
    correct = 0
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    for i, s in enumerate(scenarios):
        q_emb = embed_fn(s["session_b"])
        scores = [cos_sim(q_emb, mem_emb) for mem_emb in memories]
        ranked = sorted(range(len(scores)), key=lambda j: scores[j], reverse=True)
        top3 = [scenarios[ri]["session_a"] for ri in ranked[:3]]
        found = any(s["should_find"].lower() in t.lower() for t in top3)
        if found: correct += 1
        status = "✅" if found else "❌"
        print(f"  {status} {s['name']}")
        for j, t in enumerate(top3, 1):
            mark = " ←" if s["should_find"].lower() in t.lower() else ""
            print(f"       {j}. [{scores[ranked[j-1]]:.4f}] {t[:55]}{mark}")
    print(f"  Result: {correct}/{len(scenarios)}")
    return correct

print("="*60)
print("  ABLATION: Pure Vector Search (no hybrid, no domain vocab)")
print("="*60)

# Warm up
print("\n  Warming nomic-embed...")
_ = nomic_embed("warmup")
print("  Done.")

miniLM_score = run_test("all-MiniLM-L6-v2 (384-dim) — PURE VECTOR", minilm_embed)
nomic_score  = run_test("nomic-embed-text-v1.5 (768-dim) — PURE VECTOR", nomic_embed)

print(f"\n{'='*60}")
print(f"  COMPARISON")
print(f"{'='*60}")
print(f"  MiniLM pure vector:  {miniLM_score}/5")
print(f"  Nomic pure vector:   {nomic_score}/5")
print(f"{'='*60}")
if nomic_score > miniLM_score:
    print("  ✅ nomic-embed bridges the semantic gap automatically")
    print("  ✅ Domain vocab is redundant with better embeddings")
elif nomic_score == 5 and miniLM_score < 5:
    print("  ✅ nomic-embed bridges the semantic gap automatically")
    print("  ✅ Domain vocab is redundant with better embeddings")
else:
    print("  ❌ nomic-embed does NOT automatically bridge the gap")
    print("  → Problem persists at the embedding level")
