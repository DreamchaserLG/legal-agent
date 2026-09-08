# 法律 Agent 混合检索交付说明

更新时间：2026-09-08

## 交付范围

本阶段交付的是法律 Agent 的检索底座，不是完整多工具 Agent。已经完成：

- RAG chunk 构建
- PostgreSQL full-text 关键词检索
- pgvector 向量存储
- HNSW + cosine 向量索引
- 结构化过滤
- 关键词 + 向量混合检索
- 本地 rerank
- 检索延迟基准

## 检索链路

```text
用户问题
-> 轻量法律查询规划
-> law/case 分区
-> 关键词检索
-> 向量检索
-> RRF 融合
-> 本地法律 rerank
-> RAG 证据包
```

## PostgreSQL 关键词检索

当前 PostgreSQL 分支使用：

- `search_vector`
- `GIN` 索引
- `websearch_to_tsquery`
- 安全 `to_tsquery` fallback

已移除正文 `LOWER(text_content) LIKE` 全表扫描。SQLite fallback 仍保留 LIKE，方便轻量本地 demo。

## 向量检索

当前向量表：

```text
rag_chunk_embeddings
```

索引：

```text
idx_rag_chunk_embeddings_vector
```

配置：

```text
index_type: hnsw
operator: vector_cosine_ops
m: 16
ef_construction: 64
ef_search: 80
```

## 当前性能

基准输出：

```text
data/eval/retrieval_benchmark_20260908.json
```

摘要：

- 向量检索：约 19-105 毫秒
- 关键词检索：约 112-894 毫秒
- 混合检索：约 335-926 毫秒

## 使用命令

```powershell
python rag_manage.py --json status
python rag_manage.py --json vector-status
python rag_manage.py search "federal regulations" --module canada --source law --limit 5 --jurisdiction Canada --document-type statute
python rag_manage.py hybrid-search "contract good faith appeal" --module canada --source canada --limit 5
python scripts\benchmark_retrieval.py --repeat 2 --limit 5 --output data\eval\retrieval_benchmark_20260908.json
```

## 当前限制

- 当前 embedding 为 hash fallback，不代表真实语义检索质量。
- 真实生产效果需要切换 BGE-M3、Qwen embedding 或其他 OpenAI-compatible embedding 服务后重建向量。
- 法规 XML 仍是 document/chunk 粒度，后续应升级到 section-level chunking。
- 当前不是正式法律意见系统，回答必须展示证据和不确定性。
