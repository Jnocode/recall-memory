"""
Deep dive: nomic-embed pure vector — detailed rank analysis + entity hybrid on nomic.
"""
import sys, re, json, numpy as np
import urllib.request

scenarios = [
    {"name":"1. Deploy method","session_a":"User prefers docker-compose over Dockerfile","session_b":"How should I deploy the app?","should_find":"docker-compose"},
    {"name":"2. Database setup","session_a":"User uses PostgreSQL with asyncpg for production","session_b":"What database should I use?","should_find":"PostgreSQL"},
    {"name":"3. API structure","session_a":"FastAPI project structure: routers/services/models/schemas/","session_b":"Where should I put the new API endpoint?","should_find":"routers"},
    {"name":"4. Code quality","session_a":"All PRs must have complete type hints before merging","session_b":"What are the code review rules?","should_find":"type hints"},
    {"name":"5. Infrastructure","session_a":"We migrated from EC2 to ECS Fargate for cost savings","session_b":"What platform are we using for hosting?","should_find":"ECS Fargate"},
]

def cos_sim(a, b):
    return float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b)+1e-10))

def nomic_embed(text: str) -> list[float]:
    data = json.dumps({"model":"text-embedding-nomic-embed-text-v1.5","input":text}).encode()
    req = urllib.request.Request("http://localhost:1234/v1/embeddings", data=data, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["data"][0]["embedding"]

# Entity extraction (same as retrieve.py)
STOP_WORDS = {"the","a","an","is","are","was","were","it","this","that","to","of","in","for","on","with","at","by","from","as","and","or","but","not","be","been","being","have","has","had","do","does","did","will","would","can","could","may","might","shall","should","about","into","through","during","before","after","above","below","between","out","off","over","under","again","further","then","once","here","there","when","where","why","how","all","each","every","both","few","more","most","other","some","such","no","nor","only","own","same","so","than","too","very","just","also","now","get","use","set","put","make","take","come","go","see","know","think","want","give","tell","ask","show","try","leave","call","keep","let","begin","seem","help","turn","的","是","了","在","有","我","不","這","那","也","和","就","你","都","要","會","可","以","為","上","what","which","who","whom","whose","where","why","how"}
def extract_entities(text):
    text_lower = text.lower()
    multi_word = re.findall(r'\b[a-zA-Z][a-zA-Z0-9]+[-_][a-zA-Z0-9][-a-zA-Z0-9]*\b', text)
    capitalized = re.findall(r'\b[A-Z][a-zA-Z0-9+#_-]{2,}\b', text)
    versions = re.findall(r'\b[A-Za-z]+\s*\d+\.\d+[\w.]*\b', text)
    camel_case = re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z0-9]*\b', text)
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text_lower)
    words = [w for w in words if w not in STOP_WORDS]
    all_terms = set()
    for term in multi_word + capitalized + versions + camel_case:
        t = term.lower().rstrip('s')
        if t not in STOP_WORDS: all_terms.add(t)
    for w in words: all_terms.add(w)
    return all_terms

def entity_overlap_score(q_ents, m_ents):
    if not q_ents or not m_ents: return 0.0
    intersection = q_ents & m_ents
    return len(intersection) / max(len(q_ents), len(m_ents))

def run_test(name, scorer_fn):
    memories = [{"content":s["session_a"], "embedding":nomic_embed(s["session_a"]), "entities":extract_entities(s["session_a"])} for s in scenarios]
    correct = 0
    correct_top1 = 0
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    for i, s in enumerate(scenarios):
        q = s["session_b"]
        q_emb = nomic_embed(q)
        q_ents = extract_entities(q)
        scores = [(scorer_fn(q, q_emb, q_ents, mi, mem), mem["content"]) for mi, mem in enumerate(memories)]
        scores.sort(key=lambda x: x[0], reverse=True)
        ranked = [c for _, c in scores]
        found_top3 = any(s["should_find"].lower() in t.lower() for t in ranked[:3])
        found_top1 = s["should_find"].lower() in ranked[0].lower()
        if found_top3: correct += 1
        if found_top1: correct_top1 += 1
        status = "✅" if found_top3 else "❌"
        t1_mark = " [TOP1]" if found_top1 else ""
        print(f"  {status} {s['name']}{t1_mark}")
        for j, t in enumerate(ranked[:5], 1):
            mark = " ←" if s["should_find"].lower() in t.lower() else ""
            print(f"       {j}. [{scores[j-1][0]:.4f}] {t[:55]}{mark}")
    print(f"  Result: {correct}/{len(scenarios)} (top-3)  {correct_top1}/{len(scenarios)} (top-1)")
    return correct, correct_top1

# Baseline scorer
def baseline_scorer(q, qe, qen, mi, mem):
    return 0.6 * cos_sim(qe, mem["embedding"]) + 0.4 * entity_overlap_score(qen, mem["entities"])

# Pure vector scorer
def pure_scorer(q, qe, qen, mi, mem):
    return cos_sim(qe, mem["embedding"])

print(f"\n{'='*60}")
print(f"  DEEP DIVE: nomic-embed + entity hybrid")
print(f"{'='*60}")

_ = nomic_embed("warmup")

r1, t1 = run_test("nomic-embed PURE VECTOR (semantic=1.0)", pure_scorer)
r2, t2 = run_test("nomic-embed + HYBRID (sem=0.6 + ent=0.4)", baseline_scorer)

print(f"\n{'='*60}")
print(f"  COMPARISON (top-1 counts)")
print(f"{'='*60}")
print(f"  Pure vector top-3: {r1}/5  top-1: {t1}/5")
print(f"  Hybrid top-3:      {r2}/5  top-1: {t2}/5")
print(f"{'='*60}")

# Key insight: semantic similarity spread
print(f"\n{'='*60}")
print(f"  KEY METRIC: Semantic Spread (separation between correct vs wrong)")
print(f"{'='*60}")
memories = [{"content":s["session_a"], "embedding":nomic_embed(s["session_a"])} for s in scenarios]
print(f"  {'Scenario':30s} {'correct_sim':>12s} {'avg_wrong_sim':>13s} {'margin':>8s}")
print(f"  {'─'*63}")
for i, s in enumerate(scenarios):
    q_emb = nomic_embed(s["session_b"])
    correct_sim = cos_sim(q_emb, memories[i]["embedding"])
    wrong_sims = [cos_sim(q_emb, memories[j]["embedding"]) for j in range(5) if j != i]
    avg_wrong = sum(wrong_sims) / len(wrong_sims)
    print(f"  {s['name']:30s} {correct_sim:>12.4f} {avg_wrong:>13.4f} {correct_sim-avg_wrong:>+8.4f}")
