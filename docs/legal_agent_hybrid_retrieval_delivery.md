# 法律 Agent 检索底座升级交付文档

## 1. 交付范围

本次交付完成的是法律 Agent 系统升级中的第一阶段：将原有关键词/全文 RAG 检索扩展为可运行的 embedding、向量索引和 Hybrid Search 检索底座。

本次没有重写前端页面，也没有把现有 `agent_service.py` 完全改造成严格 ReAct Agent。当前交付重点是先补齐后续 Agent 编排所依赖的检索能力。

## 2. 已完成内容

### 2.1 Embedding 服务

新增文件：

```text
app/service/embedding_service.py
```

能力：

- 支持 OpenAI-compatible `/embeddings` 接口。
- 支持 `openai`、`custom`、`hash` 三种 provider。
- 默认使用 `hash` 本地确定性 embedding，保证无外部模型时也能跑通索引流程。
- `hash` embedding 仅用于验证向量索引和 Hybrid Search 管线是否可运行，不代表真实语义向量能力。
- 后续接入本地 Qwen embedding 时，只需要配置 OpenAI-compatible embedding 服务地址。

关键配置：

```dotenv
EMBEDDING_PROVIDER=hash
EMBEDDING_MODEL=local-hash-embedding
EMBEDDING_DIMENSION=384
EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
EMBEDDING_BATCH_SIZE=32
```

如果使用本地 Qwen embedding 服务，推荐配置示例：

```dotenv
EMBEDDING_PROVIDER=custom
EMBEDDING_MODEL=Qwen3-Embedding
EMBEDDING_BASE_URL=http://127.0.0.1:8001/v1
EMBEDDING_API_KEY=local-key
EMBEDDING_DIMENSION=1024
```

### 2.2 向量索引与检索服务

新增文件：

```text
app/service/vector_store_service.py
```

能力：

- 自动创建 `rag_chunk_embeddings` 表。
- PostgreSQL 环境下优先尝试启用 `pgvector`。
- 如果 `pgvector` 不可用，自动退回 JSON embedding + Python 余弦相似度。
- SQLite 环境下使用 JSON embedding 存储和本地余弦相似度计算。
- 支持按 `module`、`source_filter` 过滤向量检索范围。

新增能力入口：

```text
rebuild_chunk_embeddings()
vector_search()
get_vector_status()
```

### 2.3 Hybrid Search 服务

新增文件：

```text
app/service/hybrid_retrieval_service.py
```

能力：

- 同时调用原有 `rag_search` 和新增 `vector_search`。
- 使用 RRF 思路融合关键词/全文检索和向量检索结果。
- 当 `EMBEDDING_PROVIDER=hash` 时，向量通道会作为诊断通道，不再把纯向量命中的噪声证据推入最终排序。
- 证据候选会过滤只命中 `Ontario`、`Canada`、`law`、`case`、`issue` 等泛化词的结果，降低地域词造成的误召回。
- 输出每条证据的来源通道：

```text
search_channels = ["lexical", "vector"]
```

- 保留 `lexical_score`、`vector_score`、`hybrid_score`，方便后续做检索质量分析。
- 对法规、案例、标题命中做轻量本地 authority boost。

关键配置：

```dotenv
RAG_HYBRID_ENABLED=true
RAG_LEXICAL_WEIGHT=0.55
RAG_VECTOR_WEIGHT=0.45
RAG_LEXICAL_CANDIDATE_LIMIT=40
RAG_VECTOR_CANDIDATE_LIMIT=40
```

### 2.4 接入现有 RAG 上下文

修改文件：

```text
app/service/rag_service.py
```

变化：

- `build_rag_context()` 在 `RAG_HYBRID_ENABLED=true` 时优先使用 `hybrid_search()`。
- 如果 hybrid 检索失败，会自动回退到原始 `rag_search()`。
- `get_rag_status()` 增加 `hybrid_enabled` 和 `vector` 状态信息。

影响：

- 现有案情分析、预测、agent-chat 中使用的 `rag_context` 会自动受益于 Hybrid Search。
- 不需要立即重写现有业务流程。

### 2.5 API 入口

修改文件：

```text
app/api/routes.py
```

新增接口：

```text
GET  /api/rag/vector-status
GET  /api/rag/vector-search?query=...&module=canada&source=all&limit=8
GET  /api/rag/hybrid-search?query=...&module=canada&source=all&limit=8
POST /api/rag/rebuild-vectors?source=canada&module=canada
POST /api/rag/rebuild-hybrid?source=canada&module=canada
```

权限策略沿用现有规则：

- 查询类接口需要登录用户。
- 重建索引类接口需要管理员。

### 2.6 CLI 管理命令

修改文件：

```text
rag_manage.py
```

新增命令：

```powershell
python rag_manage.py vector-status
python rag_manage.py rebuild-vectors --source canada --module canada
python rag_manage.py vector-search "Ontario tenant eviction repair issue" --module canada --limit 8
python rag_manage.py hybrid-search "Ontario tenant eviction repair issue" --module canada --limit 8
python rag_manage.py rebuild-hybrid --source canada --module canada
```

推荐本地验收顺序：

```powershell
python rag_manage.py rebuild --source canada
python rag_manage.py rebuild-vectors --source canada --module canada
python rag_manage.py hybrid-search "Ontario tenant eviction repair issue" --module canada --limit 8
```

也可以一步执行：

```powershell
python rag_manage.py rebuild-hybrid --source canada --module canada
```

### 2.7 配置样例

修改文件：

```text
.env.example
.env.sqlite.example
```

已补充 Hybrid Search 和 Embedding 相关配置，便于 Docker/Linux/SQLite 场景统一部署。

## 3. 验证结果

已执行：

```powershell
python rag_manage.py --help
```

结果：

```text
status: CLI command registration OK
commands: vector-status, rebuild-vectors, vector-search, hybrid-search, rebuild-hybrid
```

已执行不写 `.pyc` 的语法检查：

```powershell
compile(source, path, "exec")
```

通过文件：

```text
app/service/embedding_service.py
app/service/vector_store_service.py
app/service/hybrid_retrieval_service.py
app/service/rag_service.py
app/api/routes.py
rag_manage.py
```

已执行基础 embedding 验证：

```text
provider=hash
dimension=384
vector_norm=1.0
```

说明：

- `python -m compileall` 因当前工作区已有 `__pycache__` 写权限问题失败，失败点是 `.pyc` 写入权限，不是 Python 语法错误。
- 已使用不写缓存文件的 `compile()` 完成替代验证。
- 已用 `Ontario tenant eviction repair issue` 做 smoke test。`hash` embedding 模式下，系统能完成 hybrid 检索流程，但最终结果主要依赖 lexical 通道；要获得真正语义召回，需要切换到 Qwen/OpenAI embedding。

## 4. 当前系统能力变化

升级前：

```text
案情文本
-> 关键词/全文检索
-> rag_context
-> LLM 分析/预测/聊天
```

升级后：

```text
案情文本
-> 关键词/全文检索
-> 向量检索
-> RRF 融合
-> hybrid evidence pack
-> rag_context
-> LLM 分析/预测/聊天
```

这使项目从“关键词 RAG 原型”推进到“具备 Hybrid Search 底座的法律 Agent 原型”。

## 5. 仍未完成的边界

以下内容不属于本次交付，仍应作为后续阶段推进：

1. 严格 ReAct Agent 编排。

当前仍是业务流程式 Agent，后续应新增：

```text
app/service/legal_agent_orchestrator.py
```

并显式拆成：

```text
parse_case
retrieve_laws
retrieve_cases
rerank_evidence
verify_evidence
predict_outcome
build_final_answer
```

2. Agent trace 审计表。

建议后续新增：

```text
agent_traces
agent_trace_steps
retrieval_runs
retrieval_results
```

3. 专业 reranker。

当前 Hybrid Search 使用 RRF + 轻量本地 boost。后续可以加入：

```text
cross-encoder reranker
LLM reranker
法律权威性 reranker
```

4. 检索评测集。

建议准备 20-50 条国外法律案情样例，评估：

```text
法规召回率
案例召回率
top-k 命中率
证据引用准确率
幻觉率
```

## 6. 后续推荐工作

下一阶段建议按以下顺序推进：

```text
1. 使用真实 Qwen/OpenAI embedding 重建向量索引
2. 构建 20-50 条法律检索评测集
3. 增加 retrieval_runs / retrieval_results 记录
4. 实现 reranker_service.py
5. 实现 legal_agent_orchestrator.py
6. 将 analyze / predict 切换到显式 Agent trace
7. 在前端展示检索证据、融合分数和 Agent 步骤
```

## 7. 交付结论

本次交付已经完成“严格软件开发流程”中的检索底座升级：配置、服务、API、CLI、回退策略和交付说明均已落地。

项目目前可以被描述为：

```text
具备 Hybrid Search 检索底座、证据约束 RAG 上下文和多模型接口能力的法律 Agent 原型系统。
```

但还不能完全表述为：

```text
完整 ReAct 法律 Agent 系统。
```

如果需要达到完整 ReAct Agent 项目标准，下一阶段应重点完成 Agent 编排、trace 审计、reranker 和评测集。
