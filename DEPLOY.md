# Legal Demo 部署指南

本文用于把项目部署到服务器或新电脑。推荐 PostgreSQL + pgvector 运行方式；SQLite 只适合轻量 demo。

## 一、准备环境

需要：

- Python 3.11
- PostgreSQL 17.x 或兼容版本
- pgvector 扩展
- Git
- 可选：Docker / Docker Compose

Ubuntu/Debian 安装 Docker 示例：

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable docker
sudo systemctl start docker
docker --version
docker compose version
```

## 二、获取项目

```bash
git clone <your-repo-url> /opt/legal-demo
cd /opt/legal-demo
```

如果使用压缩包：

```bash
mkdir -p /opt/legal-demo
tar -xzf legal-demo-pgvector-agent-minimal-*.tar.gz -C /opt/legal-demo --strip-components=1
cd /opt/legal-demo
```

## 三、安装依赖

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
```

如需本地 embedding 模型：

```bash
pip install -r requirements-local-embedding.txt
```

## 四、配置环境变量

```bash
cp .env.clean.example .env
```

编辑 `.env`：

```dotenv
DATABASE_URL=postgresql+psycopg2://postgres:change_me@127.0.0.1:5432/legal_demo
SESSION_SECRET=replace-with-a-long-random-string
LLM_PROVIDER=custom
CUSTOM_MODEL=qwen3.8-27b
CUSTOM_BASE_URL=https://your-openai-compatible-endpoint/v1
CUSTOM_API_KEY=your_api_key
```

当前 demo 可使用：

```dotenv
EMBEDDING_PROVIDER=hash
EMBEDDING_MODEL=local-hash-embedding
EMBEDDING_DIMENSION=1024
```

生产语义检索应改为真实 embedding，并重新构建向量。

## 五、初始化数据库

```bash
python scripts/db_maintenance.py init-vector
python scripts/db_maintenance.py status
```

如果需要直接执行 SQL，可参考：

```bash
psql "$DATABASE_URL" -f sql/legal_agent_demo.sql
psql "$DATABASE_URL" -f sql/postgres_vector_schema.sql
```

## 六、导入数据

A2AJ demo：

```bash
python scripts/ingest_open_legal_data.py --source a2aj --cases-per-config 8 --laws-per-config 8 --rebuild-limit 40
```

Justice Canada XML 本地目录准备好后：

```bash
python scripts/ingest_open_legal_data.py --source laws-lois-xml --laws-lois-offset 0 --laws-lois-limit 2000 --skip-sync --skip-rebuild
python rag_manage.py rebuild --source canada
python rag_manage.py rebuild-vectors --source canada --module canada
```

## 七、启动服务

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 八、验证检索

```bash
python rag_manage.py --json vector-status
python rag_manage.py hybrid-search "contract good faith appeal" --module canada --source canada --limit 5
python scripts/benchmark_retrieval.py --repeat 2 --limit 5 --output data/eval/retrieval_benchmark_20260908.json
```

## 九、安全注意

- 不要提交 `.env`。
- 不要提交 API key。
- 不要把数据库备份、原始大语料、模型权重或隔离区提交到 Git。
- 不要通过技术手段绕过第三方站点限制。
