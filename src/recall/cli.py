# recall. — CLI (typer)

import typer
from datetime import datetime
from .store import Memory, SQLiteStore
from .retrieve import extract_entities, retrieve_relevant, pure_vector_search

app = typer.Typer()
store: SQLiteStore = None  # initialized lazily


def get_store(db_path: str = "recall.db") -> SQLiteStore:
    global store
    if store is None:
        store = SQLiteStore(db_path)
    return store


@app.command()
def add(
    content: str,
    session: str = "default",
    tag: str = "episodic",
    db: str = "recall.db",
):
    """Add a memory to the store."""
    s = get_store(db)
    entities = extract_entities(content)
    mem = Memory(content=content, entities=entities, session_id=session, tag=tag)
    mem_id = s.add(mem)
    typer.echo(f"✅ [{mem_id[:8]}] {content[:60]}...")
    typer.echo(f"   Entities: {entities[:6]}")


@app.command()
def query(
    query: str,
    k: int = 5,
    db: str = "recall.db",
):
    """Query memories using hybrid scoring."""
    s = get_store(db)
    results = retrieve_relevant(query, s, k=k)
    typer.echo(f"\n🔍 Query: {query}")
    typer.echo(f"{'─'*50}")
    for i, mem in enumerate(results, 1):
        ts = mem.timestamp.strftime("%m-%d")
        typer.echo(f"  {i}. [{ts}] [{mem.tag}] {mem.content[:80]}")
        if mem.entities:
            typer.echo(f"     entities: {mem.entities[:6]}")
    if not results:
        typer.echo("  (no results)")


@app.command()
def pure(
    query: str,
    k: int = 5,
    db: str = "recall.db",
):
    """Query using pure vector search (baseline)."""
    s = get_store(db)
    results = pure_vector_search(query, s, k=k)
    typer.echo(f"\n🔍 Query (pure vector): {query}")
    typer.echo(f"{'─'*50}")
    for i, mem in enumerate(results, 1):
        ts = mem.timestamp.strftime("%m-%d")
        typer.echo(f"  {i}. [{ts}] [{mem.tag}] {mem.content[:80]}")


@app.command()
def stats(db: str = "recall.db"):
    """Show store statistics."""
    s = get_store(db)
    total = s.count()
    episodic = s.count(tag="episodic")
    semantic = s.count(tag="semantic")
    typer.echo(f"📊 recall. stats")
    typer.echo(f"{'─'*30}")
    typer.echo(f"  Total:     {total}")
    typer.echo(f"  Episodic:  {episodic}")
    typer.echo(f"  Semantic:  {semantic}")
    if total > 0:
        latest = s.get_all(limit=1)
        typer.echo(f"  Latest:    {latest[0].content[:50]}...")


@app.command()
def delete(
    memory_id: str,
    db: str = "recall.db",
):
    """Delete a memory by ID."""
    s = get_store(db)
    if s.delete(memory_id):
        typer.echo(f"🗑️  Deleted {memory_id}")
    else:
        typer.echo(f"❌ Not found: {memory_id}")


@app.command()
def clear(db: str = "recall.db"):
    """Clear ALL memories."""
    typer.confirm("Delete all memories?", abort=True)
    s = get_store(db)
    s.clear()
    typer.echo("🗑️  All memories cleared.")


if __name__ == "__main__":
    app()
