# recall. 🧠

**Better contextual retrieval for AI agents.**
Pure vector search + domain vocabulary expansion. Nomic Embed (768-dim) via LM Studio.

```python
from recall import retrieve_relevant
store.add("User prefers docker-compose over Dockerfile")
results = retrieve_relevant("How should I deploy?", store)
```

## Quick start

Requires [LM Studio](https://lmstudio.ai) with `nomic-embed-text-v1.5` loaded.

```bash
pip install sentence-transformers numpy
python3 cli.py add "User prefers docker-compose for local dev"
python3 cli.py query "How to deploy?"
```

## How it works

1. **Domain vocab expansion** — bridges semantic gaps ("deploy" → "docker docker-compose")
2. **Pure vector search** — cosine similarity via sqlite-vec ANN
3. **Safety net** — 20-entry domain vocabulary for deployment/infra terms

No LLM calls at query time. No vector database. Just SQLite + Nomic embeddings.

## Status

MVP — working prototype. 20-question eval: recall@5 = 0.402.

- [x] P0: 300-line prototype
- [x] P0.5: store + retrieve + embed + cli modules
- [x] P1: sqlite-vec ANN, Nomic embed, domain vocab
- [ ] Next: LLM re-rank top-20 for better precision

## CLI

```bash
recall add "content"          # Store a memory
recall query "question"       # Retrieve relevant memories
recall pure "question"        # Pure vector (no domain vocab)
recall stats                  # Store statistics
recall delete <id>            # Remove a memory
```

## Design decisions

| Decision | Rationale |
|----------|-----------|
| Nomic embed | 768-dim, better than MiniLM, served via LM Studio |
| Domain vocab | 20-entry safety net for deployment/infra terms |
| No LLM re-rank | Cold start makes rerank useless; future work |
| SQLite first | Zero-deployment; PostgreSQL later if needed |
| sqlite-vec | ANN cosine search, 768-dim |

## Why not hybrid scoring?

Entity extraction was ~43% noisy. Recency punished cross-session memories. Pure vector + domain vocab expansion proved simpler and equally effective. See [eval report](./eval_report.md).

## License

Apache 2.0
