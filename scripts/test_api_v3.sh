#!/bin/bash
# =========================================
# Bangumi 看板娘 — 综合测试集 v3
#
# 覆盖 Phase 9 全部四种人格 + 三种深度 + 多轮 + 边界
#
# 测试维度：
#   A. 输出质量 —— 诚实、准确、格式合规、字数控制
#   B. 人格对比 —— 同一问题 × 4 人格，差异必须肉眼可辨
#   C. 工具策略 —— 够了就停、并行/串行、deep 链式
#   D. 真实场景 —— 多轮隐式指代、情绪支持、日常查询
#   E. 边界压力 —— 异常输入、长程多轮、deep 深度链
#
# 用法：先启动服务 `uvicorn main:app --reload --port 8000`，再跑此脚本
#       ./scripts/test_api_v3.sh > docs/test_output/v3_$(date +%Y%m%d).md
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

# ╔══════════════════════════════════════════════════════════════════╗
# ║  A. 输出质量 — 数据准确性、诚实性、格式合规、字数控制            ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "A1. 精确查评分 — 测 search + 数据引用不编造"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "进击的巨人 评分多少", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "A2. 精确查排名 — 测 search + 排名数字准确"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "Clannad 在 Bangumi 上排名多少", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "A3. 不存在的作品 — 测诚实：不能说'没评分'就编一个"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "zzzznotexistzzzz 这部动画Bangumi评分多少", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "A4. 常识不调工具 — 测'什么时候不调'"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "今天星期几", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "A5. 模糊描述搜作品 — 测 RAG search_local_bangumi"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有个动画讲的是男主每集都在不同世界线里死的，叫什么来着", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "A6. Quick 模式字数 — 必须≤120字"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "这季有什么好看的，简单说一下", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "A7. 格式检查：无emoji + 无表格 + 不泄漏工具调用"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "比较巨人、鬼灭、咒术的评分", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "A8. 时效数据诚实 — 只信工具不编造"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "2028年最值得期待的新番有哪些", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  B. 人格对比 — 4 个 probe × 4 人格                              ║
# ║  probe1: 推荐（暴露价值判断）                                     ║
# ║  probe2: 主流vs个人（暴露态度是附和/质疑/温和反对）               ║
# ║  probe3: 情绪（暴露共情风格）                                     ║
# ║  probe4: 争论（暴露冲突处理方式）                                 ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── Probe 1: 推荐 ──

print_header "B1-probe1: [bangumi损友] 推荐动画"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐几部好看的动画，说明为什么", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "B2-probe1: [bangumi_cold高冷] 推荐动画"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐几部好看的动画，说明为什么", "depth": "fast", "output_style": "bangumi_cold"}' \
  | print_reply

print_header "B3-probe1: [bangumi_cute可爱] 推荐动画"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐几部好看的动画，说明为什么", "depth": "fast", "output_style": "bangumi_cute"}' \
  | print_reply

print_header "B4-probe1: [neutral中性] 推荐动画"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐几部好看的动画，说明为什么", "depth": "fast", "output_style": "neutral"}' \
  | print_reply

# ── Probe 2: 主流 vs 个人判断 ──

print_header "B5-probe2: [bangumi损友] 巨人是不是神作"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有人说进击的巨人是神作，你同意吗", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "B6-probe2: [bangumi_cold高冷] 巨人是不是神作"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有人说进击的巨人是神作，你同意吗", "depth": "fast", "output_style": "bangumi_cold"}' \
  | print_reply

print_header "B7-probe2: [bangumi_cute可爱] 巨人是不是神作"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有人说进击的巨人是神作，你同意吗", "depth": "fast", "output_style": "bangumi_cute"}' \
  | print_reply

print_header "B8-probe2: [neutral中性] 巨人是不是神作"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有人说进击的巨人是神作，你同意吗", "depth": "fast", "output_style": "neutral"}' \
  | print_reply

# ── Probe 3: 情绪共鸣 ──

print_header "B9-probe3: [bangumi损友] 看不进去了"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近什么都看不进去，打开第一集五分钟就想关。是不是我老了", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "B10-probe3: [bangumi_cold高冷] 看不进去了"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近什么都看不进去，打开第一集五分钟就想关。是不是我老了", "depth": "fast", "output_style": "bangumi_cold"}' \
  | print_reply

print_header "B11-probe3: [bangumi_cute可爱] 看不进去了"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近什么都看不进去，打开第一集五分钟就想关。是不是我老了", "depth": "fast", "output_style": "bangumi_cute"}' \
  | print_reply

print_header "B12-probe3: [neutral中性] 看不进去了"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近什么都看不进去，打开第一集五分钟就想关。是不是我老了", "depth": "fast", "output_style": "neutral"}' \
  | print_reply

# ── Probe 4: 批评/争论 ──

print_header "B13-probe4: [bangumi损友] EVA过誉论"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "说实话我觉得EVA被严重过誉了。不就是个青少年机甲片吗，为什么大家吹成哲学神作", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "B14-probe4: [bangumi_cold高冷] EVA过誉论"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "说实话我觉得EVA被严重过誉了。不就是个青少年机甲片吗，为什么大家吹成哲学神作", "depth": "fast", "output_style": "bangumi_cold"}' \
  | print_reply

print_header "B15-probe4: [bangumi_cute可爱] EVA过誉论"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "说实话我觉得EVA被严重过誉了。不就是个青少年机甲片吗，为什么大家吹成哲学神作", "depth": "fast", "output_style": "bangumi_cute"}' \
  | print_reply

print_header "B16-probe4: [neutral中性] EVA过誉论"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "说实话我觉得EVA被严重过誉了。不就是个青少年机甲片吗，为什么大家吹成哲学神作", "depth": "fast", "output_style": "neutral"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  C. 工具调用策略 — 够了就停、并行、串行、深度链式                 ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "C1. 够了就停 — 简单评分查询 ≤3轮"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "巨人的评分", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "C2. 串行依赖 — search人物→get详情→按作品分析（deep模式）"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "汤浅政明导演的所有作品里，哪几部评分最高？为什么这风格这么特别", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

print_header "C3. 并行对比 — 多部评分同轮搜"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "巨人、鬼灭、咒术这三部，分别在Bangumi上评分和排名多少", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "C4. 时效工具 — get_calendar + get_trending，不能编造"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "今天放送的作品有哪些？最近什么在热榜上", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "C5. 复杂过滤 — 特定年份+评分区间"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "2011年有哪些评分在8分以上的TV动画？不用列太多，挑最好的几部", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "C6. Quick模式 — 强制 ≤3轮，够用就停"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "这季有什么好看的动画，简单说说", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "C7. 搜不到不循环 — 测重复调用检测 + 诚实放弃"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "帮我查一下'超级无敌不存在的动画2025'的导演是谁", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  D. 真实场景 — 多轮指代、情绪、日常、深度分析                     ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 多轮对话 1: 物语系列（3轮渐进指代） ──

SESSION_D1="test-d1-$(date +%s)"
print_header "D1. 多轮: 物语系列 — 3轮渐进指代"
echo "[R1] 观看顺序"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"物语系列的观看顺序到底是什么？太乱了\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_D1\"}" \
  | print_reply

echo ""
echo "  --- [R2] 追问第一部评分（不提作品名）---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"那第一部评分怎么样\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_D1\"}" \
  | print_reply

echo ""
echo "  --- [R3] 更隐式的追问 ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"和同时期同类型的比呢？\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_D1\"}" \
  | print_reply

# ── 多轮对话 2: 推荐 → 追问 → 筛选 ──

SESSION_D2="test-d2-$(date +%s)"
print_header "D2. 多轮: 推荐 → 追问更多 → 筛选"
echo "[R1]"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"推荐几部好看的动画\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_D2\"}" \
  | print_reply

echo ""
echo "  --- [R2] 追问 — 还有吗 ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"还有吗\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_D2\"}" \
  | print_reply

echo ""
echo "  --- [R3] 筛选 — 里面哪部最短 ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"你推荐的里面哪部最短？想先看短的\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_D2\"}" \
  | print_reply

# ── 多轮对话 3: 话题切换 ──

SESSION_D3="test-d3-$(date +%s)"
print_header "D3. 多轮: 话题切换 — 巨人 → 鬼灭"
echo "[R1]"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"进击的巨人最终季评分多少\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_D3\"}" \
  | print_reply

echo ""
echo "  --- [R2] 话题切换（不提名）---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"那鬼灭之刃呢\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_D3\"}" \
  | print_reply

# ── 日常查询 ──

print_header "D4. 声优查询"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "花泽香菜配过哪些主要角色？挑最重要的几个说", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "D5. 制作公司对比（deep）"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "京都动画和SHAFT的制作风格有什么本质区别？各举三部代表作", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

print_header "D6. 情绪支持 — 测共情 + 不滥用工具"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "看完CLANNAD AS哭了三天。有没有什么能治愈的，不要再催泪了", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "D7. 影评帮助"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "刚看完千年女优，想写个短评但不知道怎么下笔。帮我理一下思路", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "D8. Bare title — 只给作品名不说是要查什么"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "Monster", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  E. 边界与压力 — 异常输入、长程多轮、deep深度链、meta质疑        ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "E1. 极短输入"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "……", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "E2. 空白书名号"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "我看了《》觉得很好看", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "E3. Meta质疑 — 不崩人设"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "说真的，你和ChatGPT有什么区别？你不也是调API的吗，装什么看板娘", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

print_header "E4. 英文输入 — 至少不崩溃"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "recommend me some dark psychological anime like Monster", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

# ── Deep 模式压力测试 ──

print_header "E5. Deep压力 — 大量角色声优（测≤12轮+不炸）"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "列出进击的巨人里所有主要角色和对应声优", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

print_header "E6. Deep压力 — 跨作品分析"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "从EVA到你的名字——日本动画三十年里'世界系'叙事是怎么演变的？有什么关键作品", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

# ── 只推一部 — 逼出最强判断 ──

print_header "E7. 只推一部 — 逼出立场"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "只推一部。你觉得这辈子必须看的动画是什么？只能推一部，说清楚理由。不许骑墙", "depth": "fast", "output_style": "bangumi"}' \
  | print_reply

# ── 长程多轮 — 测 L1 内存压力 ──

SESSION_E8="test-e8-$(date +%s)"
print_header "E8. 长程多轮 — 8轮话题跳转后回溯第一轮"
echo "[R1] 巨人评分"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"进击的巨人评分\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_E8\"}" \
  | print_reply
echo ""
echo "[R2] 鬼灭呢"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"那鬼灭之刃呢\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_E8\"}" \
  | print_reply
echo ""
echo "[R3] 今敏作品"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"今敏有哪些作品\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_E8\"}" \
  | print_reply
echo ""
echo "[R4] 推荐几部"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"推荐几部类似今敏风格的\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_E8\"}" \
  | print_reply
echo ""
echo "[R5] EVA评价"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"EVA好看吗\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_E8\"}" \
  | print_reply
echo ""
echo "[R6] 热门"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"现在有什么热门的\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_E8\"}" \
  | print_reply
echo ""
echo "[R7] 作画崩坏是什么"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"什么是作画崩坏\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_E8\"}" \
  | print_reply
echo ""
echo "[R8] 回溯 — 刚才说的第一部的评分"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"最开始我问的那部动画，评分你还记得吗\", \"depth\": \"fast\", \"output_style\": \"bangumi\", \"session_id\": \"$SESSION_E8\"}" \
  | print_reply

# ── 高冷专属场景 — 测高冷人设不崩 ──

print_header "E9. 高冷极限: 问烂番 — 必须冷+短+有论据"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "《带着智能手机闯荡异世界》Bangumi上评分多少？值得看吗", "depth": "fast", "output_style": "bangumi_cold"}' \
  | print_reply

# ── 可爱专属场景 — 测可爱不崩 + 找优点 ──

print_header "E10. 可爱极限: 低分冷门 — 必须找到可安利的点"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "有没有评分不高但你个人超喜欢的作品？推一部就行，告诉我为什么喜欢", "depth": "fast", "output_style": "bangumi_cute"}' \
  | print_reply

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 全部 45 个场景测试完成"
echo ""
echo "  测试维度覆盖："
echo "    A. 输出质量: A1-A8       (8)"
echo "    B. 人格对比: B1-B16      (16 — 4 probes × 4 人格)"
echo "    C. 工具策略: C1-C7       (7)"
echo "    D. 真实场景: D1-D8       (8)"
echo "    E. 边界压力: E1-E10      (10)"
echo ""
echo "  检查要点（人工）："
echo "    • A1/A2 — 评分/排名数字是否准确（对照 Bangumi 站）"
echo "    • A3 — 是否诚实说'没找到'（不编造）"
echo "    • A6/A7 — 字数是否在限制内、有无 emoji/表格/工具泄漏"
echo "    • B1-B4 — 推荐风格明显不同 (损友有褒贬/高冷话少/可爱热情/中性客观)"
echo "    • B5-B8 — 对'神作'的态度明显不同 (损友可能部分认同/高冷不轻易同意/可爱可能赞同/中性只报数据)"
echo "    • B9-B12 — 对'看不进去了'的情绪回应明显不同"
echo "    • B13-B16 — 对'EVA过誉'的辩论风格明显不同"
echo "    • C1 — 迭代≤3轮，不无意义继续调工具"
echo "    • C6 — quick模式 ≤3轮，回复简洁"
echo "    • D1 — R2正确关联化物语，R3正确关联同时期"
echo "    • D3 — R2切到鬼灭，不谈巨人"
echo "    • E5 — deep ≤12轮，不炸"
echo "    • E8 — R8能回溯到R1的巨人"
echo "    • E9 — 高冷：话少、精准、冷、有论据"
echo "    • E10 — 可爱：真诚安利、找得到优点"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
