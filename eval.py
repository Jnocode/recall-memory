"""recall. — Evaluation Pipeline
Measures recall of retrieve_relevant() on 20 multi-hop QA questions.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import SQLiteStore
from retrieve import retrieve_relevant

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall_p0.db")
TOP_K = 5

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


if __name__ == "__main__":
    store = SQLiteStore(DB_PATH)
    count = store.count()
    print(f"📊 recall. evaluation")
    print(f"   Memories: {count}   Questions: {len(EVAL_QUESTIONS)}   Top-K: {TOP_K}")

    total_recall = 0
    for q, truth in EVAL_QUESTIONS:
        mems = retrieve_relevant(q, store, k=TOP_K)
        ids = {m.id for m in mems}
        hit = ids & truth
        r = len(hit) / len(truth) if truth else 0
        total_recall += r

    avg_r = total_recall / len(EVAL_QUESTIONS)
    print(f"\n   Recall@{TOP_K}: {avg_r:.3f}")
    print(f"   Memories: {count}")
