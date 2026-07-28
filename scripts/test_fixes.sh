#!/bin/bash
# =========================================
# 针对性修复验证 — 只测 test_output5 暴露的问题
#
# FIX-1: 多轮 L1 截断 — 长回复后是否丢失上下文
# FIX-2: Deep 跑偏 — 是否调了无关工具
# FIX-3: 好为人师 — "什么都看不进去"不应推荐
# FIX-4: 时效真实性 — 只信工具不编造
# FIX-5: Discovery 是否调工具
#
# 用法：uvicorn main:app --reload --port 8000 然后 bash scripts/test_fixes.sh
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
    if telemetry and False:
        for c in telemetry.get('llm_calls', []):
            print(f'  LLM {c[\"label\"]}: in={c[\"prompt_tokens\"]} out={c[\"completion_tokens\"]}')
    print('───')
    print(reply)
except Exception as e:
    print(f'ERROR: {e}')
    print(sys.stdin.read())
"
}

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FIX-1: 多轮 L1 截断                                            ║
# ║                                                                  ║
# ║  原问题: R1 回答311字 → R2 "第一部评分" → agent忘了是物语         ║
# ║  诊断:                                                           ║
# ║    F1a — 短R1（<100字），R2是否记住                              ║
# ║    F1b — 和原测试一样但用depth=quick（更少token可截断）           ║
# ║    F1c — 第二轮不省略，明确说"化物语" → 对比                      ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "FIX-1a: 短R1→R2（R1回复short，测试基本多轮）"
S1="fix1a-$(date +%s)"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"命运石之门好看吗\", \"depth\": \"quick\", \"output_style\": \"bangumi\", \"session_id\": \"$S1\"}" \
  | print_reply
echo "  --- [R2] ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"那它排多少名\", \"depth\": \"quick\", \"output_style\": \"bangumi\", \"session_id\": \"$S1\"}" \
  | print_reply

print_header "FIX-1b: 长R1→R2（复现原bug：R1长回复+追问不提名）"
S2="fix1b-$(date +%s)"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"帮我详细介绍一下物语系列，包括有哪些作品和观看顺序\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$S2\"}" \
  | print_reply
echo "  --- [R2] 追问（不提作品名）---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"第一部评分怎么样\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$S2\"}" \
  | print_reply

print_header "FIX-1c: 控制组——R2明确提名（应100%正确）"
S3="fix1c-$(date +%s)"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"物语系列有哪些\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$S3\"}" \
  | print_reply
echo "  --- [R2] 明确提化物语 ---"
curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"化物语评分怎么样\", \"depth\": \"auto\", \"output_style\": \"bangumi\", \"session_id\": \"$S3\"}" \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FIX-2: Deep 模式不跑偏                                          ║
# ║                                                                  ║
# ║  原问题: 用户问汤浅政明→agent调了calendar+trending                ║
# ║  诊断:                                                            ║
# ║    F2a — 聚焦问题（不含"推荐"，纯搜集信息）                       ║
# ║    F2b — 推荐导向（含"还有什么"）→原来的跑偏源                    ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "FIX-2a: Deep聚焦 — '列出汤浅政明监督的所有动画'"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "列出汤浅政明监督的所有动画，按年份排", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

print_header "FIX-2b: Deep推荐 — '类似汤浅政明的导演'（原跑偏场景）"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "我很喜欢汤浅政明，有没有类似风格的导演或者他还有什么我没看过的", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FIX-3: 好为人师 — "什么都不想看"不应推荐                        ║
# ║                                                                  ║
# ║  原问题: 用户说"什么都看不进去"→agent推了虫师+落语心中            ║
# ║  诊断:                                                            ║
# ║    F3a — 纯消极状态，不应有任何推荐                               ║
# ║    F3b — 消极+轻微求助信号 → 可以推荐但应该先共情                  ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "FIX-3a: 纯消极 — '什么都看不进去，不想看了'"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近完全不想看动画。打开就想关。可能我就是不喜欢了。", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "FIX-3b: 消极+轻微求助 — '看不进去，有什么办法吗'"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近什么都看不进去，怎么办？有没有什么能重新燃起热情的", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FIX-4: 时效真实性 — 只信工具不编造                               ║
# ║                                                                  ║
# ║  原问题: "今季新番"可能混入训练数据里的作品                       ║
# ║  诊断:                                                            ║
# ║    F4a — depth=quick 强制浅层（应该更依赖工具）                    ║
# ║    F4b — 明确问"今天放送" → 最高时效要求                          ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "FIX-4a: 今季新番 quick模式（强制浅层，工具优先）"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "这季有什么值得看的？挑2-3部就好", "depth": "quick", "output_style": "bangumi"}' \
  | print_reply

print_header "FIX-4b: 今天放送（最高时效要求）"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "今天有什么动画在播？帮我看看放送表", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FIX-5: Discovery 是否调工具                                      ║
# ║                                                                  ║
# ║  原问题: C2 "深夜窝沙发" discovery → 0 tools                     ║
# ║  诊断:                                                            ║
# ║    F5a — 氛围推荐（和C2类似）                                     ║
# ║    F5b — 同样是氛围推荐但depth=deep → 应该调工具                  ║
# ╚══════════════════════════════════════════════════════════════════╝

print_header "FIX-5a: 氛围推荐 auto — '深夜一个人看的'"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐几部适合下雨天一个人看的动画，不要太吵的，安静一点的", "depth": "auto", "output_style": "bangumi"}' \
  | print_reply

print_header "FIX-5b: 氛围推荐 deep — 同问题，深度模式"
curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐几部适合下雨天一个人看的动画，安静一点的，说清楚为什么适合下雨天", "depth": "deep", "output_style": "bangumi"}' \
  | print_reply

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 针对性验证完成（5 个修复方向 × 2-3 场景）"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
