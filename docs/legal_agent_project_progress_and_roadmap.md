# Legal Agent Project Progress and Roadmap

更新日期：2026-07-19

## 1. 当前项目定位

当前项目已经从普通法律 Demo 推进到具备法律 RAG 检索底座的 Agent 原型系统。

目前可以较准确地描述为：

```text
具备本地 BGE-M3 向量检索、PostgreSQL pgvector 存储、Hybrid Search、结构化法律查询规划、本地 reranker、CanLII 增量补水和检索评测能力的法律智能检索与推理原型。
```

但还不能完全描述为成熟的企业级法律 Agent 产品，因为以下能力仍未完整闭环：

```text
严格 Agent trace
引用逐条校验
法规 section-level chunking
跨领域评测集
专业 cross-encoder reranker
Matter workspace
权限审计
用户文档库
```

## 2. 已完成能力

### 2.1 本地向量模型与向量数据库

已切换为本地开源 embedding 模型：

```text
provider: local
model: bge-m3
dimension: 1024
storage: PostgreSQL + pgvector
```

当前向量库状态：

```text
total_embeddings: 21700
by_model: local / bge-m3 / 1024
pgvector_enabled: true
```

说明：

- 查询时会使用本地 BGE-M3 生成 query embedding。
- 检索对象是本地 `rag_chunk_embeddings` 表中的向量。
- 新增案例后必须重新生成对应 chunk 的向量。
- 不需要训练 BGE-M3，当前阶段只需要用它做本地推理生成向量。

### 2.2 Hybrid Search

当前检索链路已经升级为：

```text
query
-> structured query plan
-> law / case split retrieval
-> lexical search
-> vector search
-> RRF fusion
-> local legal reranker
-> evidence pack
```

新增能力：

- `plan-query`：查看结构化法律查询计划。
- `hybrid-search`：执行结构化混合检索。
- `hydrate-canlii`：按 query 生成关键词，补水 CanLII 数据，并增量重建 RAG/向量。
- `eval-retrieval`：运行检索评测集。

常用命令：

```powershell
python rag_manage.py plan-query "Ontario tenant eviction repair issue"
python rag_manage.py hybrid-search "Ontario tenant eviction repair issue" --module canada --limit 8
python rag_manage.py hydrate-canlii "Ontario tenant eviction repair issue" --target-count 3 --module canada
python rag_manage.py eval-retrieval --limit 10 --output docs\retrieval_eval_report_canada.json
```

### 2.3 CanLII 增量补水

当前支持受控数据补水流程：

```text
query
-> query planner
-> hydration keywords
-> CanLII keyword hydration
-> source_items upsert
-> rag_chunks rebuild
-> BGE-M3 vector rebuild
```

最近一次真实补水测试结果：

```text
CanLII stored: 9 items
RAG rebuilt: 891 CanLII documents, 1279 chunks
new vectors written: 18
```

这个设计的重点是：大模型或 query planner 只决定“查什么”，不能直接自由写库；数据入库、去重、向量化必须由受控代码完成。

### 2.4 检索评测能力

已新增评测集：

```text
data/eval/canada_retrieval_eval.json
```

已新增评测报告：

```text
docs/retrieval_eval_report_canada.json
docs/retrieval_eval_summary.md
```

当前评测覆盖：

```text
Ontario residential tenancy
ONLTB eviction
repair / habitability
vital services
rent arrears / N4
section 83 relief
commercial tenancies regression
```

## 3. 评测结论

最近一次评测命令：

```powershell
python rag_manage.py eval-retrieval --limit 10 --output docs\retrieval_eval_report_canada.json
```

评测结果：

```text
cases: 20
hit@1: 0.90
hit@3: 1.00
hit@5: 1.00
hit@10: 1.00
mrr: 0.95
misses@10: 0
```

结论：

```text
在当前 Ontario residential tenancy / ONLTB 小型评测集上，系统已经能够稳定在 Top-3 内召回预期证据，Top-10 无漏召。
```

需要注意：

- 这不是完整法律检索基准。
- 当前评测集只有 20 条，且集中在 Ontario tenancy 场景。
- 不能据此宣称系统已经具备跨领域法律检索能力。
- 下一阶段必须扩展到 100-200 条多领域评测样例。

评测中发现并已修复的问题：

```text
问题：commercial tenancies 查询被误导到 residential tenancy。
修复：query planner 增加 commercial tenancy 分支；reranker 增加 Commercial Tenancies Act 标题 boost 和 residential drift penalty。
结果：Commercial Tenancies Act 可以排到第 1。
```

## 4. 竞品启发

### 4.1 Thomson Reuters CoCounsel Legal

官方信息显示，CoCounsel Legal 强调 agentic workflows、Deep Research，并且结果基于 Westlaw、Practical Law 和组织内部知识。

参考：

```text
https://legal.thomsonreuters.com/en/products/cocounsel-legal
https://www.thomsonreuters.com/en/press-releases/2025/august/thomson-reuters-launches-cocounsel-legal-transforming-legal-work-with-agentic-ai-and-deep-research
```

对本项目的启发：

```text
法律 AI 的核心不是单轮聊天，而是从法律研究、证据定位、文档分析到结果输出的可追踪工作流。
```

### 4.2 Lexis+ with Protege

Lexis+ with Protege 强调 trusted LexisNexis content、purpose-built workflows、verified and traceable results。

参考：

```text
https://www.lexisnexis.com/en-us/products/lexis-plus-protege.page
```

对本项目的启发：

```text
后续必须把检索结果、引用依据和结论绑定，不能只输出自然语言结论。
```

### 4.3 Harvey Vault

Harvey Vault 强调大规模文档库、review tables、well-cited reports、DMS 集成、权限和治理。

参考：

```text
https://www.harvey.ai/platform/vault
https://help.harvey.ai/articles/vault
```

对本项目的启发：

```text
企业级法律产品需要 matter workspace、用户私有文档库、权限控制和审计，而不只是公共案例检索。
```

### 4.4 vLex Vincent AI

Vincent AI 覆盖 research question、argument building、jurisdiction comparison、legal proposition、matter collections 等工作流。

参考：

```text
https://knowledge.vlex.com/en/vincent-ai
https://support.vlex.com/vincent-by-vlex/vincent/core-workflows/ask-a-research-question
```

对本项目的启发：

```text
法律 Agent 应该支持标准化任务入口，例如案情分析、研究备忘录、论证生成、法域比较、合同分析。
```

## 5. 当前主要短板

### 5.1 数据粒度不足

当前很多法规数据仍是 title / citation / summary 级别，不是 section-level。

风险：

```text
模型可能知道某个 Act 相关，但不能准确定位具体 section。
```

改进：

```text
法规应拆到 section / subsection / article 粒度。
```

### 5.2 Reranker 仍是规则型

当前 reranker 已经有效，但本质是规则加权，不是专业 cross-encoder。

风险：

```text
跨领域、长案情、复杂争议焦点下泛化能力不足。
```

改进：

```text
接入 bge-reranker-v2-m3 或同类本地 reranker。
```

### 5.3 Agent trace 不完整

当前已有检索链路，但还没有完整记录：

```text
案情解析结果
检索计划
每一步工具调用
采用/排除证据
最终结论和引用绑定关系
```

这会影响法律场景的可解释性和可信度。

### 5.4 引用校验不足

当前结果可以返回引用，但还没有强制检查：

```text
每个法律判断是否有 evidence_id 支撑
引用 URL 是否可访问
案例/法规是否属于正确法域
法规是否仍然有效
```

### 5.5 评测范围不足

当前评测集只有 20 条，集中在 Ontario tenancy。下一步必须扩展。

## 6. 后续技术路线

### 阶段一：检索质量工程化

目标：

```text
把当前小型评测集扩展为可持续调参的检索基准。
```

任务：

```text
1. 扩展评测集到 100-200 条
2. 覆盖合同、侵权、就业、欺诈、禁令、行政复议、移民、税务等领域
3. 每条样例标注 expected_law、expected_case、jurisdiction、issue_tags、must_not_return
4. 增加 law_hit_rate、case_hit_rate、wrong_jurisdiction_rate、must_not_return_violation
5. 每次修改 query planner / reranker / chunking 后自动跑评测
```

验收指标：

```text
hit@3 >= 0.85
hit@5 >= 0.90
wrong_jurisdiction_rate <= 0.10
must_not_return_violation <= 0.05
```

建议优先级：最高。

### 阶段二：接入专业 reranker

目标：

```text
用 cross-encoder reranker 替代纯规则精排。
```

推荐模型：

```text
BAAI/bge-reranker-v2-m3
```

目标链路：

```text
lexical top 80
vector top 80
RRF merge
dedupe
cross-encoder rerank top 80
legal authority boost
final top 8
```

验收指标：

```text
评测 hit@3 提升或保持
错法域结果下降
P95 检索延迟可接受
```

建议优先级：最高。

### 阶段三：法规 section-level chunking

目标：

```text
让系统能定位具体法律条款，而不是只定位 Act title。
```

任务：

```text
1. 抓取/解析法规全文
2. 按 section / subsection 拆分
3. metadata 增加 jurisdiction、act_title、section_no、effective_date、source_url
4. 检索结果展示 section_no
5. LLM 回答必须引用 section-level evidence
```

验收指标：

```text
法规类 query 的 section-level hit@5 >= 0.80
回答中每个法规判断都有 section 引用
```

建议优先级：高。

### 阶段四：Agent Orchestrator 与 trace

目标：

```text
把系统从 RAG 检索增强升级为可审计 Agent。
```

建议新增：

```text
app/service/legal_agent_orchestrator.py
```

标准流程：

```text
parse_case
identify_jurisdiction
identify_legal_issues
retrieve_laws
retrieve_cases
rerank_evidence
verify_citations
build_arguments
predict_outcome
generate_answer
```

建议新增表：

```text
agent_traces
agent_trace_steps
retrieval_runs
retrieval_results
citation_checks
```

验收指标：

```text
每次分析都有 trace_id
每个结论可追溯到 evidence_id
用户可查看采用证据和排除证据
```

建议优先级：高。

### 阶段五：引用校验与可信回答

目标：

```text
降低幻觉风险，让模型只基于证据回答。
```

任务：

```text
1. 生成答案前检查 evidence coverage
2. 每个法律结论绑定 evidence_id
3. 没有证据时输出 insufficient_evidence
4. 引用 URL 可访问性检查
5. 法域一致性检查
6. 过期/无效法规标识
```

验收指标：

```text
unsupported_claim_rate <= 0.05
missing_citation_rate <= 0.05
```

建议优先级：高。

### 阶段六：产品化界面

目标：

```text
让系统从命令行能力升级为可演示、可使用的产品。
```

建议前端模块：

```text
1. Matter Workspace
2. Evidence Panel
3. Agent Trace Timeline
4. Citation Viewer
5. Retrieval Evaluation Dashboard
6. Document Upload / Vault
7. Admin Data Sync Panel
```

核心体验：

```text
用户输入案情
-> 系统显示解析出的争议焦点
-> 展示法规和案例证据
-> 展示采用/排除原因
-> 生成法律分析报告
-> 支持导出 memo
```

建议优先级：中高。

## 7. 建议排期

### 第 1 周

```text
扩展评测集到 50 条
补充 contract / negligence / employment 三类样例
增加 wrong_jurisdiction_rate 和 must_not_return 指标
```

### 第 2 周

```text
下载并接入 bge-reranker-v2-m3
实现 reranker provider 配置
对比规则 reranker 与模型 reranker
```

### 第 3 周

```text
法规全文 section-level chunking
优先处理 Residential Tenancies Act / Commercial Tenancies Act
检索结果加入 section_no
```

### 第 4 周

```text
实现 legal_agent_orchestrator.py
新增 trace 表
让 analyze / predict 输出 trace_id
```

### 第 5 周

```text
实现引用校验
强制 final answer 使用 evidence_id
无证据时输出 insufficient_evidence
```

### 第 6 周

```text
实现 Evidence Panel 和 Agent Trace Timeline
整理演示脚本
生成项目交付文档和技术汇报材料
```

## 8. 风险与应对

### 风险一：数据源授权

法律数据源通常有访问和使用限制。

应对：

```text
只使用合规 API、RSS、公开授权数据或用户上传文档。
保留 source_url、fetch_mode、created_at、content_hash。
```

### 风险二：评测集过小导致虚高

当前 20 条评测集表现很好，但不能代表全部法律领域。

应对：

```text
扩展多领域、多法域、多表达方式的评测集。
```

### 风险三：规则 reranker 过拟合

当前部分规则针对 tenancy 场景有效，但跨领域可能失效。

应对：

```text
接入 cross-encoder reranker，并用评测集验证。
```

### 风险四：生成答案仍可能幻觉

检索准确不等于最终分析一定准确。

应对：

```text
答案生成必须绑定 evidence_id。
没有证据支撑的结论必须降级或拒答。
```

## 9. 当前结论

当前项目已经完成法律 Agent 检索底座的关键阶段：

```text
本地 embedding
pgvector 向量库
Hybrid Search
结构化 query planner
本地 reranker
CanLII 增量补水
检索评测
```

从工程进度看，项目已经可以作为：

```text
法律智能检索与推理系统原型
```

继续推进的关键不是再堆功能，而是围绕以下四个方向补齐可信产品能力：

```text
1. 更大规模评测集
2. 专业 reranker
3. section-level 法规证据
4. Agent trace + citation verification
```

完成这些后，项目才更接近校企合作或企业级法律 AI 产品的标准。
