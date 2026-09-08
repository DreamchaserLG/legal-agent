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
