# Legal Agent 智能化改造执行计划

日期：2026-09-07
状态：当前阶段已按批准范围执行完成

## 目标范围

把当前 `legal-demo` 改造成以 PostgreSQL + pgvector 为核心的加拿大法律智能 Agent demo，支持开放法律数据入库、结构化过滤、关键词检索、向量检索、混合检索、RAG 证据上下文、Qwen OpenAI-compatible 模型调用、风险评估样本沉淀和后续 MCP/A2A 工具调度扩展。

## 已执行阶段

1. 清理与法律项目无关的文件，先隔离到 `_cleanup_quarantine_20260907/`，不永久删除。
2. 切换本地数据库到 PostgreSQL，并确认 PostgreSQL 17.9 可连接。
3. 启用 pgvector，创建 HNSW cosine 向量索引。
4. 备份并清空旧查询、旧预测、旧 RAG、旧向量、旧导入任务和旧语料数据，保留 `users`。
5. 关闭 CanLII 实时检索和未授权批量全文获取。
6. 接入 A2AJ HuggingFace demo 数据源。
7. clone 并批量导入 Justice Canada 官方 `laws-lois-xml` XML 数据。
8. 按现有项目结构同步数据链路：`source_items` -> `canada_laws` / `legal_cases` / `legal_rules` -> `case_rule_relations` -> `rag_chunks` -> `rag_chunk_embeddings`。
9. 实现并验证结构化过滤、关键词检索、向量检索、混合检索。
10. 接入并健康检查 Qwen `qwen3.8-27b` OpenAI-compatible 模型。
11. 建立风险评估样本表和人工反馈表，写入 demo 风险样本。
12. 执行编译、数据库、检索、性能、模型和应用启动验证。
13. 更新 `Act.md`、`Make.md`、`result.md`、`CODEX.md`。
14. 生成最小可运行压缩包并同步 Git。

## 合规边界

- 不绕过 CanLII 或其他站点的反爬、robots、验证码、登录、访问频率限制或封禁策略。
- 不使用代理池、IP 轮换、浏览器指纹伪装等方式系统性批量下载受限文档。
- 不把未授权抓取的全文作为训练语料。
- 不提交真实 `.env`、API key、数据库备份、模型权重、原始大语料、浏览器缓存或个人文件。

## 当前数据策略

- 法规主来源：Justice Canada 官方 `justicecanada/laws-lois-xml`。
- 案例 demo 来源：A2AJ `a2aj/canadian-case-law` 的 HuggingFace datasets-server `default/train`。
- 法规补充 demo 来源：A2AJ `a2aj/canadian-laws` 的 HuggingFace datasets-server `default/train`。
- CanLII：只保留未来授权 API 接入边界，本阶段不做未授权全文批量获取。

## 当前结果

- `source_items`：15615
- `legal_cases`：8
- `legal_rules`：12568
- `canada_laws`：12568
- `rag_chunks`：203583
- `rag_chunk_embeddings`：203583
- `risk_assessment_samples`：3
- `risk_feedback_labels`：3

## 后续重点

- 切换真实语义 embedding，并重建向量。
- 在网络可用时继续 A2AJ parquet 批量导入。
- 对法规 XML 做 section-level chunking。
- 建立正式风险评估标注集和检索评测集。
- 接入 MCP/A2A 工具调度、权限、trace 和人工审批。
