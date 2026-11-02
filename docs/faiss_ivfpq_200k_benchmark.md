# FAISS IVF-PQ 200k Canadian retrieval benchmark

Run date: 2026-11-02. The experiment used an isolated deterministic sample of 200,000 BGE-M3 vectors (1024 dimensions) and 40 Canadian legal queries. It did not modify the production embedding table or resume context migration.

## Method

- Exact ground truth: normalized FP32 inner-product search.
- Candidate indexes: `IVF2048,PQ64x8` and `IVF4096,PQ64x8`.
- Probe grid: 16, 32, 64, 96, 128.
- Production-shaped result: retrieve 200 compressed candidates, then rerank with original FP32 vectors.
- Metrics: Recall@10, per-query candidate search plus local rerank latency, build time, and artifact size.

## Results after FP32 reranking

| Index | Probes | Recall@10 | Latency/query (ms) |
|---|---:|---:|---:|
| IVF2048,PQ64x8 | 16 | 0.8100 | 0.396 |
| IVF2048,PQ64x8 | 32 | 0.8725 | 0.294 |
| IVF2048,PQ64x8 | 64 | 0.9050 | 0.309 |
| IVF2048,PQ64x8 | 96 | 0.9250 | 0.377 |
| IVF2048,PQ64x8 | 128 | 0.9375 | 0.389 |
| IVF4096,PQ64x8 | 16 | 0.7900 | 0.480 |
| IVF4096,PQ64x8 | 32 | 0.8650 | 0.499 |
| IVF4096,PQ64x8 | 64 | 0.9100 | 0.503 |
| IVF4096,PQ64x8 | 96 | 0.9325 | 0.573 |
| IVF4096,PQ64x8 | 128 | 0.9425 | 0.509 |

Direct compressed ranking was only 0.49–0.525 Recall@10 and must not be used as the final ranking. FP32 reranking improves the selected IVF2048/probes96 configuration to 0.9250.

## Resource comparison and decision

| Index | Build time | Artifact size |
|---|---:|---:|
| IVF2048,PQ64x8 | 70.545 s | 24.27 MiB |
| IVF4096,PQ64x8 | 99.760 s | 32.29 MiB |

The local default candidate is `IVF2048,PQ64x8`, probes 96, candidate pool at least 200, followed by authoritative FP32 reranking in PostgreSQL. `IVF4096` gains only 0.75 percentage points Recall@10 at probes 96 while increasing artifact size by about 33% and build time by about 41%. Use IVF4096/probes128 only for a high-recall profile after validation on the full corpus.

These figures measure ANN fidelity, not substantive legal correctness. The dataset retains expected Canadian authorities so the existing hybrid retrieval evaluator can separately measure Hit@K and MRR.
