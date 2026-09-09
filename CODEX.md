# CODEX 项目记忆

更新时间：2026-09-08 13:48 Asia/Shanghai

## 项目是什么

`legal-demo` 是一个法律检索、案例分析、风险评估和判决预测 demo。当前阶段已经从普通法律 demo 改造成加拿大法律智能 Agent 原型，核心能力是：

- PostgreSQL 主数据库
- pgvector 向量存储
- HNSW + cosine 相似度检索
- PostgreSQL full-text + GIN 关键词检索
- 结构化过滤
- 混合检索与本地 rerank
- RAG 证据上下文
- Qwen OpenAI-compatible 模型调用
- 风险评估训练样本和人工反馈沉淀

系统不是正式法律意见工具。所有回答必须基于本地检索证据，清楚表达不确定性；证据不足时必须降低置信度或说明无法判断。

## 技术栈

- Python 3.11
- FastAPI
- Uvicorn
- SQLAlchemy 2.x
- PostgreSQL 17.x
- pgvector
- psycopg2
- Jinja2
- BeautifulSoup / lxml
- requests
- websocket-client
- torch / transformers
- HuggingFace datasets-server / parquet
- Git

## 当前运行配置

- 本地数据库：PostgreSQL
- 模型提供方：`LLM_PROVIDER=custom`
- 模型名称：`CUSTOM_MODEL=qwen3.8-27b`
- Qwen OpenAI-compatible base URL 和 API key：只保存在本地 `.env`，不得写入 Git。
- CanLII realtime search：已关闭
- 旧 demo seed：已关闭
- archive bootstrap：已关闭
- startup Canada background sync：已关闭
- 当前 embedding：`hash / local-hash-embedding / 1024`

说明：hash embedding 只用于验证 pgvector/HNSW/RAG 管线。生产质量需要切换真实语义 embedding 后重建向量。

## 目录结构

```text
app/
  api/          FastAPI 页面与 API 路由
  core/         配置、数据库连接、SQLite fallback 初始化
  models/       数据模型
  service/      业务逻辑、入库、RAG、检索、预测、风险样本
  static/       静态资源
  templates/    页面模板
data/
  eval/         可跟踪的评测文件和基准输出
  backups/      本地数据库备份，Git 忽略
  imports/      本地开放数据导入目录，Git 忽略
  raw/          原始数据目录，Git 忽略
docs/           项目说明、评测说明、交付说明
scripts/        运维、入库、基准和 seed 脚本
sql/            PostgreSQL schema 和 pgvector 参考 SQL
dist/           本地压缩包产物，Git 忽略
```

## 关键文件

- `app/main.py`：FastAPI 应用入口和启动初始化。
- `app/api/routes.py`：页面和 API 路由，包含风险训练样本接口。
- `app/core/config.py`：环境变量配置。
- `app/core/database.py`：SQLAlchemy 引擎和数据库工具。
- `app/service/legal_data_service.py`：法律案例、法规、关系表和 Canada 派生流程。
- `app/service/canada_case_law_service.py`：Canada law graph 与 case-law link。
- `app/service/rag_service.py`：RAG chunk、关键词检索、结构化过滤、导出。
- `app/service/vector_store_service.py`：embedding、pgvector、HNSW、向量检索。
- `app/service/hybrid_retrieval_service.py`：关键词 + 向量混合检索。
- `app/service/agent_service.py`：分析、问答、预测编排。
- `app/service/risk_assessment_service.py`：风险评估训练样本和反馈标签。
- `scripts/db_maintenance.py`：数据库状态、初始化、备份清空。
- `scripts/ingest_open_legal_data.py`：A2AJ 与 `laws-lois-xml` 入库。
- `scripts/seed_risk_samples.py`：风险样本 seed。
- `scripts/benchmark_retrieval.py`：检索延迟基准。
- `rag_manage.py`：RAG/vector/hybrid CLI。
- `llm_healthcheck.py`：Qwen/custom endpoint 健康检查。
- `sql/postgres_vector_schema.sql`：pgvector/HNSW/risk schema 参考。
- `PLAN.md`：批准后的执行计划。
- `Act.md`：执行记录。
- `Make.md`：问题与后续方案。
- `result.md`：当前执行结果。

## 当前进展

- 2026-09-09：新增并按分块要求修正全自动 BGE-M3 迁移流水线 `run_migration.py`。目标采用 `cases_metadata` + `case_chunks` 一主多从结构，模型只对 512 token、64 token overlap 的 `chunk_text` 生成 `VECTOR(1024)`，不直接编码整篇文书。
- 迁移流水线支持 Hugging Face 国内镜像和缓存路径加载、模型 OSError 三次重试、`max_seq_length=8192`、CUDA OOM 自动 CPU 降级、500 案例批处理、检查点、JSON Lines、HNSW/GIN 并发索引、72 小时旧表备份、RRF `hybrid_search` 和 CI 结果行输出。
- 本轮完成脚本交付与静态验证，未对本地或生产数据库执行真实 BGE-M3 迁移；本机还需安装锁定的迁移依赖后才能运行，现有 hash 向量保持可用。

- 项目清理已完成，无关文件已隔离，未永久删除。
- PostgreSQL 17.9 已验证。
- pgvector 已启用。
- HNSW cosine 索引已创建并验证。
- 旧查询、旧预测、旧 RAG、旧向量、旧导入任务和旧语料已备份并清空。
- A2AJ demo 数据已导入。
- Justice Canada 官方 `laws-lois-xml` 已 clone 到本地。
- 15599 个官方 XML 文件已批量导入。
- 加拿大结构化同步已完成。
- RAG chunks 已全量重建。
- pgvector embeddings 已全量重建。
- 风险评估样本和反馈标签已写入 demo 数据。
- Qwen `qwen3.8-27b` 健康检查已通过。
- 应用 `/health` 启动冒烟测试已通过。
- 最小可运行压缩包路径：`dist/legal-demo-pgvector-agent-minimal-20260908-134817.zip`
- 2026-09-08 补充：加拿大快速检索已改为法规与案例分路召回，案例按 `similarity_score` 排序，法规卡片会挂载当前命中的相关案例。
- 2026-09-09 补充：A2AJ 案例扩库到 `1018` 条，正式案例-法规关联增加到 `1016` 条；快速检索优先读取 `case_rule_relations`，并从命中案例反向补入正式关联法规。

## 当前数据库快照

- `source_items`：16625
- `a2aj_case` 源案例：1018
- `legal_cases`：1018
- `legal_rules`：12726
- `canada_laws`：12726
- `case_rule_relations`：1016
- `canada_case_law_links`：1016
- 已有关联法规的案例：544
- `rag_chunks`：229631
- 案例 RAG 分块：26285
- 法规 RAG 分块：203346
- `rag_chunk_embeddings`：229631
- `risk_assessment_samples`：3
- `risk_feedback_labels`：3

说明：当前已经完成千条级案例 demo 扩库，但仍不是 A2AJ 全量 `22.5` 万条案例。案例覆盖和正式关联质量仍是后续检索质量的主要瓶颈。

## 检索性能

基准文件：`data/eval/retrieval_benchmark_20260908.json`

- 向量检索：约 19-105 毫秒
- 关键词检索：约 112-894 毫秒
- 混合检索：约 335-926 毫秒

已修复的问题：

- PostgreSQL 关键词检索不再对正文做 `LOWER(text_content) LIKE` 全表扫描。
- 混合检索的关键词通道使用原始查询，向量通道使用扩展查询，避免扩展词造成超宽 rank。
- 首页关键词和案情分析入口走本地快速 RAG 路径。
- 加拿大检索按法规与案例分路召回，避免法规分块挤占案例结果。
- 案例结果输出 `similarity_score`，并在模块展示里以“相似度”排序。

最新验证：

- `family law child support appeal`：3 条案例 + 4 条法规；`Family Law Act` 由正式案例关联反向补入，并挂载 `Duggan v. White`、`Graydon v. Michel`。
- `criminal code sentencing appeal`：3 条案例 + 8 条法规；案例均带正式关联法规。
- `租赁 合同 违约`：5 条案例 + 6 条法规；案例按 `similarity_score` 排序。
- `analyze_sentence_search("family law child support appeal")`：走 `local_fast_rag`，返回 5 条 supporting cases 与 8 条法规。
- 最新服务层检索约 `0.56s-0.96s`；分析入口约 `2.37s`。当前仍使用 hash embedding，真实语义 embedding 切换后需要重新评估。

## 代码规范

- 进度文档、交付文档和后续说明默认使用中文。
- 代码标识符、环境变量、API 路径、SQL 表字段和命令保持英文，避免破坏运行兼容性。
- 修改范围围绕法律 Agent、RAG、数据入库、检索、风险评估，不做无关重构。
- 优先沿用现有 service 分层和 SQLAlchemy `text()` 模式。
- 结构化数据优先使用 JSON、XML、SQL 解析，不做脆弱字符串拼接。
- PostgreSQL 是主目标，SQLite 只保留轻量 fallback。
- 法律输出必须包含证据、不确定性、风险点、缺失事实和置信度边界。
- 预测和问答不能脱离检索证据。
- 不提交本地密钥、备份、大语料、模型文件或隔离区。

## 常用命令

```powershell
python -m compileall app scripts canlii_ingest.py rag_manage.py export_rag_data.py llm_healthcheck.py
python scripts\db_maintenance.py status
python scripts\db_maintenance.py init-vector
python scripts\db_maintenance.py backup-clear --include-corpus
python scripts\ingest_open_legal_data.py --source a2aj --cases-per-config 8 --laws-per-config 8 --rebuild-limit 40
python scripts\ingest_open_legal_data.py --source a2aj --a2aj-mode viewer --a2aj-offset 18 --cases-per-config 100 --laws-per-config 0 --skip-sync --skip-rebuild
python scripts\ingest_open_legal_data.py --source laws-lois-xml --laws-lois-offset 0 --laws-lois-limit 2000 --skip-sync --skip-rebuild
python rag_manage.py rebuild --source canada
python rag_manage.py rebuild --source case
python rag_manage.py rebuild-vectors --source canada --module canada
python rag_manage.py rebuild-vectors --source case --module canada
python rag_manage.py --json vector-status
python rag_manage.py hybrid-search "contract good faith appeal" --module canada --source canada --limit 5
python scripts\benchmark_retrieval.py --repeat 2 --limit 5 --output data\eval\retrieval_benchmark_20260908.json
python llm_healthcheck.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 尚未完成

- A2AJ parquet 直连下载受当前网络限制，后续需在网络畅通环境继续。
- 真实语义 embedding 尚未接入。
- 风险评估尚未微调训练，只完成样本和反馈闭环。
- MCP/A2A 工具调度尚未实现。
- 法规尚未做 section-level 精细切分。
- 案例库仍需继续扩充到更高覆盖。优先从 Hugging Face `a2aj/canadian-case-law` 分批导入更多案例，或改用 parquet 缓存 / A2AJ API / MCP，再重建 RAG 与向量索引。
- 案例-法规关联仍需质量分层。当前能建立正式关联，但还需要区分实体法律依据、程序性背景引用和普通背景引用，并对高频程序性法规降权。
