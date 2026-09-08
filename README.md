# Legal Agent

这是一个法律检索、案例分析、风险评估和判决预测 demo。当前版本重点面向加拿大法律数据，使用 PostgreSQL + pgvector + RAG + 混合检索 + Qwen OpenAI-compatible 模型调用构建可运行的 Agent 原型。

## 当前能力

- 加拿大法律数据入库：A2AJ demo 数据和 Justice Canada 官方 `laws-lois-xml`。
- PostgreSQL 主数据库。
- pgvector 向量存储。
- HNSW + cosine 相似度检索。
- PostgreSQL `tsvector + GIN` 关键词检索。
- 结构化过滤：辖区、文档类型、法院层级、语言、日期。
- 混合检索：关键词检索 + 向量检索 + RRF + 本地 rerank。
- RAG 证据上下文。
- Qwen `qwen3.8-27b` OpenAI-compatible 问答/预测入口。
- 风险评估样本沉淀和人工反馈标签。

## 重要边界

本项目不是正式法律意见工具。系统回答必须基于本地检索证据，并在证据不足时明确说明不确定性。

当前版本不实现以下能力：

- 绕过反爬、robots、验证码、登录或访问频率限制。
- 批量下载未授权 CanLII 全文。
- 把未授权全文作为训练语料。
- 提交真实 `.env`、API key、数据库备份、模型权重或原始大语料。

## 快速运行

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.clean.example .env
```

编辑 `.env`，至少配置：

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:change_me@127.0.0.1:5432/legal_demo
SESSION_SECRET=replace-with-a-long-random-string
LLM_PROVIDER=custom
CUSTOM_MODEL=qwen3.8-27b
CUSTOM_BASE_URL=https://your-openai-compatible-endpoint/v1
CUSTOM_API_KEY=your_api_key
```

初始化和验证：

```powershell
python scripts\db_maintenance.py init-vector
python llm_healthcheck.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器访问：

```text
http://127.0.0.1:8000
```

健康检查：

```text
http://127.0.0.1:8000/health
```

## 数据入库

A2AJ demo：

```powershell
python scripts\ingest_open_legal_data.py --source a2aj --cases-per-config 8 --laws-per-config 8 --rebuild-limit 40
```

Justice Canada `laws-lois-xml` 批量导入示例：

```powershell
python scripts\ingest_open_legal_data.py --source laws-lois-xml --laws-lois-offset 0 --laws-lois-limit 2000 --skip-sync --skip-rebuild
python rag_manage.py rebuild --source canada
python rag_manage.py rebuild-vectors --source canada --module canada
```

## 检索验证

```powershell
python rag_manage.py --json vector-status
python rag_manage.py hybrid-search "contract good faith appeal" --module canada --source canada --limit 5
python rag_manage.py search "federal regulations" --module canada --source law --limit 5 --jurisdiction Canada --document-type statute
python scripts\benchmark_retrieval.py --repeat 2 --limit 5 --output data\eval\retrieval_benchmark_20260908.json
```

## 项目文档

- `CODEX.md`：项目记忆、技术栈、目录结构、当前进展和规范。
- `PLAN.md`：已批准的执行计划和阶段状态。
- `Act.md`：执行记录。
- `Make.md`：问题与后续方案。
- `result.md`：当前执行结果。
- `CLEAN_PACKAGE_README.md`：最小可运行压缩包使用说明。
