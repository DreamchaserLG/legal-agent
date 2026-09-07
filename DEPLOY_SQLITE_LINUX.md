# Linux + SQLite 部署说明

这份说明用于把当前项目打包后部署到 Linux 服务器，并使用 SQLite 数据库运行。它不依赖 Docker，也不需要 PostgreSQL。

## 一、在开发机打包

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package_linux_sqlite.ps1
```

打包产物会生成在：

```text
dist/legal-demo-sqlite-YYYYMMDD-HHMMSS.tar.gz
```

默认不会打包以下内容：

- `.env`、`.env.production`
- `.git`
- `venv`
- `tmp`、`dist`
- 日志文件
- SQLite 数据库文件

如果确实需要连同 `data_archive` 一起打包：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package_linux_sqlite.ps1 -IncludeDataArchive
```

## 二、上传到服务器

示例服务器路径使用 `/opt/legal-demo`：

```bash
scp dist/legal-demo-sqlite-*.tar.gz user@your-server:/tmp/
```

## 三、服务器部署

登录服务器：

```bash
ssh user@your-server
```

创建目录并解压部署：

```bash
sudo mkdir -p /opt/legal-demo
sudo tar -xzf /tmp/legal-demo-sqlite-*.tar.gz -C /opt/legal-demo --strip-components=1
cd /opt/legal-demo
sudo bash scripts/deploy_linux_sqlite.sh \
  --app-dir /opt/legal-demo \
  --service legal-demo \
  --user legal-demo \
  --host 127.0.0.1 \
  --port 8000
```

脚本会完成：

- 安装 `python3`、`python3-venv`、`python3-pip`、`sqlite3`
- 创建 Linux 服务用户 `legal-demo`
- 创建 Python 虚拟环境
- 安装 `requirements.txt`
- 复制 `.env.sqlite.example` 为 `.env`
- 设置 `DATABASE_URL=sqlite:///./data/legal_demo.sqlite3`
- 生成 `SESSION_SECRET`
- 创建并启动 systemd 服务
- 执行 `/health` 健康检查

## 四、配置模型

编辑服务器上的环境变量：

```bash
sudo nano /opt/legal-demo/.env
```

只会调用 `LLM_PROVIDER` 指定的一个模型入口。

### 方案 A：星火

```dotenv
LLM_PROVIDER=spark
SPARK_API_KEY=你的 key
SPARK_API_SECRET=你的 secret
SPARK_APP_ID=你的 app id
SPARK_MODEL=Spark Ultra-32K
SPARK_DOMAIN=4.0Ultra
SPARK_BASE_URL=wss://spark-api.xf-yun.com/v4.0/chat
```

### 方案 B：OpenAI 或 OpenAI 兼容网关

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=你的 key
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
```

### 方案 C：本地模型

要求本地模型服务兼容 OpenAI `/v1/chat/completions`，例如 vLLM、Ollama OpenAI-compatible server、LM Studio、Xinference。

```dotenv
LLM_PROVIDER=custom
CUSTOM_API_KEY=local-key
CUSTOM_MODEL=你的模型名
CUSTOM_BASE_URL=http://127.0.0.1:8001/v1
CUSTOM_TEMPERATURE=0.1
CUSTOM_MAX_TOKENS=4096
CUSTOM_STRICT_JSON_MODE=true
CUSTOM_USE_LOCAL=false
```

注意：如果设置 `CUSTOM_USE_LOCAL=true`，代码会强制使用 `http://127.0.0.1:8000/v1`，会忽略 `CUSTOM_BASE_URL`。如果你的应用也跑在 8000 端口，不要开启这个选项。

修改 `.env` 后重启服务：

```bash
sudo systemctl restart legal-demo
```

测试模型配置：

```bash
cd /opt/legal-demo
sudo -u legal-demo ./venv/bin/python llm_healthcheck.py
```

## 五、常用运维命令

查看服务状态：

```bash
sudo systemctl status legal-demo --no-pager
```

查看实时日志：

```bash
sudo journalctl -u legal-demo -f
```

重启：

```bash
sudo systemctl restart legal-demo
```

停止：

```bash
sudo systemctl stop legal-demo
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

SQLite 数据库位置：

```text
/opt/legal-demo/data/legal_demo.sqlite3
```

## 六、Nginx 反向代理

如果通过域名访问，建议让 Uvicorn 只监听 `127.0.0.1:8000`，由 Nginx 对外提供 HTTP/HTTPS。

安装 Nginx：

```bash
sudo apt-get update
sudo apt-get install -y nginx
```

创建配置：

```bash
sudo tee /etc/nginx/sites-available/legal-demo >/dev/null <<'EOF'
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 20m;

    location /static/ {
        alias /opt/legal-demo/app/static/;
        expires 7d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
    }
}
EOF
```

启用：

```bash
sudo ln -sf /etc/nginx/sites-available/legal-demo /etc/nginx/sites-enabled/legal-demo
sudo nginx -t
sudo systemctl reload nginx
```

## 七、备份和恢复 SQLite

创建备份目录：

```bash
sudo mkdir -p /opt/legal-demo/backups
sudo chown legal-demo:legal-demo /opt/legal-demo/backups
```

在线备份：

```bash
sudo -u legal-demo sqlite3 /opt/legal-demo/data/legal_demo.sqlite3 \
  ".backup '/opt/legal-demo/backups/legal_demo_$(date +%F_%H%M%S).sqlite3'"
```

恢复备份：

```bash
sudo systemctl stop legal-demo
sudo cp /opt/legal-demo/backups/legal_demo_YYYY-MM-DD_HHMMSS.sqlite3 /opt/legal-demo/data/legal_demo.sqlite3
sudo chown legal-demo:legal-demo /opt/legal-demo/data/legal_demo.sqlite3
sudo systemctl start legal-demo
```

## 八、更新部署

在开发机重新打包并上传：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package_linux_sqlite.ps1
scp dist/legal-demo-sqlite-*.tar.gz user@your-server:/tmp/
```

在服务器更新：

```bash
sudo bash /opt/legal-demo/scripts/deploy_linux_sqlite.sh \
  --app-dir /opt/legal-demo \
  --archive /tmp/legal-demo-sqlite-*.tar.gz \
  --service legal-demo \
  --user legal-demo \
  --host 127.0.0.1 \
  --port 8000
```

更新脚本不会主动删除 `/opt/legal-demo/data/legal_demo.sqlite3`，也不会覆盖已有 `.env` 中除 `DATABASE_URL`、`APP_HOST`、`APP_PORT` 之外的模型配置。

## 九、直接开放端口的启动方式

如果暂时不使用 Nginx，并希望通过服务器 IP 访问，把部署参数改成：

```bash
sudo bash scripts/deploy_linux_sqlite.sh \
  --app-dir /opt/legal-demo \
  --service legal-demo \
  --user legal-demo \
  --host 0.0.0.0 \
  --port 8000
```

同时放行防火墙：

```bash
sudo ufw allow 8000/tcp
```
