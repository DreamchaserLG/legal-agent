# Legal Demo MVP 部署指南

## 一、服务器环境准备

### 1. 安装 Docker 和 Docker Compose

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable docker
sudo systemctl start docker

# 验证安装
docker --version
docker compose version
```

### 2. 将项目上传到服务器

```bash
# 方法1: 使用 scp
scp legal-demo-backup.tar.gz user@server:/opt/

# 方法2: 使用 rsync
rsync -avz legal-demo/ user@server:/opt/legal-demo/

# 方法3: 使用 git
git clone <your-repo-url> /opt/legal-demo
```

## 二、配置项目

### 1. 解压项目

```bash
cd /opt
tar -xzf legal-demo-backup.tar.gz
cd legal-demo
```

### 2. 配置环境变量

```bash
# 复制生产环境配置
cp .env.production .env

# 修改数据库密码
sed -i 's/change_me_in_production/你的数据库密码/g' .env

# 修改 Session 密钥
sed -i 's/change_this_to_a_random_string/随机生成的密钥/g' .env

# 生成随机 Session 密钥
openssl rand -hex 32
```

## 三、启动服务

### 方法1: Docker Compose (推荐)

```bash
cd /opt/legal-demo

# 构建并启动
docker compose up -d --build

# 查看状态
docker compose ps

# 查看日志
docker compose logs -f app

# 停止服务
docker compose down
```

### 方法2: 仅启动应用 (连接外部 PostgreSQL)

```bash
cd /opt/legal-demo

# 修改 DATABASE_URL 指向外部数据库
# DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/legal_demo

docker build -t legal-demo .
docker run -d \
  --name legal-demo \
  -p 8000:8000 \
  --env-file .env \
  legal-demo
```

## 四、内网穿透配置

### 方案1: Nginx 反向代理 (推荐)

```bash
# 安装 Nginx
sudo apt install -y nginx

# 创建配置文件
sudo tee /etc/nginx/sites-available/legal-demo << 'EOF'
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    location /static/ {
        alias /opt/legal-demo/app/static/;
        expires 7d;
    }
}
EOF

# 启用配置
sudo ln -s /etc/nginx/sites-available/legal-demo /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

### 方案2: 内网穿透工具 (frp)

如果服务器没有公网IP，需要使用内网穿透工具：

#### 服务器端配置

```bash
# 在服务器上运行 frps
cat > /etc/frp/frps.toml << 'EOF'
bindPort = 7000
EOF

frps -c /etc/frp/frps.toml
```

#### 本地电脑配置

```bash
# 在本地电脑运行 frpc
cat > frpc.toml << 'EOF'
serverAddr = "服务器IP"
serverPort = 7000

[[proxies]]
name = "legal-demo"
type = "http"
localPort = 8000
customDomains = ["your-domain.com"]
EOF

frpc -c frpc.toml
```

### 方案3: 使用 ngrok

```bash
# 安装 ngrok
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok-v3-stable-linux-amd64.tgz | tar -xz
sudo mv ngrok /usr/local/bin/

# 启动隧道
ngrok http 8000
```

## 五、验证部署

### 1. 检查服务状态

```bash
# 查看容器状态
docker compose ps

# 测试健康检查
curl http://localhost:8000/health
```

### 2. 访问应用

- 本地访问: http://localhost:8000
- 内网访问: http://服务器内网IP:8000
- 公网访问: http://your-domain.com (需要配置域名解析)

### 3. 创建管理员账户

访问 http://your-domain.com/register 注册第一个账户，然后在数据库中设置为管理员：

```bash
docker compose exec postgres psql -U postgres -d legal_demo -c "
UPDATE users SET role = 'admin' WHERE username = '你的用户名';
"
```

## 六、常见问题

### Q1: 启动失败，提示数据库连接错误

检查 .env 中的 DATABASE_URL 配置，确保 PostgreSQL 容器已启动：

```bash
docker compose logs postgres
```

### Q2: 应用启动后提示 CanLII API 错误

这是正常的，RSS 数据爬取会自动进行，无需 API Key。

### Q3: 如何更新代码

```bash
cd /opt/legal-demo
git pull
docker compose up -d --build
```

### Q4: 如何备份数据

```bash
docker compose exec postgres pg_dump -U postgres legal_demo > backup.sql
```

### Q5: 如何查看日志

```bash
# 实时查看应用日志
docker compose logs -f app

# 查看 PostgreSQL 日志
docker compose logs -f postgres
```
