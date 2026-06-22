# recall. - Nomic Embed Weight Tuning Report
Date: 2026-06-22
Model: nomic-embed-text-v1.5 (LM Studio, 768-dim)
Memories: 30  |  Questions: 20  |  Top-K: 5

## Results

| Combination | Semantic | Recency | Entity | Recall | Precision | vs Pure |
|------------|----------|---------|--------|--------|-----------|--------|
| Pure Vector (baseline) | 1.0 | 0.0 | 0.0 | 0.385 | 0.200 | 1.00x |
| Hybrid v1 (original) | 0.5 | 0.3 | 0.2 | 0.202 | 0.120 | 0.52x |
| Hybrid v2 (more entity) | 0.4 | 0.3 | 0.3 | 0.202 | 0.120 | 0.52x |
| Hybrid v3 (entity heavy) | 0.3 | 0.3 | 0.4 | 0.189 | 0.110 | 0.49x |
| Hybrid v4 (no recency) | 0.6 | 0.0 | 0.4 | 0.398 | 0.210 | 1.03x |
| Hybrid v5 (semantic+entity) | 0.7 | 0.0 | 0.3 | 0.398 | 0.210 | 1.03x |
| Hybrid v6 (balanced entity) | 0.4 | 0.2 | 0.4 | 0.256 | 0.150 | 0.66x |

Best: Hybrid v4 (no recency) (R=0.398 P=0.210)

## Comparison with MiniLM
| Model | Best Recall | Best Precision |
|-------|-------------|---------------|
| all-MiniLM-L6-v2 (384-dim) | 0.385 | 0.200 |
| nomic-embed-text-v1.5 (768-dim) | 0.398 | 0.210 |