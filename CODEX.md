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

## 当前数据库快照

- `source_items`：15615
- `legal_cases`：8
- `legal_rules`：12568
- `canada_laws`：12568
- `case_rule_relations`：4
- `canada_case_law_links`：4
- `rag_chunks`：203583
- `rag_chunk_embeddings`：203583
- `risk_assessment_samples`：3
- `risk_feedback_labels`：3

## 检索性能

基准文件：`data/eval/retrieval_benchmark_20260908.json`

- 向量检索：约 19-105 毫秒
- 关键词检索：约 112-894 毫秒
- 混合检索：约 335-926 毫秒

已修复的问题：

- PostgreSQL 关键词检索不再对正文做 `LOWER(text_content) LIKE` 全表扫描。
- 混合检索的关键词通道使用原始查询，向量通道使用扩展查询，避免扩展词造成超宽 rank。

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
python scripts\ingest_open_legal_data.py --source laws-lois-xml --laws-lois-offset 0 --laws-lois-limit 2000 --skip-sync --skip-rebuild
python rag_manage.py rebuild --source canada
python rag_manage.py rebuild-vectors --source canada --module canada
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
