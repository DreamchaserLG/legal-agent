#!/bin/bash
# Legal Demo MVP 一键部署脚本

set -e

echo "=========================================="
echo "Legal Demo MVP 一键部署脚本"
echo "=========================================="

# 检查是否在项目目录
if [ ! -f "app/main.py" ]; then
    echo "错误: 请在项目根目录运行此脚本"
    exit 1
fi

# 检查 Docker 是否安装
if ! command -v docker &> /dev/null; then
    echo "Docker 未安装，尝试安装..."
    sudo apt update
    sudo apt install -y docker.io docker-compose-plugin
    sudo systemctl enable docker
    sudo systemctl start docker
fi

# 检查 Docker Compose 是否安装
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "Docker Compose 未安装，尝试安装..."
    sudo apt install -y docker-compose-plugin
fi

echo "=========================================="
echo "步骤 1: 配置环境变量"
echo "=========================================="

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "已复制 .env.example 到 .env"
    echo "请编辑 .env 文件配置数据库和LLM"
else
    echo ".env 文件已存在"
fi

echo "=========================================="
echo "步骤 2: 构建并启动服务"
echo "=========================================="

# 尝试使用 docker compose
if docker compose version &> /dev/null; then
    echo "使用 docker compose 启动..."
    docker compose up -d --build
else
    echo "使用 docker-compose 启动..."
    docker-compose up -d --build
fi

echo "=========================================="
echo "步骤 3: 等待服务启动"
echo "=========================================="

sleep 10

echo "=========================================="
echo "步骤 4: 检查服务状态"
echo "=========================================="

if docker compose version &> /dev/null; then
    docker compose ps
else
    docker-compose ps
fi

echo "=========================================="
echo "部署完成！"
echo "=========================================="
echo ""
echo "访问地址: http://localhost:8000"
echo ""
echo "默认账户: admin / admin123"
echo ""
echo "查看日志: docker compose logs -f app"
echo "停止服务: docker compose down"
