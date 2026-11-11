"""本地兼容补丁：让上游代码用得上「bit 汉明量化」索引。

事实基线（2026-11-07 实测）
--------------------------
本机兜底库的向量索引是：

    CREATE INDEX idx_rag_embeddings_ivfflat_bq ON rag_chunk_embeddings
      USING ivfflat (((binary_quantize(embedding_vector))::bit(1024)) bit_hamming_ops)
      WITH (lists = 2500)
      WHERE embedding_provider = 'sentence_transformers' AND embedding_model = 'BAAI/bge-m3'

即 **IVFFlat 结构 + 二进制量化 + 汉明距离**，不是余弦表达式索引，也不是 HNSW。
上游代码（GitHub HEAD）只认：

  - `get_hnsw_index_state()` 有效才放行向量检索，否则 `ann_unavailable`
  - SQL 用基列 `embedding_vector <=> CAST(:q AS vector)`（需要向量余弦索引）

于是上游在本机库上要么关掉向量检索，要么退化成 658 万行全表扫（历史实测 248s）。

本补丁（仅 `RAG_ANN_COMPAT=ivfflat_bq` 生效）
--------------------------------------------
1. `vector_index_management_service.py`：HNSW 缺失时，若存在 bit 汉明索引则视为可用 ANN。
2. `vector_store_service.py`：`_build_pgvector_ann_query` 改走**两阶段**：
   汉明索引粗排取候选池 → 余弦精确重排取 top-N。
   `_build_pgvector_source_rerank_query` 不动（它本来就是 FTS 候选 + 精确重排，不吃 ANN 索引）。

远端新语料库（legal_corpus，原生 HNSW）不设该开关，完全走上游原逻辑，本补丁是死代码。
`tools/refresh_deploy.py` 覆盖 app/ 后会自动重放本补丁；上游若原生支持，脚本会因锚点消失而报错。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VECTOR_STORE = ROOT / "app" / "service" / "vector_store_service.py"
INDEX_STATE = ROOT / "app" / "service" / "vector_index_management_service.py"

MARKER = "# [local-patch:ivfflat-compat]"

COMPAT_HELPERS = '''{marker}
def _ann_compat() -> str:
    """RAG_ANN_COMPAT=ivfflat_bq 时启用「汉明粗排 + 余弦精排」两阶段检索。"""
    return os.getenv("RAG_ANN_COMPAT", "").strip().lower()


def _ann_compat_ready() -> bool:
    return _ann_compat() == "ivfflat_bq"


def _build_pgvector_ann_query_hamming(
    query_vector: list[float],
    *,
    meta: dict,
    module: str,
    source_filter: str,
    filters: dict | None,
    ann_limit: int,
    outer_limit: int | None = None,
) -> tuple[str, dict]:
    """本地兜底库的两阶段检索：bit 汉明索引粗排，余弦精确重排。

    粗排候选池必须放大（汉明只看符号位，精度低），否则外层的来源/结构化过滤
    会把候选筛空；精排在候选池内做完整余弦，保证排序质量。
    """
    coarse_limit = max(400, min(int(ann_limit) * 40, 3000))
    bounded_outer_limit = max(1, min(int(outer_limit or ann_limit), 1000))
    params = {
        "ann_limit": coarse_limit,
        "outer_limit": bounded_outer_limit,
        "embedding_model": meta["model"],
        "embedding_provider": meta["provider"],
        "query_vector": _vector_text(query_vector),
    }
    where_clause = _chunk_filter_clause(source_filter, module, params, alias="rc")
    structured_clause, structured_params = _structured_filter_clause(filters, alias="rc")
    params.update(structured_params)
    sql = f"""
        WITH ann AS MATERIALIZED (
            SELECT chunk_id
            FROM rag_chunk_embeddings
            WHERE embedding_provider = :embedding_provider
              AND embedding_model = :embedding_model
              AND embedding_vector IS NOT NULL
            ORDER BY (binary_quantize(embedding_vector))::bit(1024)
                     <~> (binary_quantize(CAST(:query_vector AS vector)))::bit(1024)
            LIMIT :ann_limit
        )
        SELECT
            rc.id, rc.source_kind, rc.source_table, rc.source_id, rc.source_code,
            rc.source_uid, rc.title, rc.source_url, rc.published_at, rc.chunk_index,
            rc.jurisdiction, rc.document_type, rc.court_level, rc.language, rc.citation,
            rc.text_content, rc.metadata_json,
            1 - (rce.embedding_vector <=> CAST(:query_vector AS vector)) AS score
        FROM ann a
        JOIN rag_chunk_embeddings rce ON rce.chunk_id = a.chunk_id
        JOIN rag_chunks rc ON rc.id = a.chunk_id
        WHERE rc.content_hash = rce.content_hash
          AND {where_clause}
          {structured_clause}
        ORDER BY rce.embedding_vector <=> CAST(:query_vector AS vector)
        LIMIT :outer_limit
    """
    return sql, params
'''

INDEX_HELPER = '''{marker}
def _compat_ann_index_state(conn) -> dict[str, Any] | None:
    """RAG_ANN_COMPAT=ivfflat_bq 时，把 bit 汉明量化索引当作可用 ANN。"""
    if os.getenv("RAG_ANN_COMPAT", "").strip().lower() != "ivfflat_bq":
        return None
    row = conn.execute(
        text(
            """
            SELECT c.relname AS index_name, i.indisvalid, i.indisready,
                   pg_get_indexdef(c.oid) AS definition
            FROM pg_class c
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE c.relname IN ('idx_rag_embeddings_ivfflat_bq', 'idx_rag_chunk_embeddings_active_hnsw')
            ORDER BY CASE WHEN c.relname LIKE '%ivfflat%' THEN 0 ELSE 1 END
            LIMIT 1
            """
        )
    ).mappings().first()
    if not row:
        return None
    state = dict(row)
    return {
        "available": True,
        "index_name": state["index_name"],
        "index_type": "ivfflat_bq",
        "compat": True,
        "valid": bool(state["indisvalid"]),
        "ready": bool(state["indisready"]),
        "definition": state["definition"],
    }
'''


def _fail(message: str) -> None:
    print(f"[patch] FAILED: {message}", file=sys.stderr)
    raise SystemExit(4)


def _ensure_os_import(text: str) -> str:
    if "\nimport os\n" in text or text.startswith("import os\n"):
        return text
    return text.replace(
        "from __future__ import annotations\n",
        "from __future__ import annotations\n\nimport os\n",
        1,
    )


def patch_vector_store(text: str) -> tuple[str, bool]:
    if MARKER in text:
        return text, False
    anchor = "def _build_pgvector_ann_query(\n"
    if anchor not in text:
        _fail("vector_store_service.py: 找不到 _build_pgvector_ann_query 锚点")
    body_anchor = "    bounded_limit = max(1, min(int(ann_limit), _hnsw_filtered_scan_tuples()))\n"
    if body_anchor not in text:
        _fail("vector_store_service.py: _build_pgvector_ann_query 函数体已变")

    text = _ensure_os_import(text)
    text = text.replace(anchor, COMPAT_HELPERS.replace("{marker}", MARKER) + "\n\n" + anchor, 1)
    text = text.replace(
        body_anchor,
        "    if _ann_compat_ready():\n"
        "        return _build_pgvector_ann_query_hamming(\n"
        "            query_vector,\n"
        "            meta=meta,\n"
        "            module=module,\n"
        "            source_filter=source_filter,\n"
        "            filters=filters,\n"
        "            ann_limit=ann_limit,\n"
        "            outer_limit=outer_limit,\n"
        "        )\n\n" + body_anchor,
        1,
    )
    print("[patch] vector_store_service.py: 已注入两阶段（汉明粗排 + 余弦精排）检索分支")
    return text, True


def patch_index_state(text: str) -> tuple[str, bool]:
    if MARKER in text:
        return text, False
    anchor = "def get_hnsw_index_state() -> dict[str, Any]:"
    if anchor not in text:
        _fail("vector_index_management_service.py: 找不到 get_hnsw_index_state 锚点")
    text = _ensure_os_import(text)
    text = text.replace(anchor, INDEX_HELPER.replace("{marker}", MARKER) + "\n\n" + anchor, 1)
    target = (
        "    if not row:\n"
        '        return {"available": False, "index_name": HNSW_INDEX_NAME, "valid": False, "ready": False}\n'
    )
    if target not in text:
        _fail("vector_index_management_service.py: 缺失分支已变，无法注入兼容逻辑")
    text = text.replace(
        target,
        "    if not row:\n"
        "        # 原 with 块已关闭连接，这里新开一个\n"
        "        with engine.connect() as compat_conn:\n"
        "            compat_state = _compat_ann_index_state(compat_conn)\n"
        "        if compat_state:\n"
        "            return compat_state\n"
        '        return {"available": False, "index_name": HNSW_INDEX_NAME, "valid": False, "ready": False}\n',
        1,
    )
    print("[patch] vector_index_management_service.py: 已注入 bit 汉明索引兼容分支")
    return text, True


def run() -> int:
    for path, fn in ((VECTOR_STORE, patch_vector_store), (INDEX_STATE, patch_index_state)):
        if not path.exists():
            _fail(f"缺少文件 {path}")
        text = path.read_text(encoding="utf-8")
        new_text, changed = fn(text)
        if changed:
            path.write_text(new_text, encoding="utf-8")
            print(f"[patch] 已写入 {path.relative_to(ROOT)}")
        else:
            print(f"[patch] 已存在，跳过 {path.relative_to(ROOT)}")
    print("[patch] done")
    return 0


if __name__ == "__main__":
    sys.exit(run())
