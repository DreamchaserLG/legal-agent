# 法律 Agent 项目进度与路线图

更新时间：2026-09-08

## 当前定位

项目当前是一个加拿大法律智能 Agent demo，重点验证：

- 开放法律数据入库
- PostgreSQL 结构化存储
- pgvector/HNSW 向量数据库
- RAG 证据上下文
- 混合检索
- Qwen 模型调用
- 风险评估样本沉淀

项目还不是成熟企业级法律 Agent。正式产品仍需要权限审计、引用逐条校验、人工评测集、真实 embedding、风险训练集、MCP/A2A 工具调度和更严格的 trace。

## 已完成能力

### 1. 数据库与向量索引

- 已切换 PostgreSQL。
- 已启用 pgvector。
- 已创建 HNSW cosine 索引。
- 已增加向量状态检查。
- 已支持结构化过滤。

当前索引：

```text
idx_rag_chunk_embeddings_vector
USING hnsw (embedding_vector vector_cosine_ops)
m=16
ef_construction=64
ef_search=80
```

### 2. 开放数据入库

- A2AJ demo：8 条案例、8 条法规。
- Justice Canada `laws-lois-xml`：15599 个 XML 文件已导入。
- 加拿大结构化同步：12568 条规则、8 个案例、4 条关系。

### 3. RAG 与检索

- RAG chunks：203583
- pgvector embeddings：203583
- 关键词检索：PostgreSQL `tsvector + GIN`
- 向量检索：pgvector HNSW cosine
- 混合检索：RRF + 本地法律 rerank

当前基准：

- 向量检索：约 19-105 毫秒
- 关键词检索：约 112-894 毫秒
- 混合检索：约 335-926 毫秒

### 4. Qwen 模型调用

- 本地 `.env` 已配置 `LLM_PROVIDER=custom`。
- 模型为 `qwen3.8-27b`。
- `python llm_healthcheck.py` 已通过。
- API key 只保存在 `.env`，不写入 Git。

### 5. 风险评估闭环

- 已新增 `risk_assessment_samples`。
- 已新增 `risk_feedback_labels`。
- 已新增样本读取和反馈写入 API。
- 已写入 3 条 demo 风险样本和 3 条人工反馈标签。
- 预测运行后会沉淀风险样本，方便后续训练。

## 尚未完成

1. 真实语义 embedding：当前为 hash fallback。
2. A2AJ parquet 批量导入：当前网络访问 HuggingFace 超时。
3. 法规 section-level chunking：当前仍是较粗 document/chunk 粒度。
4. 风险模型训练：当前只有样本沉淀和反馈入口。
5. MCP/A2A 工具调度：当前只保留扩展边界。
6. 引用逐条校验：后续需要正式 citation validator。
7. 权限与审计：后续需要用户、案件空间、操作 trace 和审批节点。

## 下一阶段建议

1. 准备真实 embedding 服务或本地模型，重建向量。
2. 在网络畅通环境完成 A2AJ parquet 批量导入。
3. 把 `laws-lois-xml` 解析升级到 section-level。
4. 建立 50-200 条人工标注检索评测集。
5. 建立风险评估标签规范，并把 demo 样本扩展为可训练数据。
6. 增加 MCP 工具注册、任务 trace、权限控制和人工审批。
7. 增加引用校验和证据不足降级策略。
