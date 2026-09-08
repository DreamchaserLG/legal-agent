# 最小可运行压缩包说明

本压缩包是 `legal-demo` 的清理版运行包，用于拷贝到其他电脑后重新安装依赖、配置环境并运行。

## 不包含的内容

- `.env`
- API key 和私人凭据
- 本地数据库文件或 PostgreSQL dump
- 本地模型权重
- 日志、缓存、临时文件
- 旧调试 HTML
- 旧压缩包
- 虚拟环境
- `data/imports/` 中的原始开放数据 clone
- `data/backups/` 中的数据库备份
- `_cleanup_quarantine_*/` 隔离区

## 安装依赖

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

如果使用本地 embedding 模型：

```powershell
pip install -r requirements-local-embedding.txt
```

GPU 版 PyTorch 请按 PyTorch 官方命令安装对应 CUDA 版本。

## 配置环境

```powershell
copy .env.clean.example .env
```

编辑 `.env`。

PostgreSQL 示例：

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:change_me@127.0.0.1:5432/legal_demo
SESSION_SECRET=replace-with-a-long-random-string
```

Qwen/OpenAI-compatible 示例：

```dotenv
LLM_PROVIDER=custom
CUSTOM_API_KEY=your_api_key
CUSTOM_MODEL=qwen3.8-27b
CUSTOM_BASE_URL=https://your-openai-compatible-endpoint/v1
```

embedding demo 示例：

```dotenv
EMBEDDING_PROVIDER=hash
EMBEDDING_MODEL=local-hash-embedding
EMBEDDING_DIMENSION=1024
```

生产语义检索应改为真实 embedding 服务或本地模型，并重建向量。

## 初始化数据库

确认 PostgreSQL 已安装并创建数据库后执行：

```powershell
python scripts\db_maintenance.py init-vector
```

如果需要查看状态：

```powershell
python scripts\db_maintenance.py status
python rag_manage.py --json vector-status
```

## 导入 demo 数据

```powershell
python scripts\ingest_open_legal_data.py --source a2aj --cases-per-config 8 --laws-per-config 8 --rebuild-limit 40
```

如果本机已经准备好 `data/imports/laws-lois-xml/`：

```powershell
python scripts\ingest_open_legal_data.py --source laws-lois-xml --laws-lois-limit 10 --rebuild-limit 80
```

全量导入建议按 offset 分批执行，避免长事务。

## 启动服务

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问：

```text
http://127.0.0.1:8000
http://127.0.0.1:8000/health
```

## 验证命令

```powershell
python llm_healthcheck.py
python rag_manage.py hybrid-search "contract good faith appeal" --module canada --source canada --limit 5
python scripts\benchmark_retrieval.py --repeat 2 --limit 5 --output data\eval\retrieval_benchmark_20260908.json
```
