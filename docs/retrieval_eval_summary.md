# 法律检索评测摘要

更新时间：2026-09-07

## 评测范围

本评测用于验证当前加拿大法律检索链路：

- PostgreSQL `tsvector + GIN` 关键词检索
- pgvector HNSW cosine 向量检索
- 结构化过滤
- 关键词 + 向量混合检索
- 本地法律 rerank
- RAG 证据上下文

## 当前基准

基准输出：

```text
data/eval/retrieval_benchmark_20260908.json
```

执行命令：

```powershell
python scripts\benchmark_retrieval.py --repeat 2 --limit 5 --output data\eval\retrieval_benchmark_20260908.json
```

结果摘要：

- 向量检索：约 19-105 毫秒
- 关键词检索：约 112-894 毫秒
- 混合检索：约 335-926 毫秒

## 已修复问题

首次全量导入后，关键词检索和混合检索存在 6-19 秒级慢查询。原因是 PostgreSQL 分支同时使用 `tsvector` 和正文 `LOWER(text_content) LIKE` 兜底，混合检索还把规划扩展词喂给关键词通道，导致超宽召回和全库 rank。

修复后：

- PostgreSQL 关键词检索改为 `tsvector + GIN`。
- 移除正文 LIKE 全表扫描。
- 混合检索关键词通道使用原始查询。
- 混合检索向量通道保留扩展查询。

## 现有限制

- 当前 embedding 为 hash fallback，只验证管线和索引，不代表最终语义质量。
- 当前评测是基础延迟基准，不是完整法律检索质量评测。
- 后续需要加入人工标注的法律问题评测集、case/law 命中率、引用准确率和风险预测准确率。
