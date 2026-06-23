# recall.sqlite — Better contextual retrieval for AI agents.
# SQLite backend for memory persistence with keyword index + tiered storage

import sqlite3
import json
import hashlib
import re
import os
import random
import numpy as np
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional


# ─── Constants ──────────────────────────────────────────────────────────────────
HOT_CAPACITY = 500       # max memories with ANN vectors
WARM_CAPACITY = 5000     # max memories with keyword/FTS5 only
PROMOTION_THRESHOLD = 3  # access_count needed for warm→hot
SAMPLING_INTERVAL = 20   # every N queries, check cold tier
COOLDOWN_HOURS = 24      # min hours after demotion before re-promotion
HYSTERESIS_BAND = 0.2    # demotion threshold = promotion threshold - band


@dataclass
class Memory:
    content: str
    id: str = ""
    entities: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    embedding: Optional[list[float]] = None
    access_count: int = 0
    session_id: str = ""
    tag: str = "episodic"
    tier: str = "hot"  # hot / warm / cold
    last_accessed_at: Optional[datetime] = None
    last_demoted_at: Optional[datetime] = None


# ─── Keyword extraction ───────────────────────────────────────────────────────

_STOP = {"the","a","an","is","are","was","were","it","this","that","to","of",
         "in","for","on","with","at","by","from","as","and","or","but","not",
         "be","been","being","have","has","had","do","does","did","will",
         "would","can","could","may","might","shall","should","about","into",
         "through","during","before","after","above","below","between","out",
         "off","over","under","again","further","then","once","here","there",
         "when","where","why","how","all","each","every","both","few","more",
         "most","other","some","such","no","nor","only","own","same","so",
         "than","too","very","just","also","now","get","use","set","put","make",
         "take","come","go","see","know","think","want","give","tell","ask",
         "show","try","leave","call","keep","let","begin","seem","help","turn",
         "what","which","who","whom","whose","user","prefers","must","over"}


def extract_keywords(text: str) -> list[str]:
    """Extract technical keywords from text. No LLM needed."""
    words = set()
    words.update(re.findall(r'\b[a-zA-Z][a-zA-Z0-9]+[-_][a-zA-Z0-9][-a-zA-Z0-9]*\b', text))
    words.update(re.findall(r'\b[A-Z][a-zA-Z0-9+#_-]{2,}\b', text))
    words.update(re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z0-9]*\b', text))
    words.update(re.findall(r'\b[A-Za-z]+\s*\d+\.\d+[\w.]*\b', text))
    for w in re.findall(r'\b[a-zA-Z]{4,}\b', text.lower()):
        if w not in _STOP:
            words.add(w)
    return sorted(words)


# ─── SQLite Store ──────────────────────────────────────────────────────────────

class SQLiteStore:
    def __init__(self, db_path: str, vec_dim: int = 768):
        self.db_path = db_path
        self.vec_dim = vec_dim
        self._query_count = 0  # for lazy sampling trigger
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.enable_load_extension(True)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            import sqlite_vec
            sqlite_vec.load(conn)
            self.vec_available = True
        except Exception:
            self.vec_available = False

        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                entities TEXT DEFAULT '[]',
                timestamp TEXT NOT NULL,
                embedding BLOB,
                access_count INTEGER DEFAULT 0,
                session_id TEXT DEFAULT '',
                tag TEXT DEFAULT 'episodic',
                tier TEXT DEFAULT 'hot',
                last_accessed_at TEXT,
                last_demoted_at TEXT
            )
        """)

        # Migration: add tier columns for existing databases
        for col, default in [("tier", "'hot'"), ("last_accessed_at", "NULL"),
                              ("last_demoted_at", "NULL")]:
            try:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {col} TEXT DEFAULT {default}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # Set default tier for existing rows that have NULL
        conn.execute("UPDATE memories SET tier='hot' WHERE tier IS NULL")

        conn.commit()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                keyword TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                PRIMARY KEY (keyword, memory_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kw ON keywords(keyword)")

        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, id UNINDEXED, tokenize='porter unicode61'
                )
            """)
        except Exception:
            pass

        if self.vec_available:
            try:
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                        id TEXT PRIMARY KEY,
                        embedding float[{self.vec_dim}] distance_metric=cosine
                    )
                """)
            except Exception:
                self.vec_available = False

        for idx in ["idx_ts ON memories(timestamp)",
                     "idx_tag ON memories(tag)",
                     "idx_session ON memories(session_id)",
                     "idx_tier ON memories(tier)"]:
            try:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {idx}")
            except Exception:
                pass
        conn.close()

    # ─── CRUD ──────────────────────────────────────────────────────────────────

    def add(self, memory: Memory) -> str:
        if not memory.id:
            raw = memory.content + str(memory.timestamp)
            memory.id = hashlib.md5(raw.encode()).hexdigest()[:12]
        conn = sqlite3.connect(self.db_path)
        conn.enable_load_extension(True)
        if self.vec_available:
            import sqlite_vec
            sqlite_vec.load(conn)
        try:
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (id, content, entities, timestamp, embedding, access_count,
                    session_id, tag, tier, last_accessed_at, last_demoted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (memory.id, memory.content, json.dumps(memory.entities),
                 memory.timestamp.isoformat(), json.dumps(memory.embedding),
                 memory.access_count, memory.session_id, memory.tag,
                 memory.tier,
                 memory.last_accessed_at.isoformat() if memory.last_accessed_at else None,
                 memory.last_demoted_at.isoformat() if memory.last_demoted_at else None)
            )
            # Vec index: only for hot tier
            if memory.tier == "hot":
                self._insert_vec_embedding(memory.id, memory.embedding)
            # Keyword index
            for kw in extract_keywords(memory.content):
                conn.execute("INSERT OR REPLACE INTO keywords VALUES (?,?)",
                             (kw.lower(), memory.id))
            try:
                conn.execute("INSERT OR REPLACE INTO memories_fts VALUES (?, ?)",
                             (memory.content, memory.id))
            except Exception:
                pass
            conn.commit()
        finally:
            conn.close()
        # Check capacity after insert
        self._gc_if_needed()
        return memory.id

    def get(self, memory_id: str) -> Optional[Memory]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?",
                               (memory_id,)).fetchone()
        return self._row_to_mem(row) if row else None

    def get_all(self, limit: int = 1000) -> list[Memory]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY timestamp DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._row_to_mem(r) for r in rows]

    def get_by_session(self, session_id: str) -> list[Memory]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE session_id=? ORDER BY timestamp DESC",
                (session_id,)).fetchall()
        return [self._row_to_mem(r) for r in rows]

    def get_by_tag(self, tag: str, limit: int = 100) -> list[Memory]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE tag=? ORDER BY timestamp DESC LIMIT ?",
                (tag, limit)).fetchall()
        return [self._row_to_mem(r) for r in rows]

    def update(self, memory_id: str, **kwargs) -> bool:
        allowed = {"content", "entities", "embedding", "access_count",
                   "tag", "tier", "last_accessed_at", "last_demoted_at"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values())
        if "entities" in updates:
            idx = list(updates.keys()).index("entities")
            vals[idx] = json.dumps(updates["entities"])
        if "embedding" in updates:
            idx = list(updates.keys()).index("embedding")
            vals[idx] = json.dumps(updates["embedding"])
        if "last_accessed_at" in updates and updates["last_accessed_at"]:
            idx = list(updates.keys()).index("last_accessed_at")
            vals[idx] = updates["last_accessed_at"].isoformat()
        if "last_demoted_at" in updates and updates["last_demoted_at"]:
            idx = list(updates.keys()).index("last_demoted_at")
            vals[idx] = updates["last_demoted_at"].isoformat()
        vals.append(memory_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE memories SET {set_clause} WHERE id=?", vals)
        conn.commit()
        return True

    def delete(self, memory_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.enable_load_extension(True)
            if self.vec_available:
                import sqlite_vec
                sqlite_vec.load(conn)
            conn.execute("DELETE FROM keywords WHERE memory_id=?", (memory_id,))
            try:
                conn.execute("DELETE FROM memories_fts WHERE id=?", (memory_id,))
            except Exception:
                pass
            if self.vec_available:
                conn.execute("DELETE FROM vec_embeddings WHERE id=?", (memory_id,))
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return cur.rowcount > 0

    def count(self, tag: Optional[str] = None, tier: Optional[str] = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            q = "SELECT COUNT(*) FROM memories WHERE 1=1"
            params = []
            if tag:
                q += " AND tag=?"
                params.append(tag)
            if tier:
                q += " AND tier=?"
                params.append(tier)
            return conn.execute(q, params).fetchone()[0]

    def clear(self):
        conn = sqlite3.connect(self.db_path)
        conn.enable_load_extension(True)
        if self.vec_available:
            import sqlite_vec
            sqlite_vec.load(conn)
        try:
            conn.execute("DELETE FROM keywords")
            conn.execute("DELETE FROM memories")
            try:
                conn.execute("DELETE FROM memories_fts")
            except Exception:
                pass
            if self.vec_available:
                conn.execute("DELETE FROM vec_embeddings")
            conn.commit()
        finally:
            conn.close()

    # ─── Tier Management ───────────────────────────────────────────────────────

    def increment_access(self, memory_id: str):
        """Increment access_count and update last_accessed_at."""
        now = datetime.now(timezone.utc)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE memories SET access_count=access_count+1,
                   last_accessed_at=? WHERE id=?""",
                (now.isoformat(), memory_id))
            conn.commit()

    def count_tier(self, tier: str) -> int:
        return self.count(tier=tier)

    def promote(self, memory_id: str, target_tier: str = "warm") -> bool:
        """Move a memory up one tier (cold→warm or warm→hot)."""
        mem = self.get(memory_id)
        if not mem:
            return False
        current = mem.tier
        if current == target_tier:
            return True  # already there

        if target_tier == "hot":
            # Need to compute embedding if missing
            from .embed import embed  # lazy import to avoid circular
            if not mem.embedding:
                emb = embed(mem.content)
                if emb is None:
                    return False  # LM Studio unavailable, stay in warm
                self.update(memory_id, embedding=emb, tier="hot")
            else:
                self.update(memory_id, tier="hot")

            # Add vec_embedding
            mem = self.get(memory_id)
            self._insert_vec_embedding(memory_id, mem.embedding if mem else None)
        elif target_tier == "warm":
            # Remove vec_embedding when demoting from hot
            if current == "hot":
                self._remove_vec_embedding(memory_id)
                self.update(memory_id, tier="warm")
            else:
                self.update(memory_id, tier="warm")
        else:
            self.update(memory_id, tier=target_tier)
        return True

    def demote(self, memory_id: str, target_tier: str = "cold") -> bool:
        """Move a memory down one or more tiers."""
        mem = self.get(memory_id)
        if not mem:
            return False
        if target_tier == "cold":
            # Remove vec_embedding if coming from hot
            if mem.tier == "hot":
                self._remove_vec_embedding(memory_id)
            self.update(memory_id, tier="cold",
                        last_demoted_at=datetime.now(timezone.utc))
        elif target_tier == "warm":
            if mem.tier == "hot":
                self._remove_vec_embedding(memory_id)
            self.update(memory_id, tier="warm",
                        last_demoted_at=datetime.now(timezone.utc))
        else:
            self.update(memory_id, tier=target_tier,
                        last_demoted_at=datetime.now(timezone.utc))
        return True

    def _remove_vec_embedding(self, memory_id: str):
        conn = sqlite3.connect(self.db_path)
        conn.enable_load_extension(True)
        if self.vec_available:
            import sqlite_vec
            sqlite_vec.load(conn)
        try:
            conn.execute("DELETE FROM vec_embeddings WHERE id=?", (memory_id,))
            conn.commit()
        finally:
            conn.close()

    def _insert_vec_embedding(self, memory_id: str, embedding: list[float]):
        """Insert or replace a vector embedding in the vec_embeddings table."""
        if not self.vec_available or not embedding:
            return
        conn = sqlite3.connect(self.db_path)
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        try:
            vec_bytes = np.array(embedding, dtype=np.float32).tobytes()
            conn.execute(
                "INSERT OR REPLACE INTO vec_embeddings(id, embedding) VALUES (?, ?)",
                (memory_id, vec_bytes))
            conn.commit()
        finally:
            conn.close()

    def replenish_hot(self):
        """Promote top warm memories to fill hot tier vacancies."""
        hot_count = self.count_tier("hot")
        if hot_count >= HOT_CAPACITY:
            return 0
        vacancy = HOT_CAPACITY - hot_count
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT id, last_demoted_at FROM memories WHERE tier='warm'
               AND access_count >= ?
               ORDER BY access_count DESC, timestamp DESC LIMIT ?""",
            (PROMOTION_THRESHOLD, vacancy)).fetchall()
        conn.close()
        now = datetime.now(timezone.utc)
        promoted = 0
        for row in rows:
            mid, ld_str = row
            # Skip if in cooldown
            if ld_str:
                ld = datetime.fromisoformat(ld_str)
                if (now - ld).total_seconds() < COOLDOWN_HOURS * 3600:
                    continue
            if self.promote(mid, "hot"):
                promoted += 1
        return promoted

    # ─── Garbage Collection ────────────────────────────────────────────────────

    def _gc_if_needed(self):
        """Check DB size and evict if over threshold."""
        size_mb = self._db_size_mb()
        if size_mb < 80:
            return 0  # no need
        return self.evict(dry_run=False)

    def evict(self, dry_run: bool = False) -> dict:
        """Evict low-score memories when DB is over capacity."""
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc)
        size_before = self._db_size_mb()

        # Score each warm/cold memory: (access_count + 1) * decay factor
        rows = conn.execute(
            """SELECT id, access_count, timestamp, tier, last_accessed_at
               FROM memories WHERE tier IN ('warm', 'cold')
               ORDER BY access_count ASC, timestamp ASC LIMIT 200"""
        ).fetchall()
        conn.close()

        candidates = []
        for mid, ac, ts_str, tier, la_str in rows:
            days_since = (now - datetime.fromisoformat(ts_str)).days
            if days_since < 30 and tier == "warm":
                continue  # new warm memories are protected
            score = (ac + 1) * (0.7 ** (days_since / 30))
            candidates.append((score, mid, tier))

        candidates.sort()  # lowest score first
        target_count = max(0, self.count() - 3000)  # evict down to 3000
        to_evict = candidates[:max(0, len(candidates) - target_count)]

        if dry_run:
            conn = sqlite3.connect(self.db_path)
            size_before = self._db_size_mb()
            conn.close()
            return {
                "dry_run": True,
                "candidates": len(to_evict),
                "lowest_score": round(candidates[0][0], 4) if candidates else 0,
                "db_size_mb": round(size_before, 1),
            }

        evicted = 0
        for score, mid, _ in to_evict:
            if score < 0.5:  # only evict very low-score memories
                self.delete(mid)
                evicted += 1

        conn = sqlite3.connect(self.db_path)
        size_after = self._db_size_mb()
        conn.close()
        return {
            "evicted": evicted,
            "db_size_before_mb": round(size_before, 1),
            "db_size_after_mb": round(size_after, 1),
        }

    def _db_size_mb(self) -> float:
        try:
            return os.path.getsize(self.db_path) / (1024 * 1024)
        except OSError:
            return 0.0

    # ─── Lazy Cold Sampling ───────────────────────────────────────────────────

    def sample_cold_for_promotion(self, query_keywords: list[str]) -> int:
        """Lazy random sampling: check a few cold memories for relevance.

        Called every N queries. Picks random cold memories, checks keyword
        overlap with recent query, promotes if match is significant.
        Returns number promoted.
        """
        cold_count = self.count_tier("cold")
        if cold_count == 0 or not query_keywords:
            return 0

        conn = sqlite3.connect(self.db_path)
        # Get random cold memories
        sample_size = min(2, cold_count)
        rows = conn.execute(
            """SELECT id FROM memories WHERE tier='cold'
               ORDER BY RANDOM() LIMIT ?""",
            (sample_size,)).fetchall()
        conn.close()

        promoted = 0
        for (mid,) in rows:
            mem = self.get(mid)
            if not mem:
                continue
            # Check cooldown: don't promote if recently demoted
            if mem.last_demoted_at and (
                datetime.now(timezone.utc) - mem.last_demoted_at
            ).total_seconds() < COOLDOWN_HOURS * 3600:
                continue
            # Keyword overlap
            mem_kws = set(extract_keywords(mem.content))
            query_kws = set(w.lower() for w in query_keywords)
            if not mem_kws or not query_kws:
                continue
            overlap = len(mem_kws & query_kws) / max(len(query_kws), 1)
            if overlap >= 0.2:  # 20% keyword overlap threshold
                if self.promote(mid, "warm"):
                    promoted += 1
        return promoted

    # ─── Query helpers ─────────────────────────────────────────────────────────

    def search_by_keywords(self, keywords: list[str], limit: int = 20,
                           tier: Optional[str] = None) -> list[str]:
        if not keywords:
            return []
        ph = ",".join("?" for _ in keywords)
        params = list(keywords)
        if tier:
            q = f"SELECT DISTINCT k.memory_id FROM keywords k"
            q += f" JOIN memories m ON k.memory_id=m.id WHERE m.tier=? AND k.keyword IN ({ph})"
            params.insert(0, tier)
        else:
            q = f"SELECT DISTINCT k.memory_id FROM keywords k WHERE k.keyword IN ({ph})"
        q += " LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(q, params).fetchall()
        return [r[0] for r in rows]

    def fts_search(self, query: str, limit: int = 20,
                   tier: Optional[str] = None) -> list[str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                safe = " ".join(f'"{w}"' for w in query.split() if len(w) > 2)
                if not safe:
                    return []
                params = [safe]
                q = "SELECT m.id FROM memories_fts f JOIN memories m ON f.id=m.id WHERE"
                if tier:
                    q += " m.tier=? AND"
                    params.insert(0, tier)
                q += " memories_fts MATCH ? LIMIT ?"
                params.append(limit)
                rows = conn.execute(q, params).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def get_related_keywords(self, memory_ids: list[str], limit: int = 20) -> list[str]:
        if not memory_ids:
            return []
        ph = ",".join("?" for _ in memory_ids)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT keyword, COUNT(*) as cnt FROM keywords WHERE memory_id IN ({ph}) "
                f"GROUP BY keyword ORDER BY cnt DESC LIMIT ?",
                memory_ids + [limit]
            ).fetchall()
        return [r[0] for r in rows]

    def get_tier_summary(self) -> dict:
        """Return tier distribution for stats display."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT COALESCE(tier, 'hot') as tier, COUNT(*) FROM memories GROUP BY tier"
        ).fetchall()
        conn.close()
        summary = {"hot": 0, "warm": 0, "cold": 0}
        for tier, count in rows:
            summary[tier] = count
        return summary

    # ─── Internal ──────────────────────────────────────────────────────────────

    def _row_to_mem(self, row) -> Optional[Memory]:
        if row is None:
            return None
        # Convert tuple to dict using known column order
        cols = ["id", "content", "entities", "timestamp", "embedding",
                "access_count", "session_id", "tag", "tier",
                "last_accessed_at", "last_demoted_at"]
        d = dict(zip(cols, row))
        return Memory(
            id=d["id"], content=d["content"],
            entities=json.loads(d["entities"]),
            timestamp=datetime.fromisoformat(d["timestamp"]),
            embedding=json.loads(d["embedding"]) if d["embedding"] else None,
            access_count=d["access_count"],
            session_id=d["session_id"], tag=d["tag"],
            tier=d["tier"] if d["tier"] else "hot",
            last_accessed_at=datetime.fromisoformat(d["last_accessed_at"]) if d["last_accessed_at"] else None,
            last_demoted_at=datetime.fromisoformat(d["last_demoted_at"]) if d["last_demoted_at"] else None,
        )
