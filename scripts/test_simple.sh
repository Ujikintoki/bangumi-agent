#!/bin/bash
# =========================================
# Bangumi 看板娘 — 真实用户场景测试
#
# 模拟站内用户的真实对话，覆盖：
#   闲聊 / 快速查分 / 问口碑 / 推荐 / 深度分析
#   时效排期 / bare title 追问 / 多轮对话 / 争论
#
# 用法：先启动服务 `uvicorn main:app --reload --port 8000`，再跑此脚本
# =========================================
set -e

BASE="http://localhost:8000/chat"
CT="Content-Type: application/json"

# ── 辅助函数 ──────────────────────────────────────────────────

print_header() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  $1"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

print_reply() {
  # 输入: curl 的 JSON 响应
  python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    reply = d.get('reply', '')
    wc = len(reply)
    iters = d.get('iterations', '?')
    tools = d.get('tools_used', [])
    intent = d.get('query_intent', '?')
    depth = d.get('depth', '?')
    style = d.get('output_style', '?')
    telemetry = d.get('telemetry')
    print(f'[{style.upper()}] depth={depth} | intent={intent} | {iters} iters | tools={tools}')
    print(f'字数: {wc}')
    if telemetry:
        print(f'耗时: {telemetry.get(\"elapsed_ms\", \"?\")}ms')
        for c in telemetry.get('llm_calls', []):
            print(f'  LLM {c[\"label\"]}: {c[\"elapsed_ms\"]}ms | in={c[\"prompt_tokens\"]} out={c[\"completion_tokens\"]}')
        for t in telemetry.get('node_timings', []):
            print(f'  Node {t[\"node\"]}: {t[\"elapsed_ms\"]}ms')
    print('───')
    print(reply)
except Exception as e:
    print(f'ERROR: {e}')
    print(sys.stdin.read())
"
}

# ═══════════════════════════════════════════════════════════════
# 1. 闲聊 — "今天好累"
#    预期：不用工具，不过 render，直接共情回复
# ═══════════════════════════════════════════════════════════════
print_header "1. 闲聊：今天好累啊"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "今天...想看点比较火的新番", "depth": "quick", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 2. 快速查分 — "进击的巨人评分"
#    预期：1-2 轮工具调用，render 把数据转成吐槽
# ═══════════════════════════════════════════════════════════════
print_header "2. 快速查分：进击的巨人"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "进击的巨人风评怎么样", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 3. 问口碑 — "EVA 值得补吗"
#    预期：有评分数据 + 有主观判断，不能只报数字
# ═══════════════════════════════════════════════════════════════
print_header "3. 问口碑：EVA 值得补吗"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "EVA 的宗教隐喻和哲学思考是怎样的？", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply
