"""Legacy core preservation gates: only synthetic, temporary SQLite fixtures."""
import importlib
from pathlib import Path
import sqlite3
import subprocess

from recall.store import Memory, SQLiteStore
from recall.retrieve import ann_search, retrieve_relevant


def test_legacy_store_reopens_without_changing_record_content(tmp_path):
    db = tmp_path / 'legacy.db'
    store = SQLiteStore(str(db))
    store.add(Memory(id='demo-one', content='cedar preservation decision',
                     session_id='demo-session', tag='semantic'))
    reopened = SQLiteStore(str(db))
    assert reopened.count() == 1
    assert reopened.get('demo-one').content == 'cedar preservation decision'
    with sqlite3.connect(db) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(memories)')}
        assert {'id', 'content', 'session_id', 'tag', 'tier'} <= columns
        # Opening the legacy provider must not implicitly migrate to MCP authority.
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='memory_metadata'").fetchall()


def test_real_sqlite_vec_knn_constraint_and_session_scores(tmp_path, monkeypatch):
    store = SQLiteStore(str(tmp_path / 'vectors.db'), vec_dim=3)
    assert store.vec_available, 'sqlite-vec is a declared dependency, not an optional skip'
    store.add(Memory(id='demo-vector', content='cedar vector', session_id='demo-a',
                     tag='semantic', embedding=[1.0, 0.1, 0.1]))
    store.add(Memory(id='demo-other', content='cedar vector other', session_id='demo-b',
                     tag='semantic', embedding=[0.1, 1.0, 0.1]))
    store.add(Memory(id='demo-legacy', content='cedar legacy', session_id='',
                     tag='semantic', embedding=[0.5, 0.2, 0.1]))
    assert ann_search(store, [1.0, 0.1, 0.1], k=1) == ['demo-vector']
    module = importlib.import_module('recall.retrieve')
    monkeypatch.setattr(module, 'embed', lambda text: [1.0, 0.1, 0.1])
    results = retrieve_relevant('cedar', store, k=5, tag_filter='semantic', session_id_filter='demo-a')
    assert {m.id for m in results} == {'demo-vector', 'demo-legacy'}
    assert all(m.score > 0 for m in results)


def test_generated_root_distributions_are_not_git_source():
    root = Path(__file__).resolve().parents[1]
    if not (root / '.git').exists():
        return  # sdist has no Git metadata
    tracked = subprocess.check_output(['git', 'ls-files', 'dist'], cwd=root, text=True).splitlines()
    assert tracked == [], 'Rebuild distributions in CI; do not preserve stale wheels as source'
