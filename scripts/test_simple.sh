#!/bin/bash
# Smoke test: 验证重构后 agent 端到端可用
# 用法: 先启动 uvicorn，再运行此脚本
#   DEV_MODE=true uvicorn main:app --port 8000
#   bash scripts/test_simple.sh

set -e
BASE="http://localhost:8000/chat"

echo "=== 1. chat 意图（不调工具）==="
curl -s -X POST "$BASE" \
  -H "Content-Type: application/json" \
  -d '{"message": "你好呀", "depth": "fast"}'

echo ""
echo "=== 2. fetch 意图（调搜索+详情）==="
curl -s -X POST "$BASE" \
  -H "Content-Type: application/json" \
  -d '{"message": "EVA评分怎么样", "depth": "fast"}'

echo ""
echo "=== 3. explore 意图（深度探索）==="
curl -s -X POST "$BASE" \
  -H "Content-Type: application/json" \
  -d '{"message": "推荐一部治愈番", "depth": "deep"}'

echo ""
echo "=== 4. realtime 意图（时效数据）==="
curl -s -X POST "$BASE" \
  -H "Content-Type: application/json" \
  -d '{"message": "这周有什么新番", "depth": "fast"}'

echo ""
echo "=== Done ==="
