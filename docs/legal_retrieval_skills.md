# 法律检索技能说明

本项目把法律检索技能实现为轻量内部规则，避免在当前 demo 阶段引入过重运行时依赖。后续如果需要，可把这些规则升级为独立 MCP 工具或 Agent 技能。

## 当前内置思路

- 探索-验证-记忆：检索时保留查询意图、证据来源和可复用上下文。
- 法律引用优先：优先匹配法规标题、引用号、章节号、法院层级和权威片段。
- 法律实体抽取：从输入中抽取案例引用、法规标题、section、法院、辖区和争议焦点。
- 权威感知 rerank：对更高法院层级、明确法规来源、case-rule 关联更强的结果加权。
- 引用校验：生成回答时标记缺少本地证据支持的案例、法规或引用。
- 证据质量门控：证据不足时降低预测置信度，避免高置信度编造。

## 代码入口

- `app/service/legal_skill_service.py`：技能目录、法律实体抽取、领域关键词扩展、结果验证和 rerank 信号。
- `app/service/evidence_quality_service.py`：预测证据质量评分、未支持引用检测、置信度上限。
- `app/service/analysis_service.py`：分析阶段应用增强关键词。
- `app/service/search_service.py`：搜索阶段应用增强关键词和结果验证。
- `app/service/rag_service.py`：RAG chunk、关键词检索、结构化过滤。
- `app/service/vector_store_service.py`：向量检索和 pgvector/HNSW。
- `app/service/hybrid_retrieval_service.py`：关键词 + 向量混合检索。
- `app/templates/predict.html`：展示预测理由和证据质量。

## 当前限制

- 当前还不是完整 ReAct Agent。
- 当前 rerank 是本地规则，不是 cross-encoder。
- 当前 hash embedding 只用于 demo 管线验证。
- 后续需要接入真实语义 embedding、引用校验器、人工评测集和 MCP 工具调度。
