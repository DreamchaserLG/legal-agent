from __future__ import annotations

"""Optional FAISS IVF-PQ candidate backend.

FAISS is imported lazily and is not a mandatory web dependency. Index building
is explicit and intended for a bounded local experiment, never application
startup. PostgreSQL remains the authority for source text and FP32 re-ranking.
"""

import json
from pathlib import Path
from typing import Sequence


def faiss_available() -> bool:
    try:
        import faiss  # type: ignore  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


def build_ivfpq_artifact(
    vectors: Sequence[Sequence[float]], chunk_ids: Sequence[int], *, output_path: str,
    nlist: int = 2048, subquantizers: int = 64, bits: int = 8,
) -> dict:
    if not faiss_available():
        raise RuntimeError("Optional dependency faiss-cpu is not installed")
    if len(vectors) != len(chunk_ids) or not vectors:
        raise ValueError("vectors and chunk_ids must be non-empty and aligned")
    import faiss  # type: ignore
    import numpy as np

    matrix = np.asarray(vectors, dtype="float32")
    ids = np.asarray(chunk_ids, dtype="int64")
    dimension = int(matrix.shape[1])
    if dimension % int(subquantizers) != 0:
        raise ValueError("Vector dimension must be divisible by PQ subquantizers")
    faiss.normalize_L2(matrix)
    effective_nlist = max(1, min(int(nlist), len(matrix)))
    quantizer = faiss.IndexFlatIP(dimension)
    base = faiss.IndexIVFPQ(
        quantizer, dimension, effective_nlist, int(subquantizers), int(bits), faiss.METRIC_INNER_PRODUCT,
    )
    index = faiss.IndexIDMap2(base)
    index.train(matrix)
    index.add_with_ids(matrix, ids)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(target))
    metadata = {
        "index_type": "faiss_ivfpq", "dimension": dimension, "vectors": len(ids),
        "nlist": effective_nlist, "subquantizers": int(subquantizers), "bits": int(bits),
        "metric": "inner_product_on_l2_normalized_vectors",
    }
    target.with_suffix(target.suffix + ".json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8",
    )
    return {"status": "completed", "path": str(target), **metadata}


def search_ivfpq_artifact(
    query_vector: Sequence[float], *, index_path: str, limit: int = 200, probes: int = 32,
) -> list[dict]:
    if not faiss_available():
        raise RuntimeError("Optional dependency faiss-cpu is not installed")
    import faiss  # type: ignore
    import numpy as np

    index = faiss.read_index(str(Path(index_path)))
    inner = index.index if hasattr(index, "index") else index
    if hasattr(inner, "nprobe"):
        inner.nprobe = max(1, int(probes))
    query = np.asarray([query_vector], dtype="float32")
    faiss.normalize_L2(query)
    scores, ids = index.search(query, max(1, int(limit)))
    return [
        {"chunk_id": int(chunk_id), "quantized_score": float(score)}
        for chunk_id, score in zip(ids[0], scores[0]) if int(chunk_id) >= 0
    ]

