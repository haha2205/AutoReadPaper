#!/usr/bin/env bash
# setup.sh — AutoReadPaper 初始化安装脚本
# 使用方法: chmod +x scripts/setup.sh && ./scripts/setup.sh

set -euo pipefail

# ── 颜色输出工具 ─────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

info "========================================"
info "  AutoReadPaper 初始化安装脚本"
info "========================================"

# ── 检查必要工具 ─────────────────────────────────────────────
info "检查依赖工具..."
for cmd in docker docker-compose curl; do
  command -v "$cmd" >/dev/null 2>&1 || error "缺少必要工具: $cmd，请先安装后重试。"
done
info "✅ 依赖工具检查通过"

# ── 检查 .env 文件 ────────────────────────────────────────────
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    warn "已创建 .env 文件（从 .env.example 复制）。"
    warn "请编辑 .env 文件并填写 DEEPSEEK_API_KEY 等必要配置后，重新运行本脚本。"
    exit 1
  else
    error ".env.example 文件不存在，请检查项目完整性。"
  fi
fi

# ── 验证关键环境变量 ──────────────────────────────────────────
source .env
if [ -z "${DEEPSEEK_API_KEY:-}" ] || [ "$DEEPSEEK_API_KEY" = "sk-xxxxxxxxxxxxxxxxxxxxxxxx" ]; then
  error "请在 .env 文件中设置有效的 DEEPSEEK_API_KEY。"
fi
if [ -z "${N8N_PASSWORD:-}" ] || [ "$N8N_PASSWORD" = "your_strong_password_here" ]; then
  error "请在 .env 文件中设置 N8N_PASSWORD（不能使用默认密码）。"
fi
info "✅ 环境变量检查通过"

# ── 创建必要目录 ──────────────────────────────────────────────
info "创建数据目录..."
mkdir -p ../data/n8n ../data/chromadb ../logs
info "✅ 目录创建完成"

# ── 构建 Docker 镜像 ──────────────────────────────────────────
info "构建 Paper API Docker 镜像（首次构建约需 5~10 分钟）..."
docker-compose build --no-cache paper-api
info "✅ Docker 镜像构建完成"

# ── 启动服务 ──────────────────────────────────────────────────
info "启动所有服务..."
docker-compose up -d

# ── 等待服务健康 ──────────────────────────────────────────────
info "等待服务启动（最多 60 秒）..."
timeout=60
elapsed=0
while ! curl -sf http://localhost:8080/health > /dev/null 2>&1; do
  sleep 3
  elapsed=$((elapsed + 3))
  if [ $elapsed -ge $timeout ]; then
    error "Paper API 服务启动超时，请运行 docker-compose logs paper-api 查看日志。"
  fi
done
info "✅ Paper API 服务已就绪 (http://localhost:8080)"

info "✅ n8n 服务已就绪 (http://localhost:5678)"
info ""
info "========================================"
info "🎉 安装完成！接下来的步骤："
info ""
info "1. 访问 http://localhost:5678 登录 n8n"
info "   用户名: ${N8N_USER:-admin}"
info "   密码:   （见 .env 中的 N8N_PASSWORD）"
info ""
info "2. 在 n8n 中导入工作流："
info "   点击左侧菜单 Workflows → Import from file"
info "   选择 workflows/auto_paper_workflow.json"
info ""
info "3. 激活工作流（点击右上角 Inactive 开关）"
info ""
info "4. 可手动触发测试: ./scripts/run.sh"
info "========================================"
