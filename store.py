# recall. — Storage Layer
# SQLite backend for memory persistence

import sqlite3
import json
import hashlib
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


class SQLiteStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
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

        # Vector virtual table for ANN search
        if self.vec_available:
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                        id TEXT PRIMARY KEY,
                        embedding float[384] distance_metric=cosine
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
            conn.commit()
        finally:
            conn.close()
        return memory.id

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
            conn.execute("DELETE FROM memories")
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
