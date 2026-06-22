# recall. — Memory retrieval for AI agents

Pure vector search with domain vocabulary expansion. Nomic Embed (768-dim).

- No LLM calls at query time
- No vector database (just SQLite + sqlite-vec)
- Domain vocab safety net for deployment/infra terms

20-question eval: recall@5 = 0.402.

github.com/Jnocode/recall
