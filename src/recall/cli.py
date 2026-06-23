# recall. — CLI (typer)

import typer
from datetime import datetime
from .store import Memory, SQLiteStore
from .retrieve import retrieve_tiered, retrieve_relevant
from .config import DEFAULT_DB_PATH

app = typer.Typer()
store: SQLiteStore = None


def get_store(db_path: str = DEFAULT_DB_PATH) -> SQLiteStore:
    global store
    if store is None:
        store = SQLiteStore(db_path)
    return store


@app.command()
def add(
    content: str,
    session: str = "default",
    tag: str = "episodic",
):
    """Add a memory to the store."""
    s = get_store()
    mem = Memory(content=content, session_id=session, tag=tag)
    mem_id = s.add(mem)
    typer.echo(f"✅ [{mem_id[:8]}] {content[:60]}...")


@app.command()
def query(
    query_text: str = typer.Argument(..., help="What to search for"),
    k: int = 5,
    include_cold: bool = typer.Option(False, "--include-cold", help="Search cold tier too"),
):
    """Query memories using tiered storage."""
    s = get_store()
    if include_cold:
        results = retrieve_relevant(query_text, s, k=k, tier=None)
    else:
        results = retrieve_tiered(query_text, s, k=k)
    typer.echo(f"\n🔍 Query: {query_text}")
    typer.echo(f"{'─'*60}")
    for i, mem in enumerate(results, 1):
        ts = mem.timestamp.strftime("%m-%d")
        tier_tag = f"[{mem.tier[0].upper()}]" if mem.tier else ""
        typer.echo(f"  {i}. {tier_tag} [{ts}] [{mem.tag}] {mem.content[:80]}")
    if not results:
        typer.echo("  (no results)")


@app.command()
def stats(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show tier distribution"),
):
    """Show store statistics."""
    s = get_store()
    total = s.count()
    episodic = s.count(tag="episodic")
    semantic = s.count(tag="semantic")
    typer.echo(f"📊 recall. stats")
    typer.echo(f"{'─'*30}")
    typer.echo(f"  Total:     {total}")
    typer.echo(f"  Episodic:  {episodic}")
    typer.echo(f"  Semantic:  {semantic}")
    if verbose:
        tiers = s.get_tier_summary()
        typer.echo(f"")
        typer.echo(f"  Tier Distribution:")
        typer.echo(f"    Hot:   {tiers['hot']}")
        typer.echo(f"    Warm:  {tiers['warm']}")
        typer.echo(f"    Cold:  {tiers['cold']}")
    if total > 0:
        latest = s.get_all(limit=1)
        typer.echo(f"  Latest:    {latest[0].content[:50]}...")


@app.command()
def gc(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without deleting"),
):
    """Run garbage collection to evict low-score memories."""
    s = get_store()
    result = s.evict(dry_run=dry_run)
    if dry_run:
        typer.echo(f"🧹 GC dry-run")
        typer.echo(f"  Candidates: {result['candidates']}")
        typer.echo(f"  Lowest score: {result['lowest_score']}")
        typer.echo(f"  DB size: {result['db_size_mb']} MB")
    else:
        typer.echo(f"🧹 GC complete")
        typer.echo(f"  Evicted: {result['evicted']}")
        typer.echo(f"  DB size: {result['db_size_after_mb']} MB")


@app.command()
def delete(
    memory_id: str = typer.Argument(..., help="Memory ID to delete"),
):
    """Delete a memory by ID."""
    s = get_store()
    if s.delete(memory_id):
        typer.echo(f"🗑️  Deleted {memory_id}")
    else:
        typer.echo(f"❌ Not found: {memory_id}")


@app.command()
def clear():
    """Clear ALL memories."""
    typer.confirm("Delete all memories?", abort=True)
    s = get_store()
    s.clear()
    typer.echo("🗑️  All memories cleared.")


if __name__ == "__main__":
    app()
