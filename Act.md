# 执行记录

## 2026-09-07

- 用户批准 `PLAN.md` 后开始执行法律 Agent 智能化改造。
- 明确合规边界：不绕过反爬、robots、验证码、登录、访问频率限制或封禁策略；不批量抓取未授权 CanLII 全文；不提交 `.env`、API key、数据库备份、模型权重、原始大语料、浏览器缓存或个人文件。
- 将 50 个无关、临时、个人、调试、旧爬虫、日志、浏览器缓存、旧压缩包目录或文件移动到 `_cleanup_quarantine_20260907/`，未做永久删除。
- 更新 `.gitignore`，忽略 `data/backups/`、`data/imports/`、`data/raw/`、`*.parquet`、`_cleanup_quarantine_*/`、`dist/` 和本地密钥文件。
- 确认当前 SQLAlchemy `DATABASE_URL` 指向 PostgreSQL，数据库版本为 PostgreSQL 17.9。
- 在本地 `.env` 写入用户提供的 OpenAI-compatible Qwen 配置，模型为 `qwen3.8-27b`；真实 API key 只保存在 `.env`。
- 关闭旧的 CanLII realtime search、archive bootstrap、demo seed 和 startup Canada background sync。
- 新增/更新 PostgreSQL + pgvector + HNSW 配置项和环境模板。
- 新增 RAG 结构化字段：`jurisdiction`、`document_type`、`court_level`、`language`、`citation`。
- 新增 pgvector 初始化、HNSW cosine 索引创建、索引状态检查、`hnsw.ef_search` 查询参数和结构化过滤。
- 新增风险评估训练样本表与人工反馈标签表，并在预测运行后沉淀风险样本。
- 新增管理接口：`GET /api/risk-training/samples` 和 `POST /api/risk-training/feedback`。
- 新增 `scripts/db_maintenance.py`，支持数据库状态、向量初始化、备份清空。
- 新增 `scripts/ingest_open_legal_data.py`，支持 A2AJ HuggingFace viewer/parquet 和本地 Justice Canada `laws-lois-xml` 入库。
- 新增 `scripts/seed_risk_samples.py`，写入风险评估 demo 样本和反馈标签。
- 新增 `scripts/benchmark_retrieval.py`，执行关键词、向量、混合检索延迟基准。
- 新增 `sql/postgres_vector_schema.sql`，并在 `sql/legal_agent_demo.sql` 中启用 `vector` 扩展。
- 执行 `python scripts\db_maintenance.py backup-clear --include-corpus`：旧 runtime、RAG、vector、查询、预测、导入任务和语料表已备份到 `data/backups/runtime-cleanup-20260907-165510/` 后清空，`users` 保留。
- A2AJ 初次按 `SCC`、`ONCA`、`LEGISLATION-FED`、`REGULATIONS-FED` 调用 HuggingFace datasets-server 返回 404；确认 viewer 暴露 `default/train`，将 demo 默认配置改为 `default`。
- 通过 A2AJ HuggingFace datasets-server 导入 8 条案例和 8 条法规。
- 本地 BGE-M3 路径 `D:\environment\embeddingModel\bge-m3` 不存在；为保证 demo 闭环，临时切换到 1024 维 hash embedding。
- 首次 clone Justice Canada 官方仓库失败，原因是 Git 代理指向 `127.0.0.1:9`；关闭 Git 代理后 clone 成功，目录为 `data/imports/laws-lois-xml/`。
- 分批导入 Justice Canada XML：offset `0`、`2000`、`4000`、`6000`、`8000`、`10000`、`12000` 各处理 2000 个文件，offset `14000` 处理 1599 个文件，合计 15599 个 XML 文件，无解析错误。
- 执行加拿大结构化同步：生成/同步 8 个案例、12568 条法规规则、4 条案例-规则关系。
- 执行 `python rag_manage.py rebuild --source canada`：扫描 40759 个文档视图，写入 203583 个 RAG chunks，用时约 228 秒。
- 执行 `python rag_manage.py rebuild-vectors --source canada --module canada`：写入 202991 条待重建向量，最终向量表总量为 203583，用时约 1147 秒。
- 执行风险样本 seed：写入 3 条 `risk_assessment_samples` 和 3 条 `risk_feedback_labels`。
- 执行 Qwen 健康检查：`python llm_healthcheck.py` 返回 `ok=true`，模型返回有效结构化 JSON。
- 初次性能基准发现关键词检索和混合检索较慢，部分查询 6-19 秒。
- 修复 PostgreSQL 关键词检索：移除正文 `LOWER(text_content) LIKE` 全表扫描，改为 `tsvector + GIN` 主检索和安全 `to_tsquery` fallback。
- 修复混合检索慢点：关键词通道使用原始查询，向量通道继续使用扩展查询，避免扩展词造成超宽 rank。
- 复测性能：向量检索约 19-105 毫秒，关键词检索约 112-894 毫秒，混合检索约 335-926 毫秒。
- 执行编译检查、RAG 状态检查、向量状态检查、混合检索检查、结构化过滤检查，均通过。
- 启动本地 Uvicorn 临时服务并访问 `/health`，返回 HTTP 200；测试后关闭临时进程。
- 将进度文档和后续写入约定统一为中文；代码标识符、命令、环境变量和 API 路径保留英文以保证运行兼容。
- 提交并推送 Git：`f4b4fb0 Implement legal agent pgvector demo pipeline` 已推送到 `origin/main`。
