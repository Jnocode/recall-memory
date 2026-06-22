"""recall. — Evaluation Pipeline (P1)
Run: python3 eval.py
Verifies hybrid scoring > pure vector after every change.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import SQLiteStore
from retrieve import retrieve_relevant, pure_vector_search

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall_p0.db")
TOP_K = 5

# 20 multi-hop questions with ground truth memory IDs
EVAL_QUESTIONS = [
    ("Which deployment method does user prefer?", {"seed_00","seed_01","seed_11","seed_20"}),
    ("What database issues encountered?", {"seed_02","seed_12","seed_16","seed_26"}),
    ("What frontend technology does user prefer?", {"seed_09","seed_15"}),
    ("What CI/CD decisions?", {"seed_00","seed_13"}),
    ("What security requirements?", {"seed_11","seed_25"}),
    ("How should APIs be structured?", {"seed_10","seed_23","seed_04"}),
    ("What performance problems?", {"seed_02","seed_03","seed_19","seed_26"}),
    ("What infrastructure changes?", {"seed_14","seed_20","seed_22"}),
    ("What Python decisions?", {"seed_09","seed_17","seed_01","seed_06"}),
    ("What monitoring tools?", {"seed_24"}),
    ("Config file format preference?", {"seed_21"}),
    ("Docker decisions?", {"seed_00","seed_08","seed_14"}),
    ("DB schema changes?", {"seed_12"}),
    ("Code quality standards?", {"seed_06","seed_13","seed_22"}),
    ("Development workflow?", {"seed_05","seed_13"}),
    ("Recent incidents?", {"seed_02"}),
    ("Migration projects?", {"seed_00","seed_14","seed_20","seed_17"}),
    ("User tool preferences?", {"seed_01","seed_04","seed_15","seed_21","seed_09"}),
    ("Architecture decisions?", {"seed_04","seed_14","seed_23","seed_22"}),
    ("Testing improvements?", {"seed_10"}),
]

MINIMUM_RATIO = 1.0   # hybrid must be >= pure vector


def evaluate(method, name: str) -> tuple:
    """Run evaluation, return (avg_recall, avg_precision, detail_list)."""
    total_r = total_p = 0
    details = []
    for q, truth in EVAL_QUESTIONS:
        mems = method(q, store, k=TOP_K)
        ids = {m.id for m in mems}
        hit = ids & truth
        r = len(hit) / len(truth) if truth else 0
        p = len(hit) / TOP_K
        total_r += r
        total_p += p
        details.append((q, r, p, len(hit), len(truth)))
    avg_r = total_r / len(EVAL_QUESTIONS)
    avg_p = total_p / len(EVAL_QUESTIONS)
    return avg_r, avg_p, details


if __name__ == "__main__":
    # Warm model
    from embed import get_embedder
    get_embedder().encode("warmup", normalize_embeddings=True)

    store = SQLiteStore(DB_PATH)
    count = store.count()
    print(f"📊 recall. evaluation pipeline")
    print(f"   Memories: {count}   Questions: {len(EVAL_QUESTIONS)}   Top-K: {TOP_K}")
    print()

    hybrid_r, hybrid_p, hybrid_d = evaluate(retrieve_relevant, "Hybrid")
    pure_r, pure_p, pure_d = evaluate(pure_vector_search, "Pure")
    ratio = hybrid_r / max(pure_r, 0.001)

    print(f"   {'Method':15s} {'Recall':>8s} {'Precision':>10s}")
    print(f"   {'─'*35}")
    print(f"   {'Hybrid':15s} {hybrid_r:>8.3f} {hybrid_p:>10.3f}")
    print(f"   {'Pure Vector':15s} {pure_r:>8.3f} {pure_p:>10.3f}")
    print(f"   {'─'*35}")
    print(f"   Ratio: {ratio:.2f}x  (threshold: {MINIMUM_RATIO:.1f}x)")
    print()

    passed = ratio >= MINIMUM_RATIO
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"   {status} — Hybrid{' ' if passed else ' NOT '}> Pure ({ratio:.2f}x {'>=' if passed else '<'} {MINIMUM_RATIO:.1f}x)")

    if not passed:
        print()
        print("   ⚠️  WARNING: Evaluation failed. Recent change may have degraded retrieval.")
        print("      Check the latest code change before proceeding.")
        sys.exit(1)

    # Show per-question breakdown
    print()
    for i, ((q, _), (_, hr, hp, hits, total)) in enumerate(zip(EVAL_QUESTIONS, hybrid_d)):
        mark = "✅" if hits >= 1 else "❌"
        print(f"   {mark} {i+1:2d}. R={hr:.2f} P={hp:.2f} ({hits}/{total}) — {q[:40]}")
    print()
    print(f"   ✅ Evaluation pipeline complete. Hybrid > Pure confirmed.")
