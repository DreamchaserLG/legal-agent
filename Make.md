# 问题与后续方案

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
