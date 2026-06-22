"""Fair comparison: recall (no domain vocab) vs AIngram (extractor=local)"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime

# Get 1400 memories from recall's DB
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall_p0.db")
import sqlite3, urllib.request
conn = sqlite3.connect(db)
all_rows = conn.execute("SELECT rowid-1, content FROM memories").fetchall()
conn.close()

# Build ground truth: find memories that match certain topics
TOPICS = {
    "deploy": ["deploy", "docker", "compose", "kubernetes", "helm", "fargate"],
    "database": ["database", "postgresql", "asyncpg", "postgres", "redis", "cache"],
    "code_quality": ["type hints", "lint", "pytest", "coverage", "ruff"],
    "infrastructure": ["ec2", "ecs", "fargate", "vpc", "terraform", "cloudfront"],
    "podcast": ["podcast", "episode", "錄音", "節目"],
    "wiki": ["wiki", "知識庫", "meilisearch", "vitepress"],
    "antigravity": ["antigravity", "mcp", "gemini"],
    "cron": ["cron", "schedule", "定時"],
}
def topic_memory_ids():
    ids = set()
    for topic, keywords in TOPICS.items():
        for rowid, content in all_rows:
            if any(k.lower() in content.lower() for k in keywords):
                ids.add(rowid)
    return ids

# Create 50 eval questions
EVAL_Q = []
import random
random.seed(42)

# 10 multi-hop questions
multi_hop = [
    ("How to deploy containers from dev to production?", {"deploy"}),
    ("What database and caching solutions are configured?", {"database"}),
    ("What are the code quality and review standards?", {"code_quality"}),
    ("What infrastructure changes were planned?", {"infrastructure"}),
    ("What podcast episodes have been recorded?", {"podcast"}),
    ("How is the wiki server deployed?", {"wiki"}),
    ("What MCP tools are configured in antigravity?", {"antigravity"}),
    ("What scheduled jobs are running?", {"cron"}),
    ("How does the CI/CD pipeline work?", {"deploy", "code_quality"}),
    ("What monitoring and alerting is set up?", {"infrastructure"}),
]
for q, topics in multi_hop:
    truth = set()
    for rowid, content in all_rows:
        if any(k.lower() in content.lower() for k in 
               [kw for t in topics for kw in TOPICS[t]]):
            truth.add(rowid)
    EVAL_Q.append((q, truth))

# 40 single-hop questions from real conversation topics
extra_q = [
    "What is the wiki server URL?",
    "How to restart the wiki containers?",
    "What embedding model does recall use?",
    "What port does LM Studio run on?",
    "What is the NAS IP address?",
    "How to SSH into the NAS?",
    "What is the podcast name?",
    "What is SoulX used for?",
    "How are podcast episodes generated?",
    "What is the recommender system for?",
    "What git repos are active?",
    "What is the Hermes agent?",
    "How does the daily mail digest work?",
    "What cron jobs are scheduled?",
    "What is the Honcho memory system?",
    "How to query Honcho memories?",
    "What port does Honcho use?",
    "How to restart the wiki Docker containers?",
    "What is the Meilisearch search engine for?",
    "What OpenClaw tools exist?",
]
for q in extra_q:
    truth = set()
    keywords = q.lower().replace("?", "").split()
    for rowid, content in all_rows:
        if any(k in content.lower() for k in keywords):
            truth.add(rowid)
    if truth:
        EVAL_Q.append((q, truth))

print(f"Created {len(EVAL_Q)} evaluation questions")
print(f"Total memories: {len(all_rows)}")

# Test recall (no domain vocab)
from store import SQLiteStore
from retrieve import retrieve_relevant

recall_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall_p0.db")
r_store = SQLiteStore(recall_db, vec_dim=768)

print(f"\nTesting recall (no domain vocab)...")
r_results = []
t0 = time.time()
for q, truth in EVAL_Q[:50]:
    mems = r_store.get_all()
    # Pure vector only (simulate no domain vocab by not using expand_query)
    from embed import embed
    import numpy as np
    q_emb = embed(q)
    scored = []
    for m in mems:
        if m.embedding:
            a, b = np.array(q_emb), np.array(m.embedding)
            sim = float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b)+1e-10))
            scored.append((sim, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    top5_ids = {m.id for _, m in scored[:5]}
    recall_ids = {r[0] for r in all_rows if r[1] in [mm.content for mm in [m for _,m in scored[:5]]]}
    hit = recall_ids & truth
    r = len(hit)/len(truth) if truth else 0
    r_results.append(r)
recall_time = time.time() - t0
r_avg = sum(r_results)/len(r_results)
print(f"  Recall@5: {r_avg:.3f}")
print(f"  Time: {recall_time:.1f}s")

# Test AIngram (extractor=local)
from aingram import AIngramConfig, MemoryStore
ai_config = AIngramConfig(extractor_mode='local', embedding_dim=768)
ai_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_ai_fair.db")
if os.path.exists(ai_db): os.remove(ai_db)
ai_store = MemoryStore(ai_db, config=ai_config)

print(f"Importing {len(all_rows)} memories into AIngram with extractor=local...")
t0 = time.time()
for rowid, content in all_rows:
    ai_store.remember(content)
print(f"  Done in {time.time()-t0:.0f}s")

print(f"\nTesting AIngram (extractor=local)...")
ai_results = []
t0 = time.time()
for q, truth in EVAL_Q[:50]:
    results = ai_store.recall(q, limit=5)
    found = set()
    for r in results:
        c = r.entry.content
        if isinstance(c, str) and c.startswith("{"):
            c = json.loads(c).get("text", c)
        elif isinstance(c, dict):
            c = c.get("text", str(c))
        for rowid, content in all_rows:
            if content == c:
                found.add(rowid)
                break
    hit = found & truth
    r = len(hit)/len(truth) if truth else 0
    ai_results.append(r)
ai_time = time.time() - t0
ai_avg = sum(ai_results)/len(ai_results)
print(f"  Recall@5: {ai_avg:.3f}")
print(f"  Time: {ai_time:.1f}s")

print(f"\n{'='*55}")
print(f"  FAIR COMPARISON — {len(EVAL_Q[:50])} Questions")
print(f"{'='*55}")
print(f"  recall (pure vector only):  R@5={r_avg:.3f}")
print(f"  AIngram (extractor=local):  R@5={ai_avg:.3f}")
if r_avg > ai_avg:
    print(f"  recall leads by {(r_avg-ai_avg)/ai_avg*100:.1f}%")
elif ai_avg > r_avg:
    print(f"  AIngram leads by {(ai_avg-r_avg)/r_avg*100:.1f}%")
else:
    print(f"  Tie")
print(f"{'='*55}")

r_store.close()
ai_store.close()
if os.path.exists(ai_db): os.remove(ai_db)
