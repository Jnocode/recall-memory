# recall-sqlite v0.2.0 — 社群宣傳素材

---

## Twitter/X Thread (4 則)

**1/4**
I built a memory system for AI agents that actually forgets.
No LLM. No vector DB. Just SQLite.

recall-sqlite: hybrid retrieval (ANN + keywords + FTS5) with automatic tiered storage. Hot/warm/cold tiers so your agent doesn't drown in stale context.

↓

**2/4**
The problem: every memory system (Mem0, Honcho) accumulates forever. Old facts pollute retrieval. More tokens → worse results.

recall's approach: tiered storage. Frequently accessed memories stay in the ANN index (~500). Less-used ones drop to keyword-only. Unused ones go cold — zero compute.

↓

**3/4**
Numbers from 6 months of production data (1,469 real memories):
- ANN scan: -66% today, -99% at 50K memories
- Memory: fixed ~1.5MB vs linear growth to 150MB
- Query latency: ~80ms (same as before)
- Schema migration: automatic on pip install --upgrade

No cron. No config. No UI. Pure SQLite.

↓

**4/4**
If you run an AI agent (Claude Code, Codex, Hermes, etc.) and want it to stop repeating itself:

```bash
pip install recall-sqlite
```

Or check the architecture:
github.com/Jnocode/recall-memory

No API keys required. Just LM Studio for embeddings (or run CPU-only with keyword fallback).

---

## Show HN 貼文

**Title:** Show HN: recall-sqlite – AI agent memory with automatic forgetting, pure SQLite

**Body:**

I built a memory system that agents actually need: one that forgets.

After 6 months of real usage (1,469 memories), every competitive system (Mem0, Honcho) has zero forgetting mechanisms. Memories accumulate forever. Old facts pollute retrieval. Agents get slower and dumber over time.

recall-sqlite solves this with tiered storage:

```
Hot (~500):   ANN + keywords + FTS5  → full 3-path retrieval
Warm (~5000): keywords + FTS5 only   → 66-99% less ANN work
Cold (unlimited): not indexed        → zero compute, fill-gap only
```

Memories naturally promote/demote based on usage. Cold tier gets sampled every N queries for keyword overlap — if relevant, it comes back. No cron, no UI, no configuration.

**Why SQLite?**
- Zero deployment. No vector database. No Docker.
- sqlite-vec for ANN, FTS5 for full-text, SQL JOIN for keyword expansion.
- Perfect for local/edge: runs on a Raspberry Pi.

**Numbers:**
- 1,469 real production memories
- ANN scan reduced 66% now → 99% at 50K
- Memory fixed at ~1.5MB vs linear growth
- ~80ms query latency

```bash
pip install recall-sqlite
```

GitHub: github.com/Jnocode/recall-memory
Docs in README. Schema migration automatic.

No LLM at query time. No API keys. Just pip install.

---

## Reddit r/LocalLLaMA 貼文

**Title:** I built a memory system for local LLMs that forgets — no vector DB, no API, just SQLite

**Body:**

If you run local LLMs (Llama, Qwen, etc.) and want agent memory, you've probably looked at Mem0 or Honcho. Both work, but both accumulate memories forever — no built-in forgetting.

I've been running my own system (called recall-sqlite) for 6 months. 1,469 real memories from daily coding sessions. Today I shipped v0.2.0 with tiered storage.

**How it works:**
- Hot memories (frequently accessed) stay in the ANN index
- Warm memories drop vectors but keep keyword/FTS5
- Cold memories don't participate in normal queries
- Promotion/demotion is automatic based on usage
- Cold memories get sampled every 20 queries — if keywords match, they come back

**Why this matters for local LLMs:**
- No API calls at query time (zero cost)
- No vector DB (just SQLite)
- Memory stays at ~1.5MB even with 50K memories
- Graceful degradation when LM Studio is offline (keyword + FTS5 fallback)

```bash
pip install recall-sqlite

recall add "User prefers docker-compose for local dev"
recall query "How to deploy?"
```

GitHub: github.com/Jnocode/recall-memory

Curious if anyone else has hit the "memory accumulates forever" problem with their agents. What's your current setup?
