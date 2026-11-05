# 第三轮过滤检索修复报告

日期：2026-11-05

基线：`c606579` 及 DeepSeek 第二轮验收

边界：只读评测；未重建/删除 HNSW，未回填向量，未迁移，未写业务表。

## 1. 根因

`c606579` 在强过滤不足时把全局 HNSW 候选从 96 扩展到 20,000，再到外层过滤 `source_kind='law'`。该方案有两个结构性问题：

1. 全局近邻中法条密度不受保证。真实 Ontario eviction 查询在 20,000 个候选中只有 5 条法条。
2. 20,000 个全局候选会加载大量 HNSW/heap 页面，冷路径达到 3.5–12.7 秒。

继续增加 `ann_limit` 只会放大 I/O，不能使全局索引变成来源内索引。因此本轮没有再次提高候选上限。

## 2. 修复设计

强法条/结构化过滤改为：

```text
中文自由文本
  -> 加拿大法律术语翻译与查询规划
  -> requested source 内的 GIN 全文候选（focused + domain anchor，最多 500）
  -> 仅对候选做 BGE-M3 精确距离重排
  -> 来源/文档去重
  -> 1.5 秒超时时只补一次小 ANN，不执行 20,000 扫描
```

普通 `all/canada/case` 查询继续使用原 HNSW ANN，不改变已经验证的 Recall@10 路径。来源内计划使用 `idx_rag_chunks_vector`、`idx_rag_chunks_source_kind`、`rag_chunks_pkey` 和 `rag_chunk_embeddings_pkey`，无 Seq Scan。

此外完成三项扩展：

- 补齐 unconscionability、undue influence、rescission、sentencing、intoxication、prior criminal record、landlord own use、good faith 等中英法律概念。
- Hybrid 的 case/law × lexical/vector 四个任务并行执行，并输出 `partition_retrieval_wall`。
- 重排增加司法辖区权威加权，并保证最终证据中保留查询计划要求的 case/law 来源。

## 3. 同口径结果

### 3.1 法条自由文本

| 查询 | c606579 返回 | 本轮策略 | c606579 查询 | 本轮查询 | 本轮来源重排 SQL（暖态） |
|---|---:|---:|---:|---:|---:|
| Ontario eviction | 5/24 | 0 个不匹配向量 + 24 条 Ontario 词法兜底 | ANN 96 → 20,000 | source rerank × 1 | 117.7 ms |
| criminal sentencing | 24/24 | 24/24 | ANN 96 → 20,000 | source rerank × 1 | 300.5 ms |
| contract rescission | 24/24 | 24/24 | ANN 96 → 20,000 | source rerank × 1 | 117.7 ms |

三条查询的纯向量填充率为 66.7%，达到第二轮报告建议的 60% 门槛；Ontario 路径在 1.5 秒内显式返回 `lexical_fallback_required`，而不是用联邦/其他省法规伪造 24/24。Hybrid 的有效法条覆盖仍为 24。运行时三条查询均不再触发 20,000 ANN。Ontario 同机首次来源重排 SQL 为 182.8 ms；进程首个端到端约 8.7 秒主要是 BGE-M3 模型首次加载，不是数据库扫描。模型加载后的 criminal/contract 总耗时约 604.1/264.0 ms。

Ontario 的 `jurisdiction_priority_rows=0` 是关键质量诊断：当前 freshness join 中没有可用于该查询的 Ontario 法条向量。代码因此丢弃 24 条看似相近、实则跨辖区的向量结果，转由法条全文索引返回 `Residential Tenancies Act, 2006`。

### 3.2 Hybrid 与结果质量

Ontario 查询连续三次（模型已加载）结果：

| 次数 | Hybrid 总耗时 | 四任务检索墙钟 | case/law 向量 | 结果来源 |
|---|---:|---:|---:|---|
| 暖态 1 | 938.8 ms | 226.5 ms | case向量24 / law词法兜底24 | case + law |
| 暖态 2 | 906.8 ms | 195.6 ms | case向量24 / law词法兜底24 | case + law |

同一代码在并行前的 Ontario 暖态为约 1,486 ms，现为约 907–939 ms，下降约 37–39%。质量方面：

- Top-1 从非 Ontario 的 `1994 NSCA 124` 提升为 `2023 ONCA 789`。
- Top-8 强制保留 Ontario `Residential Tenancies Act, 2006`，避免双通道判例挤掉法条。
- 每个结果新增 `rerank_signals.jurisdiction_match`，可解释司法辖区加权。

### 3.3 回归与计划

- 全量测试：64/64（加入超时守护后最终为 65 项）。
- 三条真实中文自由文本法条测试覆盖“来源内24/24”与“明确词法兜底”两条正确路径。
- 1.5 秒来源重排超时会被捕获，最多再执行一次 `ann_limit=96` 的小 ANN。
- 来源内 EXPLAIN：`has_seq_scan=false`。
- 普通 HNSW 计划守护继续通过，未改变全局 ANN 的 ef=500 召回修复。

原始结果：`outputs/acceptance/filtered_rerank_v3.json`。复现：

```powershell
python scripts\eval_filtered_rerank_v3.py
python -m unittest discover -s tests -v
```

## 4. 仍未掩盖的限制

1. 这是“来源内全文候选 + 精确向量重排”，不是法条专用 HNSW。它在不改业务表、不建第二个大索引的约束下解决过滤饥饿和冷扫描。
2. 部分 Ontario 法条块仍属于已知的 context-v1 哈希不一致集合，不能通过当前 fresh-vector join。最终 Hybrid 会由词法通道补回有效法条；本轮没有违规回填。
3. 若以后允许 schema 变更，可把 `source_kind` 冗余到 embedding 表并离线评测互斥 partial HNSW。需要先做磁盘、召回和构建窗口评估，不能由应用自动创建。
4. 进程首个查询仍需加载 BGE-M3。若要优化用户感知冷启动，应在服务启动后的受控预热阶段加载模型，而不是通过扩大数据库扫描掩盖。
