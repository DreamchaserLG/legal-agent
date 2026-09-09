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

## 2026-09-08

- 根据用户实测反馈，检查“关键词检索速度快但只显示法规、缺少对应案例和相似度”的问题。
- 检查本地 PostgreSQL 数据：`legal_cases=8`，A2AJ 案例 `source_items=8`，RAG 案例分块 `237`，法规分块 `203346`，正式案例-法规关联 `case_rule_relations=4`、`canada_case_law_links=4`。
- 定位原因：快速检索原先使用单个 `source_filter=canada` 查询，法规分块数量远大于案例分块，Top-N 被法规占满，导致页面看起来没有案例。
- 修改 `app/service/search_service.py`：加拿大快速检索拆成 `law` 与 `case` 两路 RAG 检索，各自去重、计算 `similarity_score`，案例按相似度排序。
- 修改 `app/service/search_service.py`：A2AJ 本地案例不再落入“其他”分组，统一作为案例分组展示。
- 修改 `app/service/search_service.py`：法规卡片挂载当前查询命中的相关案例；正式关系不足时，以本次案例相似度作为临时关联展示。
- 修复同名法规重复问题：法规按“标题 + 引用”去重，保留最高匹配分数。
- 执行编译检查：`python -m compileall app` 通过。
- 执行服务层验证：`estate` 返回 3 条法规 + 2 条案例，案例相似度分别为 `1.0`、`0.8`；每条法规均挂载 2 条相关案例。
- 执行服务层验证：`contract` 返回 2 条法规 + 1 条案例；法规均挂载相关案例。
- 执行中文关键词验证：`遗产争议` 返回 5 条法规 + 2 条案例；中文映射和案例分路检索正常。
- 执行性能验证：`estate`、`contract`、`遗产争议` 三组平均检索约 `140.69ms`，平均分析约 `75.53ms`。

## 2026-09-09

- 根据用户要求继续获取并入库 `legal_cases`，先检查本地 PostgreSQL：A2AJ 案例源 `8` 条、`legal_cases=8`、`case_rule_relations=4`、`canada_case_law_links=4`。
- 使用 A2AJ HuggingFace viewer 从 offset `8` 做小批量连通验证，成功写入 `10` 条案例源数据。
- 直接执行全量同步时在 `18` 条案例规模下超过 `180s` 未完成，定位为 `bootstrap_canada_law_graph` 使用“所有案例 × 所有法规”的交叉匹配。
- 修改 `app/service/canada_case_law_service.py`：先从案例正文抽取法规标题候选，再只对候选法规做正式关联匹配，避免扩库后关联阶段卡死。
- 修改 `app/service/rag_service.py`：修复 `rebuild --source case` 的源过滤，确保只处理案例源和 `legal_cases`，不再误扫全部 `source_items`。
- 分 `10` 批从 A2AJ viewer 导入 offset `18` 到 `918`，每批 `100` 条，共新增 `1000` 条案例源数据，无导入错误。
- 执行结构化同步：`legal_cases` 同步到 `1018` 条，`case_rule_relations` 与 `canada_case_law_links` 均增加到 `1016` 条，已有正式关联的案例为 `544` 条。
- 执行 `python rag_manage.py --json rebuild --source case`：处理 `2036` 个案例视图，写入 `26285` 个案例 RAG 分块，用时约 `32.35s`。
- 执行 `python rag_manage.py --json rebuild-vectors --source case --module canada`：补齐 `26048` 条案例向量，总向量增加到 `229631` 条，用时约 `201.47s`。
- 修改 `app/service/search_service.py`：快速检索结果优先读取 `case_rule_relations`，案例卡片展示正式关联法规，法规卡片优先展示正式关联案例；无正式关系时才使用本次相似度临时挂载。
- 执行编译检查：`python -m compileall app scripts\ingest_open_legal_data.py rag_manage.py` 通过。
- 执行检索验证：`family law child support appeal` 返回 `3` 条案例、`4` 条法规，其中 `Family Law Act` 由正式案例关联反向补入，并挂载 `Duggan v. White`、`Graydon v. Michel`。
- 执行检索验证：`criminal code sentencing appeal` 返回 `3` 条案例、`8` 条法规，`R. v. Blaney`、`R. v. Heidarian`、`R. v. Stirling` 均带正式关联法规。
- 执行中文检索验证：`租赁 合同 违约` 返回 `5` 条案例、`6` 条法规，案例带 `similarity_score` 并按相似度排序。
- 执行分析入口验证：`analyze_sentence_search("family law child support appeal")` 走 `local_fast_rag`，返回 `5` 条 supporting cases 与 `8` 条法规，能接住扩库后的正式关联数据。
## 2026-09-09 BGE-M3 分块语义向量迁移流水线

- 已按最新要求覆盖 `run_migration.py` 和 `schema.sql`。旧的“每个完整案例一条向量”设计不再使用；新结构为 `cases_metadata`（原文与元数据）和 `case_chunks`（每案多个向量分块）。
- 模型加载在导入 Hugging Face 前固定 `HF_ENDPOINT=https://hf-mirror.com`，先下载到本地缓存目录，再以缓存路径加载 BGE-M3；`OSError` 自动每 10 秒重试，最多 3 次。加载后设置并记录 `max_seq_length=8192`、缓存路径和运行设备，显存不足自动改为 CPU。
- 每篇案例强制使用 `RecursiveCharacterTextSplitter`，通过 BGE-M3 tokenizer 精确控制 512 token 分块和 64 token overlap；只有 `chunk_text` 会送入 embedding 模型，全文只留存在 `cases_metadata.raw_text`。
- 按源案例每批 500 条处理，检查点、分块写入与 `execute_values` 均可断点续传。HNSW、GIN 和案例分块索引在独立 autocommit 连接中并行 `CREATE INDEX CONCURRENTLY`。
- 已补充 `SOURCE_*` / `A2AJ_*` 配置；设置独立 `A2AJ_DB_URL` 时未给表名会自动探测 `case_embeddings`、`legal_cases` 或 `rag_chunks`。跨库 ID 冲突应设置 `A2AJ_ID_OFFSET`。
- 本机仅完成静态编译；因环境未安装 `pgvector`，入口自检会输出 `[RESULT]: FAILED`，未连接数据库、未下载模型、未写入或切换任何表。
