# Linux + SQLite 部署说明

本文用于把项目以 SQLite fallback 方式部署到 Linux。该方式适合轻量 demo，不适合当前 PostgreSQL + pgvector + HNSW 主目标；如果需要完整向量数据库能力，请优先使用 `DEPLOY.md` 的 PostgreSQL 部署方式。

## 一、打包

在开发机项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package_linux_sqlite.ps1
```

产物通常位于：

```text
dist/legal-demo-sqlite-YYYYMMDD-HHMMSS.tar.gz
```

默认不包含：

- `.env`
- `.git`
- `venv`
- `tmp`
- `dist`
- 日志文件
- SQLite 数据库文件
- 原始导入数据
- 数据库备份

## 二、上传到服务器

```bash
scp dist/legal-demo-sqlite-*.tar.gz user@your-server:/tmp/
```

## 三、解压部署

```bash
ssh user@your-server
sudo mkdir -p /opt/legal-demo
sudo tar -xzf /tmp/legal-demo-sqlite-*.tar.gz -C /opt/legal-demo --strip-components=1
cd /opt/legal-demo
```

如果仓库包含自动部署脚本：

```bash
sudo bash scripts/deploy_linux_sqlite.sh \
  --app-dir /opt/legal-demo \
  --service legal-demo \
  --user legal-demo \
  --host 127.0.0.1 \
  --port 8000
```

## 四、手动配置

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
cp .env.sqlite.example .env
```

确认 `.env` 中使用 SQLite：

```dotenv
DATABASE_URL=sqlite:///./data/legal_demo.sqlite3
SESSION_SECRET=replace-with-a-long-random-string
```

## 五、模型配置

Qwen/OpenAI-compatible 示例：

```dotenv
LLM_PROVIDER=custom
CUSTOM_MODEL=qwen3.8-27b
CUSTOM_BASE_URL=https://your-openai-compatible-endpoint/v1
CUSTOM_API_KEY=your_api_key
```

SQLite 模式没有 pgvector/HNSW，向量会使用 JSON fallback，性能和能力不等同于 PostgreSQL 部署。

## 六、启动和检查

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

## 七、后续维护

```bash
python scripts/db_maintenance.py status
python rag_manage.py hybrid-search "contract good faith appeal" --module canada --source canada --limit 5
```
