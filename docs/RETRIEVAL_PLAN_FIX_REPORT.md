# 向量检索执行计划修复报告

日期：2026-11-04  
范围：本机 `legal_demo`，PostgreSQL 17.9 + pgvector 0.8.1  
固定查询：`Ontario sentencing principles prior criminal record intoxication`  
边界：未重建/删除 HNSW，未修改向量，未回填 `legal-context-v2`，未迁移或部署，未写入业务表。

## 1. P0 基线

修复前，`vector_search()` 先连接两张 658 万行级表，再按向量距离排序。固定查询使用原 SQL、`LIMIT 96`、数据库默认参数复现时，执行计划原文为：

```text
Limit
  -> Sort
       Sort Key: ((rce.embedding_vector <=> '[1024 dimensions omitted]'::vector))
       -> Gather
            -> Parallel Hash Join
                 Hash Cond: ((rc.id = rce.chunk_id) AND (rc.content_hash = rce.content_hash))
                 -> Parallel Seq Scan on rag_chunks rc
                 -> Parallel Hash
                      -> Parallel Seq Scan on rag_chunk_embeddings rce
```

本轮复现超过 60 秒仍未完成，主动终止只读客户端；应用的 `statement_timeout=12000` 会更早中断。独立全程基线的同形 SQL 实测为 **306,380.6 ms**。这解释了应用为何静默降级为纯关键词，而不是使用已经建好的 HNSW。

修复前参数保持不变：

```text
shared_buffers=128MB
effective_cache_size=4GB
random_page_cost=4
work_mem=4MB
maintenance_work_mem=64MB
```

## 2. P0 结构修复

修复后的实际查询先在 `rag_chunk_embeddings` 上完成有界 ANN，再按主键连接向量与文本表。只有 HNSW 局部索引谓词进入 ANN 子查询；新鲜度、模块、`source_code` 和结构化条件全部保留在外层：

```sql
WITH ann AS MATERIALIZED (
    SELECT chunk_id,
           embedding_vector <=> CAST(:query_vector AS vector) AS distance
    FROM rag_chunk_embeddings
    WHERE embedding_provider = :embedding_provider
      AND embedding_model = :embedding_model
      AND embedding_vector IS NOT NULL
    ORDER BY embedding_vector <=> CAST(:query_vector AS vector)
    LIMIT :ann_limit
)
SELECT rc.*,
       1 - (rce.embedding_vector <=> CAST(:query_vector AS vector)) AS score
FROM ann a
JOIN rag_chunk_embeddings rce ON rce.chunk_id = a.chunk_id
JOIN rag_chunks rc ON rc.id = a.chunk_id
WHERE rc.content_hash = rce.content_hash
  -- outer business filters
ORDER BY a.distance
LIMIT :outer_limit;
```

每次查询在当前事务中设置：

```sql
SET LOCAL hnsw.iterative_scan = relaxed_order;
SET LOCAL hnsw.max_scan_tuples = 20000;
SET LOCAL hnsw.ef_search = max(configured_ef_search, ann_limit);
```

第一次 `ann_limit=max_items*4`；外层过滤或去重后不足时，只执行一次 `ann_limit*2` 的补查，不存在无界循环。

修复后的计划原文（固定查询、`ann_limit=96`）为：

```text
Limit
  CTE ann
    -> Limit
         -> Index Scan using idx_rag_chunk_embeddings_active_hnsw
              on rag_chunk_embeddings
  -> Sort
       Sort Key: a.distance
       -> Nested Loop
            -> Nested Loop
                 -> CTE Scan on ann a
                 -> Index Scan using rag_chunk_embeddings_pkey
                      on rag_chunk_embeddings rce
            -> Index Scan using rag_chunks_pkey on rag_chunks rc
```

计划守护从实际应用 SQL 的公共构造函数取得 SQL，并对 `EXPLAIN (FORMAT JSON)` 断言：包含 `idx_rag_chunk_embeddings_active_hnsw`，不含 `Seq Scan`、`Parallel Seq Scan` 或 `Hash Join`。

## 3. P0 实测结果

`python scripts/verify_retrieval_plan.py --runs 20` 的实际输出：

```text
plan_uses_hnsw=true
plan_has_seq_scan=false
p50_ms=9.563 p95_ms=14.769 rows=96
```

| 指标 | 修复前 | P0 修复后 | 结果 |
|---|---:|---:|---|
| 同形 SQL 全程耗时 | 306,380.6 ms | P50 9.563 ms | 约 32,038 倍 |
| P95 | 不适用（单次 306 秒） | 14.769 ms | `<150 ms` 通过 |
| 返回候选 | 超时前不可用 | 96 | `>=24` 通过 |
| HNSW | 否 | 是 | 通过 |
| 大表顺序扫描 | 2 次 | 0 次 | 通过 |

注意：306,380.6 ms 是独立全程基线，9.563 ms 是 20 次暖缓存 P50；倍数用于量级说明，不表示严格同统计量的置信区间。

测试结果：

```text
python -m unittest tests.test_vector_search_query_plan -v
Ran 3 tests ... OK

python -m unittest discover -s tests -v
Ran 58 tests ... OK
```

额外覆盖了 `limit=40` 且配置 `ef_search=80` 时至少返回 40 个去重结果，以及不可能命中过滤条件时恰好执行两次（首次 + 一次补查）。

## 4. P1 参数调整

P0 提交时尚未执行。必须在 P0 独立提交后再调整，并在本节补充重启后的实际值、相同脚本指标，以及会话级 `random_page_cost=4` 的计划回退验证。

## 5. 未解决问题与本轮边界

- `context-v1` 共 60,025 块，其中 60,000 条虽有向量但哈希一致为 0，另 25 条无向量；本轮按约束未重新编码，仍不可由新鲜度过滤后的语义路径检索。
- 当前 HNSW 为 `m=16, ef_construction=64`。小样本召回多数为 1.00，但独立样本曾出现 Recall@10=0.70（`ef=400` 时 0.80），召回余量仍需更大正式评测集确认。
- 本轮不重建 50.2GB HNSW，不做 halfvec 索引，不写业务表。
- 延迟为单连接暖缓存本机结果，尚未覆盖并发连接池和 I/O 竞争。
