#!/bin/bash
# ============================================
# Legal Demo MVP 服务器部署脚本
# 服务器路径: /home/caojingyu/legal-demo
# ============================================

set -e

echo "=========================================="
echo "Legal Demo MVP 服务器部署脚本"
echo "=========================================="

# 1. 停止旧服务
echo ""
echo "步骤 1: 停止旧服务..."
cd /home/caojingyu/legal-demo 2>/dev/null || cd /home/caojingyu
docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true
pkill -f "uvicorn" 2>/dev/null || true
pkill -f "python.*main.py" 2>/dev/null || true
sleep 2
echo "旧服务已停止"

# 2. 删除旧文件
echo ""
echo "步骤 2: 删除旧文件..."
cd /home/caojingyu
rm -rf legal-demo
rm -f legal-demo-backup.tar.gz
echo "旧文件已删除"

# 3. 解压新文件
echo ""
echo "步骤 3: 解压新文件..."
if [ -f "legal-demo-backup.tar.gz" ]; then
    tar -xzf legal-demo-backup.tar.gz
    echo "新文件已解压"
else
    echo "错误: legal-demo-backup.tar.gz 不存在"
    echo "请先上传文件: scp legal-demo-backup.tar.gz user@server:/home/caojingyu/"
    exit 1
fi

# 4. 配置环境变量
echo ""
echo "步骤 4: 配置环境变量..."
cd /home/caojingyu/legal-demo
if [ ! -f ".env" ]; then
    cp .env.production .env
    echo "已复制 .env.production 到 .env"
else
    echo ".env 文件已存在"
fi

# 5. 启动新服务
echo ""
echo "步骤 5: 启动新服务..."
chmod +x deploy.sh
./deploy.sh

# 6. 验证服务
echo ""
echo "步骤 6: 验证服务..."
sleep 10
docker compose ps
curl -s http://localhost:8000/ | head -5

echo ""
echo "=========================================="
echo "部署完成！"
echo "=========================================="
echo ""
echo "访问地址: http://$(hostname -I | awk '{print $1}'):8000"
echo ""
