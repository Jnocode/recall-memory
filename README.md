# recall. 🧠

**Better contextual retrieval for AI agents.**  
Hybrid scoring (semantic + recency + entity) beats pure vector search by **2.08x**.

Your coding agent won't repeat questions it already knows the answer to.

```python
# One function, ~300 lines of core logic
from recall import retrieve_relevant

# Agent remembers past conversations
store.add("User prefers docker-compose over Dockerfile for local dev")

# Later query: hybrid scoring finds the relevant memory
results = retrieve_relevant("How should I deploy?", store)
# → "User prefers docker-compose over Dockerfile for local dev"
```

## Why not just use vector search?

Multi-hop questions need more than semantic similarity:

| Query | Pure Vector | recall. (Hybrid) |
|-------|-------------|------------------|
| "Which deployment method?" | ❌ wrong context | ✅ finds user's docker-compose preference |
| "What database issues?" | ❌ misses old incident | ✅ finds 3-month-old connection pool bug |
| "User tool preferences?" | ❌ misses scattered facts | ✅ aggregates all preferences |

**2.08x recall improvement** on 20 multi-hop QA tests. [(eval report)](./eval_report.md)

## Quick start

```bash
pip install sentence-transformers typer

# Add a memory
python3 cli.py add "User prefers docker-compose for local dev"

# Query with hybrid scoring
python3 cli.py query "How to deploy?"
```

## How it works

Three scoring signals, combined:

```
score = 0.5 × semantic_similarity 
      + 0.3 × recency 
      + 0.2 × entity_overlap
```

| Signal | Weight | What it captures |
|--------|--------|-----------------|
| Semantic | 0.5 | Meaning similarity via embedding |
| Recency | 0.3 | Time decay (half-life: 14 days) |
| Entity | 0.2 | Shared keywords & proper nouns |

No LLM calls at query time. No vector database. Just SQLite + sentence-transformers.

## CLI

```bash
recall add "content"          # Store a memory
recall query "question"       # Hybrid retrieval
recall pure "question"        # Pure vector baseline
recall stats                  # Store statistics
recall delete <id>            # Remove a memory
```

## Status

MVP — working prototype, validated hypothesis. ~400 lines of Python.

- [x] P0: Hybrid > pure vector (2.08x) ✅
- [x] P0.5: store.py + retrieve.py + cli.py
- [ ] P1: sqlite-vec, improved extraction, eval pipeline
- [ ] P2: Real coding agent integration, release

## Design decisions (audited)

| Decision | Rationale |
|----------|-----------|
| No LLM re-rank | Cold start makes rerank useless; embedding similarity covers 80% |
| No fork Honcho | Honcho is PostgreSQL server; recall needs SQLite CLI |
| No hypergraph | SQL JOIN is sufficient for multi-hop |
| SQLite first | Zero-deployment; PostgreSQL later if needed |
| Entity by regex | Rules cover P0 needs; LLM extraction is P2+ |

## License

Apache 2.0
