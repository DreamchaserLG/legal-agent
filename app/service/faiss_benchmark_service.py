from __future__ import annotations

"""Reproducible, isolated FAISS benchmarks over a PostgreSQL sample table."""

import json
from pathlib import Path
from time import perf_counter

from sqlalchemy import text

from app.core.database import engine
from app.service.embedding_service import create_embedding
from app.service.vector_benchmark_service import summarize_backend_trials


ALLOWED_SAMPLE_TABLES = {f"rag_vector_eval_{size}" for size in (50_000, 100_000, 200_000, 300_000, 500_000)}


def load_sample_matrix(table_name: str) -> tuple[object, object]:
    """Stream vectors into one compact float32 allocation instead of Python float objects."""
    if table_name not in ALLOWED_SAMPLE_TABLES:
        raise ValueError("Only isolated vector evaluation tables are allowed")
    import numpy as np

    with engine.connect() as conn:
        count = int(conn.execute(text(f"SELECT count(*) FROM {table_name}")).scalar() or 0)
        dimension = int(conn.execute(text(
            f"SELECT vector_dims(embedding_vector) FROM {table_name} WHERE embedding_vector IS NOT NULL LIMIT 1"
        )).scalar() or 0)
        matrix = np.empty((count, dimension), dtype="float32")
        ids = np.empty(count, dtype="int64")
        result = conn.execution_options(stream_results=True).execute(text(
            f"SELECT chunk_id, embedding_vector::real[] FROM {table_name} ORDER BY chunk_id"
        ))
        offset = 0
        for partition in result.partitions(512):
            end = offset + len(partition)
            ids[offset:end] = [row[0] for row in partition]
            matrix[offset:end] = [row[1] for row in partition]
            offset = end
    if offset != count:
        raise RuntimeError(f"Expected {count} vectors but loaded {offset}")
    return matrix, ids


def load_canada_queries(eval_path: str, *, limit: int = 100) -> list[dict]:
    payload = json.loads(Path(eval_path).read_text(encoding="utf-8"))
    return [
        {"id": str(item["id"]), "query": str(item["query"]), "domain": str(item.get("domain") or "unspecified")}
        for item in payload.get("cases", [])[: max(1, int(limit))]
    ]


def _set_nprobe(index, probes: int) -> None:
    import faiss  # type: ignore
    faiss.ParameterSpace().set_index_parameter(index, "nprobe", max(1, int(probes)))


def run_ivfpq_grid(
    *, table_name: str, eval_path: str, output_dir: str,
    nlists: tuple[int, ...] = (2048, 4096), probes: tuple[int, ...] = (16, 32, 64, 96, 128),
    k: int = 20, threads: int = 0,
) -> dict:
    """Compare IVF-PQ configurations against an exact normalized-IP ground truth."""
    import faiss  # type: ignore
    import numpy as np

    if threads > 0:
        faiss.omp_set_num_threads(int(threads))
    loaded_at = perf_counter()
    matrix, ids = load_sample_matrix(table_name)
    faiss.normalize_L2(matrix)
    load_seconds = perf_counter() - loaded_at
    cases = load_canada_queries(eval_path)
    query_matrix = np.ascontiguousarray([create_embedding(item["query"]) for item in cases], dtype="float32")
    faiss.normalize_L2(query_matrix)

    exact = faiss.IndexFlatIP(matrix.shape[1])
    exact.add(matrix)
    exact_started = perf_counter()
    _, exact_positions = exact.search(query_matrix, int(k))
    exact_ms = (perf_counter() - exact_started) * 1000 / max(1, len(cases))
    exact_ids = ids[exact_positions]

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    configurations: list[dict] = []
    for nlist in nlists:
        quantizer = faiss.IndexFlatIP(matrix.shape[1])
        base = faiss.IndexIVFPQ(
            quantizer, matrix.shape[1], int(nlist), 64, 8, faiss.METRIC_INNER_PRODUCT,
        )
        # Bound CPU cost while retaining at least the conventional 39 samples/centroid.
        base.cp.niter = 12
        base.cp.min_points_per_centroid = 20
        base.cp.max_points_per_centroid = 64
        index = faiss.IndexIDMap2(base)
        build_started = perf_counter()
        index.train(matrix)
        index.add_with_ids(matrix, ids)
        build_seconds = perf_counter() - build_started
        artifact = target_dir / f"ivf{nlist}_pq64x8_{len(ids)}.faiss"
        faiss.write_index(index, str(artifact))
        trials: list[dict] = []
        for nprobe in probes:
            _set_nprobe(index, nprobe)
            started = perf_counter()
            _, candidate_ids = index.search(query_matrix, int(k))
            elapsed_ms = (perf_counter() - started) * 1000 / max(1, len(cases))
            rows = [
                {
                    "query": case["query"], "domain": case["domain"],
                    "exact_ids": exact_ids[i].tolist(), "candidate_ids": candidate_ids[i].tolist(),
                    "latency_ms": elapsed_ms,
                }
                for i, case in enumerate(cases)
            ]
            trials.append({"probes": int(nprobe), "summary": summarize_backend_trials(rows), "queries": rows})
        configurations.append({
            "name": f"IVF{nlist},PQ64x8", "nlist": int(nlist), "build_seconds": round(build_seconds, 3),
            "artifact": str(artifact), "artifact_bytes": artifact.stat().st_size, "trials": trials,
        })
        del index, base, quantizer
    report = {
        "status": "completed", "isolated_sample": True, "migration_started": False,
        "table": table_name, "vectors": int(len(ids)), "dimension": int(matrix.shape[1]),
        "queries": len(cases), "domains": sorted({item["domain"] for item in cases}),
        "load_seconds": round(load_seconds, 3), "exact_latency_ms_per_query": round(exact_ms, 3),
        "faiss_version": getattr(faiss, "__version__", "unknown"), "configurations": configurations,
    }
    report_path = target_dir / "ivfpq_benchmark_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def evaluate_ivfpq_rerank(
    *, table_name: str, eval_path: str, artifact_paths: list[str],
    probes: tuple[int, ...] = (16, 32, 64, 96, 128), k: int = 20,
    candidate_pool: int = 200, threads: int = 0, output_path: str | None = None,
) -> dict:
    """Evaluate the production shape: compressed candidates followed by FP32 reranking."""
    import faiss  # type: ignore
    import numpy as np

    if threads > 0:
        faiss.omp_set_num_threads(int(threads))
    matrix, ids = load_sample_matrix(table_name)
    faiss.normalize_L2(matrix)
    cases = load_canada_queries(eval_path)
    queries = np.ascontiguousarray([create_embedding(item["query"]) for item in cases], dtype="float32")
    faiss.normalize_L2(queries)
    exact = faiss.IndexFlatIP(matrix.shape[1])
    exact.add(matrix)
    _, exact_positions = exact.search(queries, int(k))
    exact_ids = ids[exact_positions]
    results: list[dict] = []
    for artifact_path in artifact_paths:
        index = faiss.read_index(str(Path(artifact_path)))
        trials: list[dict] = []
        for nprobe in probes:
            _set_nprobe(index, nprobe)
            started = perf_counter()
            _, candidates = index.search(queries, max(int(k), int(candidate_pool)))
            reranked = np.empty((len(cases), int(k)), dtype="int64")
            for row_number, candidate_ids in enumerate(candidates):
                valid_ids = candidate_ids[candidate_ids >= 0]
                positions = np.searchsorted(ids, valid_ids)
                valid = (positions < len(ids)) & (ids[np.minimum(positions, len(ids) - 1)] == valid_ids)
                positions = positions[valid]
                scores = matrix[positions] @ queries[row_number]
                best = positions[np.argsort(-scores)[: int(k)]]
                reranked[row_number, : len(best)] = ids[best]
            elapsed_ms = (perf_counter() - started) * 1000 / max(1, len(cases))
            rows = [
                {"exact_ids": exact_ids[i].tolist(), "candidate_ids": reranked[i].tolist(), "latency_ms": elapsed_ms}
                for i in range(len(cases))
            ]
            trials.append({"probes": int(nprobe), "candidate_pool": int(candidate_pool), "summary": summarize_backend_trials(rows)})
        results.append({"artifact": str(artifact_path), "trials": trials})
    report = {
        "status": "completed", "isolated_sample": True, "migration_started": False,
        "vectors": int(len(ids)), "queries": len(cases), "candidate_pool": int(candidate_pool), "indexes": results,
    }
    if output_path:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(target)
    return report
