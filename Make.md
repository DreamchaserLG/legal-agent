# 问题与后续方案

## 2026-09-09 A2AJ 数据恢复问题

- 问题：datasets-server rows 接口在 offset `14618` 后反复发生 TLS 失败，不适合作为全量无人值守通道；历史通道还写入了少量只有元数据、没有正文的记录。
- 处理：使用 Hugging Face 镜像优先、官方地址回退的 Parquet 分区下载；每个分区在 PostgreSQL 保存已处理行号。导入函数对空正文返回跳过标记，不创建 `source_items`、关键词或后续向量任务。
- 问题：旧全量入口默认截断 50000 字符，会丢失长篇裁判书后半部分。处理：A2AJ 全量流水线默认 `0` 表示不截断，已重新从分区检查点开始幂等覆写历史记录；旧截断数会随着各分区完成归零。

## 2026-09-10 RAD 分区 NUL 字符阻断

- 问题：A2AJ 的 `RAD` Parquet 分区在第 2700 行后含有 NUL 字符（`\\x00`）。PostgreSQL 的 `text` 与 `jsonb` 不接受 NUL，导致同一断点自动重试后仍然失败。
- 处理：在统一 `repair_text` 入口删除 NUL，保证正文、标题、摘要和 JSON 元数据在进入数据库前一致清洗；不跳过该案例。恢复任务将从原分区检查点继续。

## 2026-09-10 二十万案例结构化冲突

- 问题：A2AJ 存在相同标题、法院和裁判日期的多来源记录。`legal_cases` 的语义唯一索引按预期拒绝了直接 `INSERT ... SELECT` 的重复实体。
- 处理：快照物化改为分页读取并复用既有 `_upsert_legal_case_from_source`。该逻辑按来源 ID、URL 和语义身份查找并更新，保留原始多来源记录，同时避免重复创建结构化案例。
- 补充：集合式加速物化还发现同一裁判书 URL 可对应不同上游元数据。写入 `legal_cases` 前现按规范化 URL 去重，再按标题、法院和日期去重；两个约束只作用于派生实体，不删除 `source_items` 原始来源。

## 2026-09-10 全量完成门禁

- 问题：A2AJ 中少量记录没有正文，导入器按质量规则跳过。旧流水线只比较 `source_items` 有效正文数量与上游总数，会在所有分区处理完成后仍误报失败。
- 处理：最终门禁同时检查有效正文入库数和已完成 Parquet 分区的上游行数。只有两者都低于期望才失败；跳过数量仍会写入导入结果，避免隐藏数据质量问题。
- 备用源评估：Refugee Law Lab 数据集规模约 195646 条但为 CC BY-NC 4.0，且与 A2AJ 多法院分区可能重合；不自动混入主库。CanLegalRAGBench 仅作为评测集，不能作为主语料扩容。

## 2026-09-09 BGE-M3 迁移期间的运行问题

### 1. Hugging Face rows 接口偶发 TLS 断连

现象：A2AJ 从 offset `2118` 继续拉取时出现 `SSLEOFError`，旧逻辑会结束整个持续导入任务。

处理：rows 请求已改为最多 5 次指数退避重试；`auto` 模式在重试全部失败时自动回退 parquet。当前使用 `viewer` 模式时会记录失败 offset，可从该断点重启。

### 2. BGE-M3 查询与迁移争用同一张 GPU

现象：迁移进程占用约 5.5GB 显存时，另一个新进程加载 BGE-M3 可能占满 8GB 显存并使基准超时。

处理：迁移脚本可安全中断并按未完成切片恢复；服务验证应在迁移暂停或迁移完成后执行。生产部署应让在线服务和离线迁移使用不同 GPU，或给迁移进程显式设置 CPU 设备。

### 3. PostgreSQL 未使用 HNSW

现象：迁移期间统计信息陈旧时，优化器将 BGE-M3 行数误估为 1，改走模型索引、回表并排序，单次 SQL 约 185ms。

处理：已创建当前模型的部分 HNSW 索引并执行 `ANALYZE`，查询计划已切换到 HNSW；代码会在迁移过程中定期刷新统计信息。

### 4. 全量任务耗时不能被误报为已完成

现象：A2AJ 目标规模为 22.5 万条以上，导入、关联和 BGE-M3 向量化均为长时间任务，单次前台命令不能在短时间内完成。

处理：新增无交互编排器，只有源数据达到 `225000` 且上游返回空页后才进入同步和向量化；最终仅在所有质量门禁通过时输出 `[RESULT]: SUCCESS`。当前仍在源数据拉取阶段。

### 5. A2AJ rows 接口在 14618 条后不稳定

现象：`datasets-server.huggingface.co/rows` 在 offset `14618` 可偶发返回 200，但应用请求可连续 5 次 TLS 失败；长退避期间数据量不再增长。

处理：已确认数据集由 29 个 Parquet 分片组成、总计 225807 条。全量编排器改用镜像优先的 Parquet 下载与 `a2aj_parquet_checkpoints` 检查点，BCCA 已验证从 4200 恢复到 6600；不再依赖 rows 接口完成全量导入。

### 6. 实际 RAG 分块规则与迁移脚本不一致

现象：`run_migration.py` 已实现 512 token/64 token overlap，但在线 RAG 仍使用 1800 字符/180 字符规则。

处理：`app/service/rag_service.py` 已统一为 BGE-M3 tokenizer 精确计数、`RecursiveCharacterTextSplitter` 优先语义边界和 token 窗口兜底；本地 snapshot 路径加载避免失效代理触发网络请求。

### 7. A2AJ 主数据集不足 25 万条

现象：A2AJ `default/train` 正式元数据显示为 225807 条，不应宣传为 25 万真实案例。

处理：将 A2AJ 作为 MIT 许可主库并完整导入。候选补充源 Refugee Law Lab 有 195646 条，但为 CC BY-NC 4.0 且可能与 A2AJ 的联邦案件重叠；在确认部署用途和完成 citation 级去重前不自动合并。UBC 基准集只用于质量评测。

## 2026-09-07

### 1. CanLII 全文批量获取不执行

原因：不能绕过反爬、robots、验证码、登录、访问频率限制或封禁策略，也不能批量下载未授权全文。

当前处理：关闭 CanLII 实时检索和批量全文获取，改用 A2AJ 开放数据集和 Justice Canada 官方 `laws-lois-xml`。

后续方案：如果取得 CanLII 官方授权和 API key，只接入官方允许的 metadata、citator 或其他授权接口。

### 2. A2AJ parquet 直连下载超时

现象：`LEGISLATION-FED/train.parquet` 通过 HuggingFace 直连下载时，沙箱内和外部网络权限重试均在连接 `huggingface.co` 时超时。

当前处理：保留 parquet 模式代码，继续使用已成功的 HuggingFace datasets-server `default/train` demo 数据；不中断本地闭环。

后续方案：
- 在网络可访问 HuggingFace 的环境重新运行 parquet 导入。
- 或手动下载 parquet 到 `data/imports/a2aj-cache/` 后使用本地缓存导入。
- 对超大案例 parquet 设置分批、断点续跑和下载大小上限。

### 3. 真实语义 embedding 尚未启用

现象：本机配置路径 `D:\environment\embeddingModel\bge-m3` 不存在。

当前处理：临时使用 `hash / local-hash-embedding / 1024`，保证 pgvector、HNSW、RAG、混合检索和 API 管线可运行。

后续方案：
- 安装或下载 BGE-M3 到本机路径，并设置 `EMBEDDING_PROVIDER=local`。
- 或配置 OpenAI-compatible embedding 服务，并设置 `EMBEDDING_PROVIDER=custom`、`EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`。
- 切换 embedding 后必须重新执行 `python rag_manage.py rebuild-vectors --source canada --module canada`。

### 4. 关键词检索与混合检索已优化但仍受文本规模影响

现象：全量法规分块后，关键词检索从 6-9 秒降到约 0.1-0.7 秒，混合检索从 6-19 秒降到约 0.3-0.9 秒；已达到当前 demo 秒级目标，但不是严格所有查询都保证毫秒级。

当前处理：使用 PostgreSQL `tsvector + GIN`，移除正文 LIKE 全表扫描；混合检索关键词通道使用原始查询，向量通道保留扩展查询。

后续方案：
- 对法规 XML 做 section-level chunking，减少重复大段全文。
- 建立专门的候选召回表或 materialized view。
- 针对常见法律问题建立评测集并调参。

### 5. 风险评估训练仍处于样本沉淀阶段

现象：当前已建立 `risk_assessment_samples` 和 `risk_feedback_labels`，并写入 3 条 demo 样本，但没有完成模型微调或大规模标注训练。

当前处理：预测运行后会沉淀风险样本，管理接口支持读取样本和写入人工反馈。

后续方案：
- 导出风险样本，建立人工标注规范。
- 增加真实案例标签和负样本。
- 用检索证据、风险因素、最终结果训练/评估风险分类器或 reranker。

### 6. MCP/A2A 工具调度尚未实现

当前处理：本阶段只完成数据、检索、风险样本和模型调用基础闭环。

后续方案：在当前 service 分层基础上增加 MCP 工具注册、权限控制、任务 trace、失败重试和人工审批节点。

## 2026-09-08

### 7. 本地案例量不足导致案例覆盖有限

现象：快速检索修复后已经可以返回案例并按相似度排序，但本地唯一案例只有 8 条，案例分块 237 条，正式案例-法规关联只有 4 条。常见关键词只能命中 1-4 个案例，无法覆盖足够多的法律领域。

当前处理：加拿大快速检索改为法规与案例分路召回；案例结果按 `similarity_score` 排序；法规卡片会挂载当前查询命中的相关案例。正式关联表不足时，以本次查询的案例相似度做临时关联展示。

后续方案：

- 优先扩充 `a2aj/canadian-case-law`。该数据集目前约 22.5 万条加拿大法院和 tribunal 决定，字段包含案例名称、引用、日期、来源 URL、英文/法文全文、`cases_cited`、`cases_citing` 和 `citing_cases_count`。
- 使用 A2AJ GitHub 仓库 `https://github.com/a2aj-ca/canadian-legal-data` 的访问示例；其支持 API、Hugging Face、Parquet、MCP 四类访问方式。
- 如果需要在线查询而不是全量入库，可接入 A2AJ 公共 API / MCP：`https://api.a2aj.ca/docs`、`https://api.a2aj.ca/mcp`。
- 如果需要自托管搜索服务，可参考 `https://github.com/a2aj-ca/a2aj-api-public`，该项目用 Hugging Face 数据集同步到 MongoDB + Elasticsearch，并提供搜索、fetch 和 MCP 能力。
- CanLII 官方 API 可以作为授权 metadata 来源，但需要申请 API key，并且只能按官方接口和条款使用，不做反爬绕过。

建议执行顺序：

```powershell
python scripts\ingest_open_legal_data.py --source a2aj --a2aj-mode viewer --cases-per-config 1000 --laws-per-config 0 --skip-rebuild
python rag_manage.py rebuild --source canada
python rag_manage.py rebuild-vectors --source canada --module canada
```

如果 Hugging Face viewer 分页稳定，可以逐步增加 `--a2aj-offset` 批量导入；如果需要全量，建议改用 parquet 缓存导入或 A2AJ 自托管 API，避免逐页 viewer 请求过慢。

## 2026-09-09

### 8. 案例扩库后正式关联同步过慢

现象：新增 10 条案例源后直接执行 `sync_canada_legal_data(force=True)`，在 `18` 条案例规模下 `180s` 仍未完成。

原因：原 `bootstrap_canada_law_graph` 会把所有加拿大案例与所有加拿大法规做交叉匹配。法规已超过一万条，扩库后复杂度会快速放大。

当前处理：
- 已将案例-法规关联改为候选匹配：先从案例正文抽取明确出现的法规标题，再只匹配对应候选法规。
- `18` 条案例规模下，`bootstrap_canada_law_graph(force=True)` 从超时降到约 `16.24s`，后续同步 `1018` 条案例可在约 `152.52s` 完成。
- 后续 `case_rule_relations` 和 `canada_case_law_links` 均已达到 `1016` 条。

后续方案：
- 继续扩库到万级以上时，应改为真正的增量同步，只处理新增 `source_items`，不要每次重跑全部 `legal_rules`。
- 可为法规别名建立专门索引表，例如 `canada_law_aliases(normalized_alias, law_id)`，把 Python 侧候选匹配进一步下推到 SQL。
- 可增加“引用号 / section / statute citation”解析，提升正式关联准确率。

### 9. 正式关联仍存在少量宽泛程序性法规噪声

现象：部分查询会同时出现 `Criminal Appeal Rules`、省级 Court of Appeal Rules 等程序性法规；这些法规可能是案例正文程序背景，不一定是实体争点法律依据。

原因：当前正式关联主要依据案例正文直接提到法规标题，尚未区分“程序背景引用”和“实质争点依据”。

当前处理：
- 快速检索展示已改为优先使用 `case_rule_relations` 正式关联。
- 从命中案例的正式关联反向补入法规，例如家庭法查询会补入 `Family Law Act`，并挂载正式关联案例。
- 没有正式关系时仍保留 `retrieved_pending_relation`，避免把临时相似度挂载误标为正式关系。

后续方案：
- 在 `case_rule_relations` 增加或派生 `relation_role`：`substantive_issue`、`procedural_context`、`background_reference`。
- 使用段落位置和上下文关键词重新打分，例如 `issue`、`held`、`analysis`、`under s.` 附近的法规优先，法院标题、appeal procedure、costs 背景降权。
- 对高频程序性规则建立降权白名单，避免其在普通实体争议查询中长期排在前列。
## 10. 分块迁移的来源与 ID 冲突边界

问题：当前项目的案例正文来自 `legal_cases` / `rag_chunks`，旧 hash 向量在 `rag_chunk_embeddings`。独立 A2AJ 数据库若和主库存在不同案例但相同 ID，目标 `cases_metadata.id` 会发生冲突。

处理：脚本按 `case_embeddings`、`legal_cases`、`rag_chunks` 的顺序自动探测，也可使用 `SOURCE_*` 与 `A2AJ_*` 显式配置。A2AJ 使用独立库时，自动探测不到时再设置 `A2AJ_SOURCE_TABLE`；不同 ID 空间必须设置足够大的 `A2AJ_ID_OFFSET`，案例编号同时带 `primary:` 或 `a2aj:` 前缀保证唯一。

后续建议：确认全量 22.5 万案例的主键策略后，可将来源代码单列并使用联合唯一键。该变更超出本次指定的 `case_id` 外键结构，因此本次没有擅自扩展表字段。

## 11. BGE-M3 模型下载和长文书分块条件

问题：BGE-M3 首次下载需要网络与本地缓存空间，长案例也不能直接整体编码。

处理：脚本在 Hugging Face 导入前强制使用 `https://hf-mirror.com`，发生 `OSError` 时每 10 秒重试一次、最多 3 次。模型载入后输出缓存路径、8192 最大序列长度和就绪标记；CUDA 显存不足会自动切到 CPU。案例通过 tokenizer 计数的 `RecursiveCharacterTextSplitter` 分为 512 token、64 token 重叠的块，超长无分隔文本才按同一 tokenizer 的 token 边界兜底。

后续建议：在 CI 镜像中预热 `HF_HOME` 模型缓存并为 HNSW 建索引预留内存；缓存和运行日志已加入 Git 忽略规则。
## 12. 正式关联覆盖率与真实语义向量未切换

问题：正式案例-法规关系的检索准确率在抽样基准中已达到 92%，但 1018 个案例中只有 544 个拥有正式关系，覆盖率只有 53.44%。此外，229631 个现有 RAG 向量全部是 hash 向量，不能作为真实语义相似度质量的证据。

处理：本轮不把 `retrieved_pending_relation` 作为正式关联准确率统计，只用 `case_rule_relations` 的真实关系进行测试。已交付的 `run_migration.py` 可切换为 BGE-M3 分块语义向量，但本机 Python 缺少 `pgvector`，`venv` 缺少 `sentence-transformers`，因此没有在依赖不完整时擅自启动数据库迁移。

后续建议：先安装锁定依赖并在维护窗口运行 `python run_migration.py`，再使用含人工标注的案例-法规测试集验证真实语义召回。对于未关联的 474 个案例，应先补充引用解析和人工抽样复核，再提高关系覆盖率，不能直接用宽松标题匹配凑足覆盖率。

## 2026-09-10 全量语义向量化的已知问题与处理

- **规模与空间**：`225762` 条有效原文约 `5.62 GB`，按已经写入的样本观测，案例平均约 `18.5` 个切片。向量化任务设定 `20 GiB` 磁盘保护阈值；低于阈值即失败退出，但保留数据库检查点，不继续写满磁盘。
- **吞吐瓶颈**：初版每个 chunk 单独执行 SQL，首批吞吐不足以覆盖全量。已改为每页聚合 payload 后的 PostgreSQL 批量 `INSERT ... ON CONFLICT DO UPDATE`，检查点与整页事务绑定；异常时该页回滚并从前一检查点重试。
- **HNSW 写放大**：数百万向量逐条插入时维护通用 HNSW 和活动模型 HNSW 会显著放大 IO 与磁盘占用。入库期间通过 `RAG_VECTOR_DEFER_INDEXES=true` 暂缓两类索引，完成后只创建当前 BGE-M3 查询所需的部分 HNSW cosine 索引。
- **评测边界**：已建立的案例-法规关系来自正文中法规标题、引文或别名的直接命中，不是人工金标。最终报告必须明确此限制，并额外记录失败样本；不得将关系匹配分数直接称为检索准确率。

## 2026-09-11 当前风险、限制与处理方案

- **法规语义向量未完成**：当前全量作业只迁移 `a2aj_case` 的 BGE-M3 向量；法规切片仍主要依赖全文 GIN 和旧向量记录。处理方案：案例阶段完成后，执行法规切片的同模型迁移，再构建一次活动 HNSW 索引并进行全量混合检索评测。
- **关系可靠性边界**：`111391` 条案例-法规关系均可在保存证据摘录中找到匹配别名，但这只能证明文本提及，不能证明法规在案件中具有决定性法律适用。处理方案：建立法律人员分级标注的保留集，单独报告 `Precision@5`、`Recall@12/50`、`MRR@10` 和 `nDCG@10`，并保存误报、漏报样本。
- **案例互引解析缺口**：A2AJ 有 `1039332` 条源级正向引用边，`941326` 条能按中立引注解析到本地案例，仍有约 `9.43%` 未解析。可能原因是被引案例不在本地快照、引注格式差异或跨库引用。处理方案：保留未解析引注，后续扩库或补充引注规范化规则后重跑幂等同步。
- **最终性能尚不可下结论**：HNSW 被有意推迟至全量写入后创建，避免数百万次增量维护造成写入放大。处理方案：待案例与法规 BGE-M3 向量齐全后，记录冷/热查询的嵌入、ANN、全文和混合检索 P50/P95，并以端到端 P95 小于 1 秒作为工程验收目标。
