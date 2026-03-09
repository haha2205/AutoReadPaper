#!/usr/bin/env bash
# run.sh — 手动触发一次完整流水线（搜索→去重→AI总结→向量存储→推送）

set -euo pipefail

API_URL="${PAPER_API_URL:-http://localhost:8080}"

echo "🚀 手动触发 AutoReadPaper 流水线..."
echo "API 地址: $API_URL"
echo ""

response=$(curl -sf -X POST "$API_URL/api/pipeline/run" \
  -H "Content-Type: application/json" \
  --max-time 300 | python3 -m json.tool)

echo "$response"

new_count=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('new_papers_count', 0))")
echo ""
echo "✅ 流水线执行完成：发现 $new_count 篇新论文"
