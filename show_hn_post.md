# Show HN: 貼文草稿

## 標題
Show HN: Recall – Hybrid scoring memory for coding agents (2.08x better than pure vector)

## 正文

I got tired of my coding agent asking me the same questions every session (“how do you deploy?”, “what database do you use?”).

**Problem**: Pure vector search can't connect "deploy" to "user prefers docker-compose" if they're in different conversations. It needs multi-hop reasoning.

**Solution**: Hybrid scoring – semantic similarity + recency + entity overlap. All in SQLite. No vector DB, no LLM rerank, no hypergraph.

**Result**: 2.08x recall improvement on multi-hop QA. 400 lines of Python.

```bash
pip install sentence-transformers typer
python3 cli.py add "User prefers docker-compose for local dev"
python3 cli.py query "How should I deploy?"
```

**Key decisions** (audited by 5 perspectives – Feynman, Karpathy, Sutskever, Musk, Zhang):
- No LLM rerank (cold start makes it useless)
- No hypergraph (SQL JOIN is enough)
- SQLite first (PostgreSQL later)
- Entity extraction by regex (LLM extraction is P2)

**What's next**: sqlite-vec integration, more extraction patterns, real coding agent integration.

Would love feedback from anyone building AI agents!

GitHub: [link after repo created]
