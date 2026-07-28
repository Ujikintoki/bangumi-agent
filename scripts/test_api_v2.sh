#!/bin/bash
# =========================================
# Bangumi 看板娘 — 压力测试集 v2
#
# 设计原则：模拟站内真实用户，暴露能力边界，不呆在舒适区
#
# 测试维度：
#   情绪多变 / 精确查数据 / 有约束推荐 / 争论与立场
#   时效数据 / 多轮话题绑定 / 创作制作 / 边界压力
#   深度链式 / 诚实度 / 风格对比
#
# 用法：先启动服务 `uvicorn main:app --reload --port 8000`，再跑此脚本
# =========================================
set -e

BASE="http://localhost:8000/chat"
CT="Content-Type: application/json"

print_header() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  $1"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

print_reply() {
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
    print('───')
    print(reply)
except Exception as e:
    print(f'ERROR: {e}')
    print(sys.stdin.read())
"
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  A. 情绪/状态 — 测 intent 分类 + 共情 + 不滥用工具              ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "A1. 情绪+推荐混合：刚哭完"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "昨晚看完CLANNAD AS哭到现在。有没有什么能治愈的，不要催泪的了", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "A2. 状态表达（不是要推荐）：什么都不想看"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近什么都看不进去，打开第一集五分钟就想关。我是不是脱宅了", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  B. 精确查数据 — 测工具策略 + 数据消化 + 诚实                    ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "B1. Bare title + 歧义：只说作品名"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "Monster", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "B2. 复杂参数：特定季度+特定评分"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "2011年有哪些8.5分以上的动画", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "B3. 人物搜索：声优/导演"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "花泽香菜配过哪些主要角色？挑最重要的几个说", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "B4. 名称消歧：相似名字"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "86 和 不存在的战区 是一部动画吗？评分怎么样", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  C. 推荐/发现 — 测搜索策略 + 理由质量 + 约束感知                 ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "C1. 有约束推荐：数量限制"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有没有类似Psycho-Pass的？推3部就够了，说清楚为什么", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "C2. 氛围推荐：抽象描述"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐几部适合深夜一个人窝在沙发里看的动画，不要太吵的", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "C3. 创作者导向：跟着导演走"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "我最近发现只要是汤浅政明的我都喜欢。他还有什么我没看过的？或者类似风格的导演？", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

print_header "C4. 逆向推荐：你觉得被低估的"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有没有评分不高——7.5以下——但你个人觉得被严重低估的作品？推一部就行", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  D. 讨论/争论 — 测立场表达 + 数据支撑 + 不骑墙                    ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "D1. 挑战经典"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "说实话我觉得EVA被严重过誉了。不就是个青少年机甲片吗，为什么大家吹成哲学神作", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "D2. 创作方向批评"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "新海诚是不是越来越商业了？感觉从《你的名字》之后就一直在重复自己，《铃芽之旅》完全看不下去", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  E. 时效数据 — 测"只信工具不编造"                               ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "E1. 今季新番"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "这一季有什么值得追的？不用列太多，挑你觉得真的能打的", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "E2. 趋势变化"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近有什么新番评分在往下掉的？想避雷", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  F. 多轮对话 — 测 L1 话题绑定 + 去重 + 渐进追问                  ║
# ╚══════════════════════════════════════════════════════════════════╝

SESSION_F="test-multi-f-$(date +%s)"
print_header "F. 多轮：物语系列（3 轮渐进）"
echo "[R1] 观看顺序"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"物语系列的观看顺序到底是什么？好乱\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_F\"}" \
  | print_reply

echo ""
echo "  --- [R2] 追问评分（不提作品名）---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"那第一部评分怎么样\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_F\"}" \
  | print_reply

echo ""
echo "  --- [R3] 更隐式的追问 ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"和同时期同类型的比呢？\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_F\"}" \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  G. 创作/制作 — 测跨域知识 + 深度分析                            ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "G1. 配乐维度"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有哪些动画的配乐比剧情本身更值得聊？菅野洋子、梶浦由记那些", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "G2. 制作公司分析"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "扳机社（Trigger）的作品都有什么特点？和骨头社比呢", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  H. 边界/压力 — 测兜底 + 不崩                                    ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "H1. 极短输入：纯标点"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "……", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "H2. 空作品名"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "我今天看了《》", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "H3. Meta 问题：质疑身份"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "说真的，你和B站那个AI客服有什么区别？你不也是调API的吗", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "H4. 只推一部：逼出最强判断"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "只推一部。你觉得这辈子必须看的动画是什么？只能推一部，说清楚理由", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  I. 深度 — 测 depth=deep 链式调用                               ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "I1. Deep: 动画史演变"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "从EVA到你的名字——日本动画三十年里的'世界系'叙事是怎么演变的", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  J. 风格对比 — 同一个复杂问题                                    ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "J. 风格对比：Bangumi娘 vs Neutral（讨论创作）"
echo "--- Bangumi娘 ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "你觉得今敏的《红辣椒》和诺兰的《盗梦空间》到底是什么关系？是抄袭还是致敬", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

echo ""
echo "--- Neutral ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "你觉得今敏的《红辣椒》和诺兰的《盗梦空间》到底是什么关系？是抄袭还是致敬", "depth": "auto", "output_style": "neutral"}' \
  | print_reply

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 全部 25 个场景测试完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
