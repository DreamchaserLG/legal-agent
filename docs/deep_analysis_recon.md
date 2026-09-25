# 案情分析深度解析改造阶段 0 勘察

勘察时间：2026-09-24
范围：按 `CODEX_TASK_DEEP_ANALYSIS.md` 阶段 0 要求读取指定服务、路由、模板和评测脚本。当前未修改业务代码。

## 1. `legal_query_planner_service.py`

### 当前 `QueryPlan`

当前不存在 Pydantic `QueryPlan`。`build_legal_query_plan(query, keywords=None)` 返回普通 `dict`，实际字段为：

| 字段 | 类型 | 来源/含义 |
| --- | --- | --- |
| `original_query` | `str` | `repair_text(query)` |
| `keywords` | `list[str]` | 调用方传入关键词去重 |
| `jurisdictions` | `list[str]` | `_JURISDICTION_HINTS` 规则匹配 |
| `domains` | `list[str]` | `_DOMAIN_HINTS` 规则匹配 |
| `issues` | `list[str]` | 仅在命中 `eviction/termination/repair/...` 等词时追加领域提示 |
| `meaningful_terms` | `list[str]` | 英文 token 过滤通用词 |
| `law_query` | `str` | `jurisdictions + domains + issues + law_expansions + base_terms` 拼接 |
| `case_query` | `str` | `jurisdictions + domains + issues + case_expansions + base_terms` 拼接 |
| `preferred_sources` | `list[str]` | 租赁/修缮类优先 `case, law`，其他默认 `law, case` |

### 当前争点抽取

不是语义争点推理，而是规则关键词触发：输入被 `_terms()` 提取英文 token，只有命中 `eviction`、`termination`、`repair`、`repairs`、`habitability`、`maintenance` 时才把 `_DOMAIN_HINTS` 结果放进 `issues`。没有 `supporting_facts`、`legal_domain`、双方立场、置信度。

### 当前检索式生成

`law_query` 和 `case_query` 已分开，但本质是扩展词和原始查询的字符串拼接。没有基于结构化事实、争点、数据库先验候选生成，也没有 `prior_rule_candidates` / `prior_case_candidates`。

### 调用方

`build_legal_query_plan` 被以下路径消费：

| 调用方 | 用法 |
| --- | --- |
| `app/service/hybrid_retrieval_service.py` | 构建 `structured_query`，读取 `law_query`、`case_query`、`preferred_sources`、`meaningful_terms`、`domains`、`issues` |
| `app/service/legal_ingestion_pipeline_service.py` | 用 `meaningful_terms`、`domains`、`issues` 补充结构化检索词 |
| `app/service/reranker_service.py` | 用 `domains`、`issues`、`preferred_sources`、`meaningful_terms` 做证据重排加分 |

兼容要求：阶段 A 即使迁移到 Pydantic，也必须继续暴露上述旧字段，或通过 `model_dump()` 生成等价 dict。

## 2. `multi_agent_service.py`

### 当前 DAG / 路由

当前没有外部 LangGraph 依赖，而是固定函数顺序：

| intent | 节点 |
| --- | --- |
| simple | `single_rag_agent`, `rule_engine` |
| complex | `query_agent`, `retrieval_agent`, `authority_agent`, `verification_agent`, `risk_agent`, `arbitration_agent` |

`run_multi_agent_analysis()` 对 complex intent 依次执行 `_query_agent`、`_retrieval_agent`、`_authority_agent`、`_verification_agent`、`_risk_agent`，最后 `_arbitration_agent`。simple intent 由 `single_rag_agent` 包装查询、检索和引用核验。

### `LegalGraphState`

实际字段：

`query`、`intent`、`module`、`filters`、`tenant_id`、`case_acl`、`max_steps`、`timeout_seconds`、`token_budget`、`started_at`、`steps`、`query_plan`、`evidence`、`authority_links`、`citation_verification`、`risks`、`blocked_reasons`、`review_required`。

### `risk_agent`

`_risk_agent` 固定生成四类中文风险：实体风险、程序风险、证据风险、执行风险。每条通过 `_risk_binding` 绑定最多 3 条法规和 3 条案例证据。置信度为规则计算，证据不足时 `binding_status="insufficient_evidence"`，但当前不是目标文档中的 `RiskPrediction` schema。

### `arbitration_agent`

`_arbitration_agent` 当前只做确定性阻断：

- 步数超过 `max_steps` 追加阻断原因。
- 无证据时禁止输出实体结论。
- 跨法域但未接入目标知识库时阻断。
- 引用缺少可核验元数据时设置 `review_required=True`。

当前没有 `QualityGateResult`，也没有按阈值计算 `citation_traceability`、`rule_currency`、`evidence_completeness`、`low_confidence_ratio`。

### `AGENT_TOOL_ALLOWLIST`

实际内容：

| agent | tools |
| --- | --- |
| `single_rag_agent` | `query_understanding`, `hybrid_search`, `citation_verification` |
| `query_agent` | `query_understanding` |
| `retrieval_agent` | `hybrid_search` |
| `authority_agent` | `authority_links` |
| `verification_agent` | `citation_verification` |
| `risk_agent` | `risk_annotation` |
| `arbitration_agent` | `rule_arbitration` |
| `hearing_orchestrator` | `hearing_state` |
| `judge_agent` | `issue_fact_extraction`, `evidence_verification` |
| `opposing_counsel_agent` | `authority_links`, `evidence_verification` |
| `evidence_agent` | `evidence_verification`, `authority_links` |
| `evaluation_agent` | `risk_annotation`, `citation_verification` |
| `report_agent` | `rule_arbitration`, `citation_verification` |

### 审计表

`ensure_multi_agent_tables()` 创建 `multi_agent_runs`，字段包括 `graph_version`、`tenant_id`、`user_id`、`intent`、`query_text`、`status`、`state_json`、`duration_ms`、`created_at`。阶段 C 可复用该表记录质量门禁结果。

## 3. `risk_assessment_service.py`

当前风险标注用于训练样本沉淀，不是深度解析输出。

### 风险等级逻辑

`_risk_level(prediction)` 读取：

- `prediction.evidence_status`
- `prediction.data_readiness.status`
- `prediction.evidence_quality.status`
- `prediction.reliability_warnings`
- `prediction.risk_points`
- `prediction.caveats`
- `prediction.confidence`

规则：证据不足、数据准备不足、证据质量薄弱、多个 warning/risk、置信度低会提高风险等级。

### 样本表

`risk_assessment_samples` 字段：

`id`、`agent_run_id`、`agent_prediction_id`、`module_code`、`input_text`、`risk_level`、`confidence`、`evidence_status`、`evidence_quality_status`、`jurisdiction`、`requested_relief`、`disputed_issues_json`、`retrieved_evidence_json`、`prediction_json`、`label_status`、`created_at`。

PostgreSQL 下 JSON 字段为 `JSONB`；SQLite 下为 `TEXT`。

### 反馈表

`risk_feedback_labels` 字段：

`id`、`sample_id`、`human_risk_level`、`human_outcome`、`human_notes`、`label_json`、`created_at`。

`record_risk_feedback_label()` 插入反馈后会把样本 `label_status` 更新为 `labeled`。

## 4. `hybrid_retrieval_service.py`

### 入参

`hybrid_search(query, *, keywords=None, module="canada", source_filter="all", limit=None, lexical_limit=None, vector_limit=None, filters=None)`。

### 出参

空查询返回：

```json
{"query": "", "items": [], "total": 0, "status": "empty_query"}
```

正常返回字段：

`query`、`keywords`、`structured_query`、`module`、`source_filter`、`filters`、`items`、`total`、`status`、`strategy`、`channels`。

`structured_query` 当前就是 `build_legal_query_plan()` 的 dict。

### 检索流程

1. `build_legal_query_plan()` 生成结构化查询。
2. `_retrieval_partitions()` 按 `source_filter` 和 `preferred_sources` 分成 law/case 两路。
3. 每路同时调用 `rag_search(clean_query, ...)` 和 `vector_search(partition_query, ...)`。
4. 使用 RRF 融合 lexical/vector，hash embedding 时禁用向量权重。
5. 用 `rerank_legal_evidence(clean_query, fused_items, plan=query_plan)` 重排。

### 正式关联查询

`hybrid_retrieval_service.py` 本身不直接查询 `case_rule_relations`。正式关联主要在：

- `app/service/legal_data_service.py`：`build_canada_case_rule_packet()`、`_fetch_case_rule_matches()`、`_keyword_relation_rows()`、`list_case_rule_relations()`。
- `app/service/module_service.py`：`build_module_packet()` 整合 `relevant_laws` 和 `case_law_rows`。
- `app/service/multi_agent_service.py`：`_authority_agent()` 直接查 `canada_case_law_links` 和 `canada_laws`，不是 `case_rule_relations`。

## 5. `routes.py` 案情分析入口

### 页面入口

| 路径 | 方法 | 处理函数 | 权限 |
| --- | --- | --- | --- |
| `/analysis` | GET | `analysis_page()` -> `_render_prediction_page(page_id="analysis")` | 需要页面登录 |
| `/analyze` | GET | 重定向到 `/analysis` | 无业务处理 |

### API 入口

| 路径 | 方法 | 处理函数 | 请求 Schema | 权限 |
| --- | --- | --- | --- | --- |
| `/api/analyze-search` | GET | `api_analyze_search()` | query params: `text`, `limit`, `offset`, `source`, `sort`, `module`, `refresh` | `require_user` |
| `/api/analyze` | POST | `api_analyze()` | `AnalyzePayload` | `require_user` |

`AnalyzePayload` 字段：`text: str`、`limit: int`、`offset: int`、`source: str`、`sort: str`、`module: str`、`refresh: bool`。

### 响应结构

主流程调用 `analyze_sentence_search()`，返回普通 dict，核心字段包括：

`input_text`、`module_code`、`analysis`、`intake_outline`、`bilingual_context`、`analysis_mode`、`extracted_keywords`、`retrieval_keywords`、`module_packet`、`retrieval_summary`、`rag_context`、`data_readiness`、`supporting_case_groups`、`supporting_case_rows`、`linked_laws` 等。

当前没有 `DeepAnalysis` / `QualityGateResult` 响应字段。

### Jinja 模板

对应模板是 `app/templates/analyze.html`，并包含：

- `partials/module_sections.html`
- `partials/agent_chat_panel.html`
- 间接包含 `partials/relevance_highlights.html`

## 6. 与案情分析相关模板

### 当前展示结构

`analyze.html` 当前展示：

1. Hero + 表单。
2. 分析任务横幅。
3. 案情概览：展示 `item.case_summary or item.analysis_summary or item.query_text`。
4. `<details>` 中直接展示“原始案情输入”即 `item.query_text`。
5. 关键事实、争议焦点、法律关系、证据关注点、风险点分析。
6. 关联法规、相关案例。
7. `module_sections.html` 中继续展示法规和案例-法规关系。
8. Agent chat 面板。

### CSS / JS 组织

页面主要依赖 `app/static/style.css` 的通用类，如 `hero`、`tool-panel`、`info-panel`、`analysis-report-grid`、`risk-point-card`、`case-slider`、`detail-box` 等。交互主要是模板内 `data-case-slider` 约定和现有全局 JS；案情分析页面没有独立 JS 文件。

### 与目标冲突

当前模板仍直接展示原文：

- `{{ item.case_summary or item.analysis_summary or item.query_text }}`
- `<summary>查看原始案情输入</summary><div class="detail-box">{{ item.query_text }}</div>`

阶段 D 必须改为读取 `deep_analysis` 和 `quality_gate`，并在门禁失败时只展示引用清单、未完成事项和人工复核提示。

## 7. 评测脚本接口

### `scripts/evaluate_case_retrieval.py`

用途：从 `case_rule_relations` 中按固定随机种子抽样高置信度法规-案例关系，用法规标题检索案例，评估关联集合可检索性。

CLI：

- `--sample-size` 默认 100
- `--limit` 默认 12
- `--min-match-score` 默认 0.92
- `--seed` 默认 20260910
- `--output` 默认 `logs/retrieval_evaluation.json`

输出 JSON 包含：

`evaluation_type`、`sample_size`、`sample_method`、`ground_truth_constraint`、`query_limit`、`min_relation_match_score`、`metrics`、`failures`、`completed_at`。成功打印 `[RESULT]: SUCCESS`。

### `scripts/verify_full_vectorization.py`

用途：等待全量向量化检查点完成后，执行向量一致性门禁和检索评测。

CLI：

- `--poll-seconds` 默认 60
- `--sample-size` 默认 200
- `--limit` 默认 12
- `--evaluation-timeout` 默认 3600
- `--evaluation-output` 默认 `logs/retrieval_evaluation_full.json`
- `--log-path` 默认 `logs/full_vectorization_verification.jsonl`

流程：

1. 轮询 `vectorization_checkpoints(job_name='a2aj_case_bge_m3_v1')`。
2. 检查 `rag_chunks` 与 `rag_chunk_embeddings` 的覆盖、hash、维度、空向量和单位向量。
3. 调用 `scripts/evaluate_case_retrieval.py`。

阶段 E 不应修改这两个脚本接口。

## 8. 额外发现：实际案情分析主链路不在任务点名文件内

文档要求重点改 `legal_query_planner_service.py`、`multi_agent_service.py`，但当前 `/analysis` 页面主路径实际是：

`routes.py` -> `analysis_service.analyze_sentence_search()` -> `build_structured_analysis()` -> 搜索/`build_module_packet()` -> 保存历史 -> `analyze.html`。

其中：

- `build_structured_analysis()` 当前 schema 是 `ANALYSIS_SCHEMA`：`facts`、`disputed_issues`、`requested_relief`、`search_keywords`、`summary`、`jurisdiction`、`legal_topics`、`claims`、`risk_flags`。
- `analysis_local_fast_mode=True` 时默认走本地规则/技能增强，不强制调用 LLM。
- `analyze_sentence_search()` 会拼出 `module_packet`、`rag_context`、`data_readiness`、`supporting_case_groups`、`linked_laws`。

因此，阶段 A-C 如果只改 planner/multi-agent，不接入 `analysis_service` 和 `/analysis` 路由，用户页面不会进入新深度解析路径。

## 9. 现状 vs 目标差异表

| 目标 | 现状 | 差异 | 调整方案 |
| --- | --- | --- | --- |
| `QueryPlan` 为 Pydantic schema，含结构化事实、争点、先验候选、置信度 | planner 返回 dict，仅有关键词扩展字段 | 差距大 | 新增 schema 并保留旧 dict 字段；`build_legal_query_plan()` 可返回兼容 dict，新增深度 planner 函数供 analysis 使用 |
| 事实结构化优先 LLM JSON Schema，失败规则降级 | `analysis_service` 有 LLM/本地分析，但输出粗粒度 `facts` 字符串 | 部分可复用 | 在新 planner 中复用 `llm_service.create_structured_response`，失败走日期/金额/当事人正则 |
| 争点推理基于结构化事实 | 当前 issues 多为规则/LLM字符串列表 | 不满足证据链要求 | 将 `DisputeIssue.supporting_facts` 指向 `StructuredFact.content` |
| 先验候选来自数据库 | 当前 planner 不查 DB；module packet 查正式关系/关键词关系 | 缺失 | 新增受控 DB 查询，从 `case_rule_relations`、`legal_rules`、`legal_cases` 生成候选 |
| L2 `DeepAnalysis` 结构化输出 | 当前 `analysis_result` 是粗粒度 dict，预测逻辑在 `agent_service` | 缺失 | 新建 `deep_analysis_service.py`，读取 QueryPlan、`module_packet`、`hybrid_search` 输出生成规则化 DeepAnalysis |
| 风险四类绑定法规/案例 | `multi_agent_service._risk_agent` 已四类风险，但 schema 不一致，未接 `/analysis` | 部分满足 | 复用其证据绑定思路，产出目标 `RiskPrediction` |
| 案件强弱评分规则计算 | 听证评分权重存在；案情分析无目标评分 | 缺失 | 在 config 添加权重，用规则计算 `CaseStrengthScore` |
| L3 质量门禁阈值化 | arbitration 只有阻断规则，无阈值结果 | 缺失 | 新建 `quality_gate_service.py`，并把结果审计到 `multi_agent_runs` 或新表 |
| 展示层深度解析卡片 | 模板展示历史 display payload，且可展开原文 | 不满足 | 在开关启用时渲染 `deep_analysis`，门禁失败走降级区块，移除原文作为结果展示 |
| 默认可回滚 | 当前无 `DEEP_ANALYSIS_ENABLED` | 缺失 | 在 `Settings` 新增开关且默认 false，旧请求默认旧路径 |
| 测试与权限 | 现有没有专门 deep analysis tests 目录 | 缺失 | 增加聚焦单元测试；权限测试沿用 `/api/analyze` 登录门禁和历史隔离 |

## 10. 建议的阶段 A-E 调整方案

1. 阶段 A 不直接破坏 `build_legal_query_plan()` 返回格式。先新增 Pydantic schema 和 `build_deep_query_plan()`，再让 `build_legal_query_plan()` 包含新字段但保留旧字段。
2. 阶段 B 新建 `app/service/deep_analysis_service.py`，避免把大量 L2 规则塞进 `multi_agent_service.py`。输入为案情文本、QueryPlan、`analysis_result/module_packet`、`hybrid_search` 结果。
3. 阶段 C 新建 `app/service/quality_gate_service.py`，`multi_agent_service._arbitration_agent` 后续可复用门禁函数，但第一优先级是 `/analysis` 新路径。
4. 阶段 D 在 `settings.deep_analysis_enabled` 为 true 时渲染新深度解析；false 时完全保留旧页面和 API 行为。
5. 阶段 E 需要新增测试文件或脚本覆盖 schema、降级、空输入、超长输入、注入文本、无证据输入、API 登录门禁和简单检索回归。

## 11. 当前暂停点

阶段 0 勘察完成。由于任务文档“使用说明”明确要求阶段 0 后暂停等待确认，且实际主链路与文档目标文件存在明显差异，建议确认上面的调整方案后再进入阶段 A。
