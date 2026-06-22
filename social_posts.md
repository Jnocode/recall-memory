## Reddit r/LocalLLaMA 貼文

### Title
Built a hybrid memory system for coding agents – 2.08x better recall than pure vector search

### Body

Spent the weekend building **recall.** – a lightweight memory layer for AI coding agents.

**The problem:** My coding agent kept asking me the same questions every session ("how do you deploy?", "what database do you use?"). Pure vector search can't connect "deploy" to "user prefers docker-compose" across different conversations.

**What it does:** Hybrid scoring – semantic similarity + recency + entity overlap – in a single SQLite database. No vector DB, no LLM rerank, no hypergraph nonsense.

**Result:** 2.08x recall improvement on multi-hop QA (20 questions, same embedding model).

```python
score = 0.5 × semantic + 0.3 × recency + 0.2 × entity_overlap
```

~400 lines of Python. Apache 2.0.

https://github.com/Jnocode/recall

Would love feedback from anyone running local agents!

---

## Twitter/X Thread

1/4
Built recall. – a memory layer for coding agents.
Hybrid scoring (semantic + recency + entity) beats pure vector by 2.08x.
No vector DB. No LLM rerank. ~400 lines.
github.com/Jnocode/recall

2/4
The problem: agents keep asking the same questions.
"how do you deploy?" → should know user prefers docker-compose
Pure vector can't connect those dots across sessions.

3/4
The fix:
score = 0.5 semantic + 0.3 recency + 0.2 entity overlap
All in SQLite. One function. No infra.

4/4
Key decisions (audited by 5 engineers):
• No LLM rerank (cold start makes it useless)
• No hypergraph (SQL JOIN is enough)
• SQLite first (zero-deployment)
Apache 2.0. github.com/Jnocode/recall

---

## HN 七天后重試

這週在 HN 參與討論，累積 karma。
下週再發 Show HN。
