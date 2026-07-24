#!/bin/bash
# API 手动测试脚本 — 覆盖各类工具和场景
# 使用：先在一个终端执行 `uvicorn main:app --reload --port 8000`，再在另一个终端跑此脚本
set -e

BASE="http://localhost:8000/chat"
CT="Content-Type: application/json"

echo "========================================="
echo "1. Dialogue — 搜索条目"
echo "========================================="
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "帮我搜一下尼古喵喵", "agent_type": "dialogue"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "2. Research — 京吹 评分 + 口碑分析"
echo "========================================="
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "京吹评分怎么样？口碑两极分化吗？", "agent_type": "research", "output_style": "bangumi"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "3. Research — 人物搜索"
echo "========================================="
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "花泽香菜配过哪些有名角色？", "agent_type": "research"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "4. Research — 放送排期"
echo "========================================="
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "今天有什么新番", "agent_type": "research", "output_style": "bangumi"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "5. Research — 条目角色 + 声优"
echo "========================================="
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "京吹有哪些主要角色？声优是谁？", "agent_type": "research"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "6. Research — 热门趋势（get_trending_subjects + get_hot_topics）"
echo "========================================="
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近什么动画最火？社区在热议什么？", "agent_type": "research", "output_style": "bangumi"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "7. Research — 推荐"
echo "========================================="
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "今天有点想冲，推荐几部里番吧", "agent_type": "research", "output_style": "bangumi"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "8. 风格对比：Dialogue vs Research (neutral)"
echo "========================================="
echo "--- Dialogue (Bangumi娘) ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "星际牛仔好看吗", "agent_type": "dialogue"}' | python3 -m json.tool --no-ensure-ascii
echo ""
echo "--- Research (neutral) ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "星际牛仔好看吗", "agent_type": "research", "output_style": "neutral"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "9. 多轮对话 — 话题绑定（L1 记忆）"
echo "========================================="
echo "--- 第一轮：搜作品 ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "帮我搜一下 Mononoke", "agent_type": "research", "session_id": "test-001", "output_style": "neutral"}' | python3 -m json.tool --no-ensure-ascii
echo ""
echo "--- 第二轮：追问评分（不提作品名） ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "评分呢？", "agent_type": "research", "session_id": "test-001", "output_style": "neutral"}' | python3 -m json.tool --no-ensure-ascii

echo ""
echo "========================================="
echo "✅ 全部测试完成"
echo "========================================="
