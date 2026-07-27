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
  -d '{"message": "今天好累啊……想看点轻松的", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 2. 快速查分 — "进击的巨人评分"
#    预期：1-2 轮工具调用，render 把数据转成吐槽
# ═══════════════════════════════════════════════════════════════
print_header "2. 快速查分：进击的巨人"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "进击的巨人评分怎么样", "depth": "quick", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 3. 问口碑 — "EVA 值得补吗"
#    预期：有评分数据 + 有主观判断，不能只报数字
# ═══════════════════════════════════════════════════════════════
print_header "3. 问口碑：EVA 值得补吗"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "EVA 值得补吗？好多人说看不懂", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 4. 推荐 — "有没有类似 Psycho-Pass 的"
#    预期：搜索 + 推荐几个，有理由，不是标签匹配列表
# ═══════════════════════════════════════════════════════════════
print_header "4. 推荐：类似 Psycho-Pass 的番"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有没有类似 Psycho-Pass 的番？喜欢那种反乌托邦+犯罪心理的感觉", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 5. 深度分析 — "深度分析攻壳机动队"
#    预期：12 轮上限，Critic + Render，回复有深度
# ═══════════════════════════════════════════════════════════════
print_header "5. 深度分析：攻壳机动队"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "深度分析一下攻壳机动队，它在科幻动画史里什么地位", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 6. 时效排期 — "今季有什么新番"
#    预期：并行调用日历/热门，不要逐个搜详情
# ═══════════════════════════════════════════════════════════════
print_header "6. 时效排期：今季新番"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "今季有什么好看的新番？帮我挑几部", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 7. Bare Title — 只给作品名，没说要查什么
#    预期：追问确认，不是直接 dump 数据
# ═══════════════════════════════════════════════════════════════
print_header "7. Bare Title：只说作品名"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "星际牛仔", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 8. 多轮对话 — 话题绑定
#    第一轮搜作品 → 第二轮追问（不提作品名）→ 检验 L1 记忆
# ═══════════════════════════════════════════════════════════════
SESSION="test-multi-$(date +%s)"
print_header "8. 多轮对话：第一轮 — 搜 Mononoke"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"帮我看看 Mononoke 这部\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION\"}" \
  | print_reply

echo ""
echo "  --- 第二轮：追问（不提作品名）---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"评分呢？\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION\"}" \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 9. 风格对比 — 同一个问题，Bangumi娘 vs Neutral
# ═══════════════════════════════════════════════════════════════
print_header "9. 风格对比：Bangumi娘 vs Neutral（同一问题）"
echo "--- Bangumi娘 ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "CLANNAD 好看吗", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

echo ""
echo "--- Neutral ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "CLANNAD 好看吗", "depth": "auto", "output_style": "neutral"}' \
  | print_reply

# ═══════════════════════════════════════════════════════════════
# 10. 争论 — "我觉得鬼灭过誉了"
#     预期：用数据支撑，但不列数据表，有自己的立场
# ═══════════════════════════════════════════════════════════════
print_header "10. 争论：我觉得鬼灭之刃过誉了"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "说实话我觉得鬼灭之刃被吹过头了，你说呢", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 全部 10 个场景测试完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
