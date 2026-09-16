# CODEX 项目记忆

## 2026-09-10 OPTI 实施状态

- P0 当前阻塞：A2AJ 导入在 RAD 分区出现 NUL，已通过 `app/service/common_service.py:repair_text` 统一清洗并重启。未完成 `225807` 条前不能启动全量案例同步与向量重建。
- P1 已实现：`app/service/skill_runtime_service.py` 维护 7 个本地加拿大技能，运行审计写入 `skill_runs`；`app/skills/ca/manifest.json` 是声明式技能清单；`/api/skills` 和 `/api/skills/run` 需要登录用户。
- P2 未开始：不存在经过时间切分和人工复核的风险标签训练集，不能训练或宣称具备可靠胜诉概率预测。
- 最新导入检查：A2AJ 已到 `188557` 条有效正文，当前正在处理 `SCC` 分区；`legal_cases=1018` 和 `case_rule_relations` 覆盖 `544/1018=53.44%` 仍是全量同步前的旧结构化快照。
- 2026-09-10 后续：29 个 A2AJ 分区已处理 `225807` 上游行，有效正文 `225762`。`scripts/materialize_a2aj_snapshot.py` 正在分页同步结构化案例并将在完成后批量生成关联；全量向量化受磁盘 30GB 剩余空间的容量门禁约束，禁止无界启动。

## 2026-09-09 A2AJ 全量恢复状态

- 主案例源：Hugging Face `a2aj/canadian-case-law`，官方数据量 `225807`。全量导入不再依赖 datasets-server rows，而是按 29 个 Parquet 分区下载并以 `a2aj_parquet_checkpoints` 续传。
- 当前任务：`scripts/run_a2aj_full_pipeline.py` 在后台执行。分区完成后才执行加拿大案例结构化同步、案例-法规关联、RAG 重建、512 token/64 token overlap 分块、BGE-M3 1024 维向量写入与检索验收。
- 数据质量：`source_items.source_uid` 是幂等键；空 `raw_text` 记录在导入时跳过。历史残留的空正文记录必须在同步前清理，避免产生无效案例与空分块。
- 全文策略：A2AJ 流水线的 `--max-text-chars=0` 表示保留全文。旧任务的 50000 字符截断记录由本次从零重跑的分区以相同 `source_uid` 覆写补全；不要在全量补齐前启动案例 RAG 重建。

## 2026-09-09 当前运行状态

- 语义向量栈：`sentence-transformers==3.0.1`、`pgvector==0.3.0`、`BAAI/bge-m3`、`VECTOR(1024)`、HNSW cosine、PostgreSQL 全文 GIN 与 RRF 混合检索。
- 迁移实现位置：`app/service/embedding_service.py` 负责 BGE-M3 本地缓存加载和 GPU/CPU 降级；`app/service/vector_store_service.py` 负责幂等向量写入、活跃模型部分 HNSW 及统计信息刷新；`scripts/ingest_open_legal_data.py` 负责 A2AJ 断点导入。
- 当前数据快照：`a2aj_case=11318`，`legal_cases=1018`，本轮一致性校验时 BGE-M3 分块=12224；A2AJ 新源尚待结构化同步，因此未计入 `legal_cases`。已验证 BGE-M3 分块不存在缺失外键、维度、哈希或归一化异常。
- 性能基线：HNSW SQL 均值 1.17ms；常驻 BGE-M3 端到端向量检索均值 49.21ms；常驻混合检索均值 187.91ms。模型冷启动约 8 秒。
- 后续顺序：保持 A2AJ 断点导入和案例 BGE 迁移运行；案例迁移完成后转换法规切片；源数据拉取结束后执行结构化同步、关联、RAG 重建和新增案例 BGE 转换；最后重新运行关联准确率与性能基准。
- 全自动入口：`scripts/run_a2aj_full_pipeline.py`。它以数据库中的 `a2aj_case` 数量作为断点，持续拉取至 A2AJ 空页且案例数不少于 `225000`；之后顺序执行正式案例同步、案例-法规关联、案例分块、案例与法规 BGE-M3 向量化、向量一致性和检索延迟验收。运行状态写入 `logs/a2aj_full_pipeline.jsonl`，最终输出 `[RESULT]: SUCCESS` 或 `[RESULT]: FAILED`。
- 当前后台状态：全自动入口已从 offset `14618` 启动；在上游 TLS 短暂断连时保留原 offset 自动退避，不能将当前 14618 条误报为全量完成。
- 导入通道已改为 29 个 A2AJ Parquet 分片（总计 225807 条），由 `a2aj_parquet_checkpoints` 保存每个配置的已处理行数；无需依赖不稳定的 datasets-server rows 接口。
- 分块规范：实际 RAG 与迁移流程统一使用 `RecursiveCharacterTextSplitter`，BGE-M3 tokenizer 精确控制 `512 token` 和 `64 token overlap`，优先分隔符为 `\n\n`、`\n`、`。`、`！`、`？`、`.`、`!`、`?`、空格；超过限制时按 tokenizer token 边界切分。
- 数据源规范：A2AJ 主库的正式规模是 225807 条，当前采用 MIT 许可的 29 个 Parquet 分片导入。Refugee Law Lab（195646 条，CC BY-NC 4.0）只能在确认非商业用途并完成 citation 级去重后作为补充；CanLegalRAGBench（1649 条）仅作带标注检索评测，不能用其私有来源文档扩充主语料。

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
- 2026-09-09：当前案例数 `1018`；用 50 条正式案例-法规关系做标题查询基准，案例与法规联合关联命中率 `92%`。全文检索已改为精确命中优先、零命中才前缀回退，长标题查询从约 20 秒降至热态约 117ms；50 条样本平均 85.55ms、P95 477.81ms。
- 现有 `229631` 个 RAG 向量仍全部为 hash 向量，真实语义向量迁移未执行；正式关系只覆盖 544/1018 个案例，不能把未正式关联案例的临时检索挂载当作关系准确率。

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

## 2026-09-10 全量向量化运行说明

### 当前数据状态

- `source_items(a2aj_case)`：225762 条有效加拿大案例原文。
- `legal_cases`：224376 条结构化案例；去重以来源 URL 或语义身份为依据。
- `case_rule_relations`：110410 条正式案例-法规关系，覆盖 88424 个案例。关系按法规标题、别名或引文在案例正文中直接命中建立，并保存匹配来源、证据摘录与分数。

### 全量执行架构

- 入口：`scripts/run_full_case_vectorization.py`。
- 正文只从 `source_items(a2aj_case)` 读取；`legal_cases` 只存结构化元数据和关系，避免同一案例重复切片。
- 切片：`RecursiveCharacterTextSplitter`，BGE-M3 tokenizer 精确计数，512 token 块、64 token overlap。
- 断点：`vectorization_checkpoints(job_name='a2aj_case_bge_m3_v1')`；每页切片与检查点同一事务提交。
- 向量：`sentence-transformers / BAAI/bge-m3 / 1024`，pgvector cosine 距离。
- 索引：写入期暂停 HNSW，完成后并发创建 BGE-M3 的部分 HNSW 索引；检索继续由全文 GIN 和向量 ANN 经 RRF 融合。

### 验收口径

- 数据完整性：源案例数、已切片案例数、待向量数、1024 维度一致性、内容哈希一致性和空向量计数。
- 检索效果：`scripts/evaluate_case_retrieval.py` 对高置信度案例-法规关系做固定种子随机抽样，报告 `Recall@K`、`MRR@K`、P50/P95/均值延迟和失败样本。
- 限制：关系来自规则化正文命中，是可复现的代理金标，不等价于人工法律专家标注。最终数值仅在全量 BGE-M3 入库、索引完成和评测实际运行后写入。

## 2026-09-11 A2AJ 案例互引图

- 数据源：A2AJ `source_items.raw_json.cases_cited` 与 `cases_citing`。同步仅以 `cases_cited` 写入正向边，避免两字段镜像重复。
- `a2aj_case_citations`：保存引用方源 ID、被引中立引注、标准化引注、可选被引源 ID 和解析方式；未解析引注也保留。
- `case_case_citations`：仅保存引用方和被引方均能映射到 `legal_cases` 的确定边，并以外键、唯一约束和自引用检查保证图完整性。
- 命令：`python scripts\\sync_a2aj_case_citations.py`。该脚本为幂等 upsert，不影响 RAG 切片或向量化任务。

## 2026-09-11 当前运行快照

### 数据库状态

- A2AJ 有效案例源：`225762`；去重后的 `legal_cases`：`224376`。
- 案例-法规证据关系：`canada_case_law_links=111391`，其中 `89140` 个源案例至少有一条直接法规证据；结构化正式关系 `case_rule_relations=110410`。
- A2AJ 案例互引：源级 `a2aj_case_citations=1039332`，其中 `941326` 条可解析到本地源案例；结构化 `case_case_citations=933750`。完整性检查中孤儿边、自引用、重复边均为 `0`。

### 向量化状态

- 案例切片已完成：`3613411` 个 `a2aj_case` 切片，覆盖全部 `225762` 个有效案例。
- 当前嵌入模型：`sentence_transformers / BAAI/bge-m3 / 1024`。
- 已写入案例 BGE-M3 向量：`643301`，进度约 `17.80%`；任务处于 `embedding` 阶段，检查点名称为 `a2aj_case_bge_m3_v1`。
- 法规切片已在 RAG 语料中，但尚未迁移为当前 BGE-M3 向量。因此，法规语义检索、HNSW 最终索引和全量混合检索验收均未完成。

### 当前验收边界

- `match_score` 是正文证据匹配分数，不是人工法律正确率或检索准确率。
- 全量向量完成后，由 `scripts/verify_full_vectorization.py` 自动检查向量覆盖、内容哈希、维度和归一化，并执行固定随机样本的 `Recall@12`、`MRR@12` 与 P50/P95 延迟评测。
