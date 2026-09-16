# 执行结果

## 2026-09-10 当前数量、关联与优化实施

- 2026-09-10 最后查询时，A2AJ 有效原始案例为 `188557` 条；正式案例为 `1018` 条，源到正式案例同步率为 `0.54%`。全量导入尚未完成，因此不能将该比例误认为数据丢失或将旧案例关联率外推到全库。
- 旧正式案例中，至少关联一条法规的案例为 `544/1018`，覆盖率 `53.44%`。关联质量仍需在全量同步后用冻结人工评测集重新验收。
- 已修复 RAD NUL 字符阻断并恢复分区导入；已完成 P1 本地技能运行时的最小实现和审计测试。P0 的全量同步、关联、分块、BGE-M3 迁移仍依赖全量导入成功结束。

## 2026-09-10 二十万阈值后的物化状态

- A2AJ 全部 29 个 Parquet 分区已完成，上游行数 `225807`；其中 `225762` 条包含有效正文，差额为被质量规则跳过的空正文。
- 已启动分页快照物化。最后检查时 `legal_cases` 中 A2AJ 结构化案例为 `13223` 条，任务继续以来源 ID 上界为快照同步；关系和向量阶段不会抢跑。
- 向量全量入库已加入容量熔断。当前磁盘余量约 30GB，不能安全承载数百万个全文分块的 1024 维 HNSW 向量，普通结构化入库和法规关联优先执行。

## 2026-09-09 A2AJ 数据源恢复进展

- 已确认主数据源 A2AJ 的真实总量为 `225807` 条案例，完整导入尚在执行中，不能将当前已入库数量误报为全量完成。
- 当前采用 29 分区 Parquet + PostgreSQL 断点表恢复导入；检查时 `BCCA` 已完成 14700 行，`BCSC` 正在继续，`a2aj_case` 已超过 3 万条唯一记录。
- 本轮恢复已清理历史空正文记录，并取消 50000 字符截断；当前任务会在重跑分区时保留全文并幂等更新已有案例，全文补齐完成前不会启动后续全量关联和向量验收。
- 有效正文记录才允许进入后续同步链路：案例实体关联、法规关联、512 token/64 token overlap 分块、BGE-M3 向量化、HNSW/GIN 混合检索和最终完整性测试均会在全量源导入完成后自动运行。

## 2026-09-09 BGE-M3 与 A2AJ 当前结果

- 真实语义向量：已启用 `sentence-transformers` 的 `BAAI/bge-m3`，向量维度为 1024，使用余弦距离与 HNSW 索引。
- A2AJ 入库一致性：当前 `a2aj_case=11318`；在 9757 条快照中，唯一 `source_uid=9757`，重复数为 0；正文非空 9748，标题和来源 URL 覆盖 100%。9 条无正文源记录不会产生有效案例文本分块。
- 向量迁移进度：本轮校验时 BGE-M3 已写入 12224 个 RAG 分块；缺失分块、维度异常、内容哈希失配和非单位范数均为 0。剩余 hash 分块由恢复后的后台幂等任务持续转换。尚未同步的新 A2AJ 源记录不会被误当作已关联正式案例。
- 检索性能：纯 PostgreSQL HNSW 路径均值 1.17ms；BGE-M3 常驻模型生成均值 19.95ms；端到端向量检索均值 49.21ms，混合检索均值 187.91ms。模型首次加载约 8 秒，不计入常驻服务延迟。
- 已验证：`python -m py_compile app/service/embedding_service.py app/service/vector_store_service.py scripts/ingest_open_legal_data.py` 通过；BGE-M3 输出维度为 1024，向量归一化范数为 1。
- 后续待完成：A2AJ 全量源数据拉取完成后，执行结构化同步、案例-法规关联、案例 RAG 重建，并为新增案例及现有法规切片完成 BGE-M3 转换。
- 全量执行状态：已启动 `scripts/run_a2aj_full_pipeline.py --expected-cases 225000`。该脚本将当前数据量视为断点，只有源数据空页且总数达到 225000 后才执行关联和全量向量化；当前仍处于拉取阶段，尚未宣告全流程成功。
- 导入修复状态：已核验 A2AJ 总量为 225807 条。rows 模式在 14618 条发生 TLS 停滞，现已切换为 29 个 Parquet 分片和数据库检查点；BCCA 分片已从 4200 成功续传到 6600。当前案例总数尚未增长是由于该区间与先前 rows 导入记录重叠，upsert 正常去重。
- 当前切片规则：BGE-M3 tokenizer 精确 `512 token`，相邻块 `64 token` 重叠；分隔符顺序为段落、换行、中文句末、英文句末、空格和 token 窗口兜底。测试通过，所有分块不超过 512 token。
- 数据源核验：A2AJ 主数据集真实总量为 `225807`，不等于 25 万；当前主库已到 `19745` 条。Refugee Law Lab 候选源有 `195646` 条、许可为 CC BY-NC 4.0，暂未合并，避免许可证和重复判例风险。UBC CanLegalRAGBench 的 `1649` 条仅用作评测。

更新时间：2026-09-08 13:48 Asia/Shanghai

补充更新时间：2026-09-08 19:40 Asia/Shanghai

## 总体状态

当前阶段已完成可运行 demo 闭环：项目已清理，数据库已切换到 PostgreSQL，pgvector/HNSW 已启用，官方加拿大法规 XML 已批量入库，RAG 分块、向量索引、混合检索、Qwen 健康检查、风险样本和应用启动验证均已完成。

## 数据库快照

- 数据库后端：PostgreSQL
- `users`：3
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
- `agent_runs` / `agent_predictions` / `agent_chat_logs` / `search_histories`：0

## 数据来源

- A2AJ demo：已通过 HuggingFace datasets-server 的 `default/train` 导入 8 条案例和 8 条法规。
- Justice Canada：已 clone 官方 `justicecanada/laws-lois-xml` 到本地忽略目录 `data/imports/laws-lois-xml/`。
- Justice Canada XML：已按批次导入 15599 个 XML 文件，写入 `source_items` 后同步为加拿大法规结构数据。
- CanLII：实时检索和批量全文抓取已关闭；不实现绕过反爬、验证码、登录、robots 或访问频率限制的逻辑。

## 向量数据库

- pgvector：可用
- 索引：`idx_rag_chunk_embeddings_vector`
- 索引类型：HNSW
- 距离/相似度：cosine，operator class 为 `vector_cosine_ops`
- 参数：`m=16`，`ef_construction=64`，查询时 `ef_search=80`
- 当前 embedding：`hash / local-hash-embedding / 1024`

说明：hash embedding 只用于本地 demo 验证 pgvector/HNSW/RAG 管线，不代表最终语义检索质量。生产阶段应切换为本地 BGE-M3、Qwen embedding 或 OpenAI-compatible embedding 服务。

## 性能验证

基准文件：`data/eval/retrieval_benchmark_20260908.json`

- 向量检索：约 19-105 毫秒
- 关键词检索：约 112-894 毫秒
- 混合检索：约 335-926 毫秒
- 之前慢点：混合检索中关键词通道错误使用扩展词，导致部分查询 6-19 秒。
- 已修复：关键词通道使用原始查询，向量通道保留扩展查询；PostgreSQL 关键词检索移除正文 `LIKE` 全表扫描，改为 `tsvector + GIN`。

## 模型验证

- `LLM_PROVIDER=custom`
- `CUSTOM_MODEL=qwen3.8-27b`
- OpenAI-compatible 地址和 API key 已保存到本地 `.env`，未写入 Git 文档和模板。
- `python llm_healthcheck.py` 已通过，模型返回有效结构化 JSON。

## 应用验证

- 编译检查通过：`python -m compileall app scripts canlii_ingest.py rag_manage.py export_rag_data.py llm_healthcheck.py`
- RAG 状态检查通过：`python rag_manage.py --json status`
- 向量状态检查通过：`python rag_manage.py --json vector-status`
- 混合检索检查通过：`python rag_manage.py hybrid-search "contract good faith appeal" --module canada --source canada --limit 5`
- 结构化过滤检查通过：`python rag_manage.py search "federal regulations" --module canada --source law --limit 5 --jurisdiction Canada --document-type statute`
- 应用启动冒烟测试通过：`/health` 返回 `200` 和 `{"status":"ok","app":"Legal Demo MVP"}`

## 本地交付物

- 最小可运行压缩包：`dist/legal-demo-pgvector-agent-minimal-20260908-134817.zip`
- 压缩包不包含 `.env`、API key、数据库备份、原始导入数据、parquet、模型权重、隔离区、虚拟环境和 Git 历史。

## Git 同步

- 已提交并推送到 `origin/main`。
- 提交号：`f4b4fb0`

## 尚未完成

- A2AJ parquet 直连下载在当前网络环境下连接 HuggingFace 超时；已保留代码能力和 demo 默认数据，后续可在网络畅通时继续。
- 真实语义 embedding 未启用；当前使用 hash fallback。
- 没有进行模型微调训练；当前完成的是风险样本沉淀和人工反馈闭环入口。
- MCP/A2A 工具调度尚未实现，只保留后续接入边界。

## 2026-09-08 检索案例补充结果

本次修复目标：解决快速检索只返回法规、不返回对应案例、不显示案例相似度的问题。

本地数据库检查结果：

- `legal_cases`：8
- A2AJ 案例 `source_items`：8
- RAG 案例分块：237
- RAG 法规分块：203346
- `case_rule_relations`：4
- `canada_case_law_links`：4

结论：本地不是完全没有案例，但案例量非常少，且正式案例-法规关联表很稀疏。之前 Top-N 被法规分块占满，所以页面表现为只有法规。

已完成修复：

- 加拿大快速检索改为法规和案例分路召回。
- 案例结果按 `similarity_score` 排序。
- A2AJ 本地案例统一进入案例分组。
- 法规卡片挂载当前查询命中的相关案例。
- 同名法规按“标题 + 引用”去重。

验证结果：

- `estate`：返回 3 条法规 + 2 条案例；案例相似度 `1.0`、`0.8`；每条法规挂载 2 条相关案例。
- `contract`：返回 2 条法规 + 1 条案例；法规挂载相关案例。
- `遗产争议`：返回 5 条法规 + 2 条案例；中文关键词映射和案例分路检索正常。
- 三组平均检索延迟约 `140.69ms`，平均分析延迟约 `75.53ms`。

仍需扩库：

- 当前 8 条案例只能用于 demo 验证，无法支撑真实法律检索覆盖。
- 推荐优先导入 `a2aj/canadian-case-law` 的更多案例，再重建 RAG 和向量索引。

## 2026-09-09 legal_cases 扩库与关联结果

本次目标：获取更多加拿大 `legal_cases`，完成入库、案例-法规正式关联、案例 RAG/向量重建，并验证检索和分析入口。

已完成：

- A2AJ HuggingFace viewer 连通验证成功。
- 从 `a2aj/canadian-case-law` 的 `default/train` 分 `10` 批导入 offset `18` 到 `918`，每批 `100` 条，共新增 `1000` 条案例源数据。
- `source_items` 中 `a2aj_case` 增加到 `1018` 条。
- `legal_cases` 增加到 `1018` 条。
- `canada_case_law_links` 增加到 `1016` 条。
- `case_rule_relations` 增加到 `1016` 条。
- 已有关联法规的案例为 `544` 条。
- 案例 RAG 分块增加到 `26285` 条。
- 总向量数量增加到 `229631` 条。

代码处理结果：

- 修复案例-法规关联性能瓶颈：不再执行“所有案例 × 所有法规”的全量交叉匹配，改为从案例正文抽取法规标题候选后再匹配。
- 修复 `rag_manage.py rebuild --source case` 的底层过滤：现在只处理案例源和 `legal_cases`。
- 快速检索结果优先使用 `case_rule_relations` 正式关联；命中案例的正式关联法规会反向补入 `relevant_laws`。
- 法规卡片优先挂载正式关联案例；没有正式关系时才使用 `retrieved_pending_relation` 临时相似度挂载。

验证结果：

- 编译检查通过：`python -m compileall app scripts\ingest_open_legal_data.py rag_manage.py`。
- RAG 状态检查通过：案例分块 `26285`、法规分块 `203346`。
- 向量状态检查通过：pgvector/HNSW 可用，总向量 `229631`。
- `family law child support appeal`：返回 `3` 条案例、`4` 条法规；`Family Law Act` 由正式关联补入，并挂载 `Duggan v. White`、`Graydon v. Michel`。
- `criminal code sentencing appeal`：返回 `3` 条案例、`8` 条法规；案例均带正式关联法规。
- `租赁 合同 违约`：返回 `5` 条案例、`6` 条法规，案例按 `similarity_score` 排序。
- 分析入口 `analyze_sentence_search("family law child support appeal")` 走 `local_fast_rag`，返回 `5` 条 supporting cases 和 `8` 条法规。

当前限制：

- 本轮完成的是约千条级 demo 扩库，不是 A2AJ 全量 `22.5` 万条案例入库。
- 当前 embedding 仍是 `hash / local-hash-embedding / 1024`，用于本地管线验证；生产质量需要切换真实语义 embedding 后重建向量。
- 关联仍主要依据法规标题直接出现，少量程序性法规会作为背景引用出现，后续需要增加引用角色分类和降权规则。
## 2026-09-09 BGE-M3 分块向量迁移流水线交付

交付文件：`run_migration.py`、`schema.sql`、锁定依赖的 `requirements.txt`，以及迁移环境变量模板 `.env.example`。

实现结果：`cases_metadata` 持有案例全文和元数据，`case_chunks` 持有 `case_id`、`chunk_index`、`chunk_text`、`VECTOR(1024)` 与自动生成的全文检索列。分块采用 tokenizer 精确计数的 512 token / 64 token overlap，BGE-M3 模型由 `hf-mirror.com` 下载至 `HF_HOME` 后本地加载，模型加载路径、8192 最大长度与设备会写入 JSON Lines 日志并输出到控制台。

自动化保障：批量源读取固定为 500 案例；分块写入使用 `execute_values`；检查点支持中断续传；CUDA OOM 自动 CPU 降级；向量生成有指数退避重试；三类索引通过独立 autocommit 连接并行 `CREATE INDEX CONCURRENTLY`。随机抽样最多 1000 个分块，写入向量与相同 `chunk_text` 重编码向量均值低于 0.95 即异常退出且不切换表。通过后以事务将暂存表切换为正式表，旧 `case_embeddings` 备份保留 72 小时。

验证：`python -m py_compile run_migration.py` 通过，关键约束扫描通过。本机环境缺少 `pgvector` Python 包，执行入口已验证为结构化报错并以 `[RESULT]: FAILED` 结束，不会接触数据库或下载模型；安装 `pip install -r requirements.txt` 后运行 `python run_migration.py` 即可执行真实迁移。
## 2026-09-09 当前案例检索验收与长尾优化

当前案例数量为 `1018`，每条均保存正文；正式案例-法规关系 `1016` 条，覆盖 `544` 个案例。使用 50 个正式关联案例标题作为基础查询，`case_recall@12=92%`、`formal_law_recall@8=96%`、`joint_association_recall=92%`，达到 90% 关联准确率验收线。

运行时修复：长案例标题被拆成多个泛词后，原检索会用 `websearch_to_tsquery OR prefix tsquery` 扫描大量分块并排序，单请求可达 20.47 秒。现已改为精确全文查询优先、零命中才前缀回退，并排除低选择性词。复测同一长标题首次 `264.79ms`、热态 `117.45ms`；50 条基准平均 `85.55ms`，P95 `477.81ms`，最大 `602.97ms`。

向量标准检查未通过：现有 `229631` 个向量全部为 `hash/local-hash-embedding/1024`，真实语义向量为 0。真实 BGE-M3 分块迁移脚本已存在，但当前解释器缺少 `pgvector`，项目 `venv` 缺少 `sentence-transformers`；因此本轮没有运行会写入数据库的迁移。旧 Ontario 租赁评测集只有 `21.43%` 的预期实体仍存在于当前 A2AJ 语料，不适合作为本轮 A2AJ 检索准确率结论。

## 2026-09-10 当前可核验结果

- A2AJ 有效案例源：`225762`；结构化 `legal_cases`：`224376`。
- 案例-法规图：`canada_case_law_links=111391`；正式关系 `case_rule_relations=110410`；有至少一条正式关系的案例 `88424`。该覆盖率约为 `39.4%`，不应误读为全库关联准确率。
- 全量 BGE-M3 向量任务正在运行，检查点表为 `vectorization_checkpoints`，日志为 `logs/full_case_vectorization.jsonl`。当前阶段是精确 token 切片，完成后才进入 1024 维向量写入和 HNSW 构建。
- 已新增独立评测入口 `scripts/evaluate_case_retrieval.py`。最终将基于固定随机抽样、明确的关系金标代理和 `Recall@12`、`MRR@12`、P50/P95 端到端延迟输出实际结果；本记录不预先承诺 99% 或任何固定准确率。

## 2026-09-11 A2AJ 案例互引关系

- 新增源级表 `a2aj_case_citations` 和结构化表 `case_case_citations`。
- A2AJ 原始数据集提供案例互引字段，未提供案例-法规金标；因此案例-法规关系仍由正文中的法规标题、引文与别名证据生成。
- 当前案例互引边 `933750` 条，源级引用解析覆盖率 `90.57%`，完整性校验中的孤儿边、自引用和重复边均为 `0`。

## 2026-09-11 最新可核验结果

| 项目 | 当前结果 | 状态 |
| --- | ---: | --- |
| A2AJ 有效案例源 | 225762 | 已完成导入 |
| 结构化案例 | 224376 | 已完成物化与去重 |
| 案例切片 | 3613411 | 已完成 |
| 案例 BGE-M3 向量 | 643301（17.80%） | 正在生成 |
| 案例-法规源级关系 | 111391 | 已完成，带证据摘录 |
| 正式案例-法规关系 | 110410 | 已完成 |
| A2AJ 源级案例互引 | 1039332 | 已完成 |
| 结构化案例互引 | 933750 | 已完成，完整性校验通过 |

当前不能宣称最终检索准确率、案例法规语义关联质量或秒级延迟已验收，原因是案例 BGE-M3 向量尚未完成，法规 BGE-M3 向量尚未迁移，活动 HNSW 索引和独立抽样评测尚未运行。后续自动验收将输出实际 `Recall@12`、`MRR@12`、P50/P95 延迟与失败样本，不预设 99% 或其他固定指标。
