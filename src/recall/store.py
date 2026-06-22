# recall. — Storage Layer
# SQLite backend for memory persistence with keyword index

import sqlite3
import json
import hashlib
import re
import numpy as np
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Memory:
    content: str
    id: str = ""
    entities: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    embedding: Optional[list[float]] = None
    access_count: int = 0
    session_id: str = ""
    tag: str = "episodic"


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
    # Multi-word tech terms (docker-compose, multi-stage, etc.)
    words.update(re.findall(r'\b[a-zA-Z][a-zA-Z0-9]+[-_][a-zA-Z0-9][-a-zA-Z0-9]*\b', text))
    # Capitalized proper nouns (Docker, FastAPI, PostgreSQL, React)
    words.update(re.findall(r'\b[A-Z][a-zA-Z0-9+#_-]{2,}\b', text))
    # CamelCase terms (PostgreSQL, SQLAlchemy, Microservices)
    words.update(re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z0-9]*\b', text))
    # Version numbers (Python 3.12, v2, 0.8.0)
    words.update(re.findall(r'\b[A-Za-z]+\s*\d+\.\d+[\w.]*\b', text))
    # 4+ char lowercase words (filtered by stop list)
    for w in re.findall(r'\b[a-zA-Z]{4,}\b', text.lower()):
        if w not in _STOP:
            words.add(w)
    return sorted(words)


# ─── SQLite Store ─────────────────────────────────────────────────────────────

class SQLiteStore:
    def __init__(self, db_path: str, vec_dim: int = 768):
        self.db_path = db_path
        self.vec_dim = vec_dim
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.enable_load_extension(True)
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
                tag TEXT DEFAULT 'episodic'
            )
        """)

        # Keyword index table (for two-path retrieval)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS keywords (
                keyword TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                PRIMARY KEY (keyword, memory_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kw ON keywords(keyword)")

        # FTS5 full-text search table (from AIngram)
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, id UNINDEXED, tokenize='porter unicode61'
                )
            """)
        except Exception:
            pass  # FTS5 not available in this SQLite build

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
                     "idx_session ON memories(session_id)"]:
            try:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {idx}")
            except Exception:
                pass
        conn.close()

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
                "INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?,?)",
                (memory.id, memory.content, json.dumps(memory.entities),
                 memory.timestamp.isoformat(), json.dumps(memory.embedding),
                 memory.access_count, memory.session_id, memory.tag)
            )
            if self.vec_available and memory.embedding:
                vec_bytes = np.array(memory.embedding, dtype=np.float32).tobytes()
                conn.execute(
                    "INSERT OR REPLACE INTO vec_embeddings(id, embedding) VALUES (?, ?)",
                    (memory.id, vec_bytes)
                )
            # Keyword index
            for kw in extract_keywords(memory.content):
                conn.execute("INSERT OR REPLACE INTO keywords VALUES (?,?)",
                             (kw.lower(), memory.id))
            
            # FTS5 index
            try:
                conn.execute("INSERT OR REPLACE INTO memories_fts VALUES (?, ?)",
                             (memory.content, memory.id))
            except Exception:
                pass
            
            conn.commit()
        finally:
            conn.close()
        return memory.id

    def search_by_keywords(self, keywords: list[str], limit: int = 20) -> list[str]:
        if not keywords:
            return []
        ph = ",".join("?" for _ in keywords)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT DISTINCT memory_id FROM keywords WHERE keyword IN ({ph}) LIMIT ?",
                keywords + [limit]
            ).fetchall()
        return [r[0] for r in rows]

    def fts_search(self, query: str, limit: int = 20) -> list[str]:
            """FTS5 full-text search (from AIngram). Returns memory IDs."""
            try:
                with sqlite3.connect(self.db_path) as conn:
                    # Sanitize FTS query
                    safe = " ".join(f'"{w}"' for w in query.split() if len(w) > 2)
                    if not safe:
                        return []
                    rows = conn.execute(
                        "SELECT id FROM memories_fts WHERE memories_fts MATCH ? LIMIT ?",
                        (safe, limit)
                    ).fetchall()
                return [r[0] for r in rows]
            except Exception:
                return []

    def get_related_keywords(self, memory_ids: list[str], limit: int = 20) -> list[str]:
        """Get keywords that appear in the given memories."""
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

    def get(self, memory_id: str) -> Optional[Memory]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._row_to_mem(row) if row else None

    def get_all(self, limit: int = 1000) -> list[Memory]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM memories ORDER BY timestamp DESC LIMIT ?",
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
        allowed = {"content", "entities", "embedding", "access_count", "tag"}
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
        vals.append(memory_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE memories SET {set_clause} WHERE id=?", vals)
        return True

    def delete(self, memory_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM keywords WHERE memory_id=?", (memory_id,))
            try:
                conn.execute("DELETE FROM memories_fts WHERE id=?", (memory_id,))
            except Exception:
                pass
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return cur.rowcount > 0

    def count(self, tag: Optional[str] = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            if tag:
                return conn.execute("SELECT COUNT(*) FROM memories WHERE tag=?", (tag,)).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

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

    def _row_to_mem(self, row) -> Memory:
        return Memory(
            id=row[0], content=row[1], entities=json.loads(row[2]),
            timestamp=datetime.fromisoformat(row[3]),
            embedding=json.loads(row[4]) if row[4] else None,
            access_count=row[5], session_id=row[6], tag=row[7]
        )
