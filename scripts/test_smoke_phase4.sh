#!/bin/bash
# =========================================
# Bangumi Agent — Phase 4 冒烟测试集
#
# 覆盖 Phase 1-4 所有关键架构变更：
#   Phase 1-3: 隐式终止、Few-Shot、tool docstring
#   Phase 4:   异质拓扑（pipeline + ReAct）
#   Phase 4.1: 移除 submit_facts、统一渲染路径、Path B 降级、classifier 微调
#
# 用法：先启动服务 `uvicorn main:app --reload --port 8000`，再跑此脚本
#       bash scripts/test_smoke_phase4.sh
# =========================================
set -e

BASE="http://localhost:8000/chat"
CT="Content-Type: application/json"
PASS=0
FAIL=0

print_reply() {
  python3 -c "
import sys, json
d = json.load(sys.stdin)
reply = d.get('reply', '')
wc = len(reply)
iters = d.get('iterations', '?')
tools = d.get('tools_used', [])
intent = d.get('query_intent', '?')
depth = d.get('depth', '?')
style = d.get('output_style', '?')
telemetry = d.get('telemetry')
print(f'  [{style.upper()}] depth={depth} | intent={intent} | {iters} iters | tools={tools}')
if telemetry:
    print(f'  耗时: {telemetry.get(\"elapsed_ms\", \"?\")}ms')
    for c in telemetry.get('llm_calls', []):
        print(f'  LLM {c[\"label\"]}: {c[\"elapsed_ms\"]}ms | in={c[\"prompt_tokens\"]} out={c[\"completion_tokens\"]}')
    for t in telemetry.get('node_timings', []):
        print(f'  Node {t[\"node\"]}: {t[\"elapsed_ms\"]}ms')
print(f'  字数: {wc}')
print(f'  ---')
print(reply)
print()
"
}

check() {
  local label="$1" resp="$2"
  shift 2
  for check in "$@"; do
    if ! echo "$resp" | python3 -c "$check" 2>/dev/null; then
      FAIL=$((FAIL + 1))
      echo "  ❌ FAIL: $label — $check"
      return
    fi
  done
  PASS=$((PASS + 1))
  echo "  ✅ PASS"
}

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║       Phase 4 冒烟测试 — 异质拓扑 + 隐式终止                ║"
echo "╚══════════════════════════════════════════════════════════════╝"

# ═══════════════════════════════════════════════════════════════
# A. Pipeline 拓扑验证
# ═══════════════════════════════════════════════════════════════

echo ""
echo "━━━ A. Pipeline 拓扑 ━━━"

echo ""
echo "A1. fetch 3步 pipeline — '进击的巨人 评分多少'"
A1=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "进击的巨人 评分多少", "depth": "fast", "output_style": "neutral"}')
echo "$A1" | print_reply
check "A1" "$A1" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='fetch', f'intent={d[\"query_intent\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert d['iterations']==3, f'iters={d[\"iterations\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert 'search_bangumi_subject' in d['tools_used'], 'missing search'" \
  "import sys,json; d=json.load(sys.stdin); assert 'get_bangumi_subject_detail' in d['tools_used'], 'missing detail'" \
  "import sys,json; d=json.load(sys.stdin); assert 'submit_facts' not in str(d['tools_used']), 'submit_facts leaked'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>20, 'reply too short'" \
  "import sys,json; d=json.load(sys.stdin); assert '8.22' in d['reply'] or '8.2' in d['reply'], 'score missing'"

echo ""
echo "A2. realtime pipeline — '最近什么番比较火'"
A2=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "最近什么番比较火", "depth": "fast", "output_style": "neutral"}')
echo "$A2" | print_reply
check "A2" "$A2" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent'] in ('realtime','explore'), f'intent={d[\"query_intent\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert d['iterations']>=1, f'iters={d[\"iterations\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>20, 'reply too short'"

echo ""
echo "A3. 不存在作品诚实回复 — 'zzzznotexist2025 评分'"
A3=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "zzzznotexist2025 这部动画评分多少", "depth": "fast", "output_style": "neutral"}')
echo "$A3" | print_reply
check "A3" "$A3" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='fetch', f'intent={d[\"query_intent\"]}'" \
  "import sys,json; d=json.load(sys.stdin); r=d['reply']; assert '未找到' in r or '没有' in r or '不存在' in r or '找不到' in r, f'no honest reply: {r[:80]}'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>10, 'reply too short'"

# ═══════════════════════════════════════════════════════════════
# B. ReAct 隐式终止
# ═══════════════════════════════════════════════════════════════

echo ""
echo "━━━ B. ReAct 隐式终止 ━━━"

echo ""
echo "B1. explore ReAct 隐式终止 — '推荐几部好看的治愈番'"
B1=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐几部好看的治愈番", "depth": "fast", "output_style": "bangumi"}')
echo "$B1" | print_reply
check "B1" "$B1" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='explore', f'intent={d[\"query_intent\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert 'submit_facts' not in str(d['tools_used']), 'submit_facts found'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>50, f'reply too short: {len(d[\"reply\"])}'"

echo ""
echo "B2. discuss ReAct 多步 — 'EVA真的被过誉了吗'"
B2=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "EVA真的被过誉了吗？用数据说话", "depth": "fast", "output_style": "bangumi"}')
echo "$B2" | print_reply
check "B2" "$B2" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent'] in ('discuss','explore'), f'intent={d[\"query_intent\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert 'submit_facts' not in str(d['tools_used']), 'submit_facts found'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>30, f'reply too short: {len(d[\"reply\"])}'"

# ═══════════════════════════════════════════════════════════════
# C. Chat 直通
# ═══════════════════════════════════════════════════════════════

echo ""
echo "━━━ C. Chat 直通 ━━━"

echo ""
echo "C1. 闲聊 0 tools — '你好呀，今天心情不错'"
C1=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "你好呀，今天心情不错", "depth": "fast", "output_style": "bangumi"}')
echo "$C1" | print_reply
check "C1" "$C1" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='chat', f'intent={d[\"query_intent\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert d['iterations']==0, f'iters={d[\"iterations\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert d['tools_used']==[], f'tools={d[\"tools_used\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>10, 'reply too short'"

echo ""
echo "C2. 感叹不触发工具 — 'CLANNAD真的太好看了'"
C2=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "CLANNAD真的太好看了", "depth": "fast", "output_style": "bangumi"}')
echo "$C2" | print_reply
check "C2" "$C2" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='chat', f'intent={d[\"query_intent\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert d['iterations']==0, f'iters={d[\"iterations\"]}'"

# ═══════════════════════════════════════════════════════════════
# D. Classifier 新边界（Phase 4.1 微调）
# ═══════════════════════════════════════════════════════════════

echo ""
echo "━━━ D. Classifier 新边界 ━━━"

echo ""
echo "D1. 裸标题 → fetch — 'EVA'"
D1=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "EVA", "depth": "fast", "output_style": "neutral"}')
echo "$D1" | print_reply
check "D1" "$D1" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='fetch', f'intent={d[\"query_intent\"]}'"

echo ""
echo "D2. '讲什么' → explore (新!) — '进击的巨人讲什么'"
D2=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "进击的巨人讲什么", "depth": "fast", "output_style": "neutral"}')
echo "$D2" | print_reply
check "D2" "$D2" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='explore', f'intent={d[\"query_intent\"]}'"

echo ""
echo "D3. 多实体比较 → explore — '巨人鬼灭咒术哪个评分最高'"
D3=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "巨人、鬼灭、咒术哪个评分最高", "depth": "fast", "output_style": "neutral"}')
echo "$D3" | print_reply
check "D3" "$D3" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='explore', f'intent={d[\"query_intent\"]}'"

echo ""
echo "D4. '配过什么' → explore (新!) — '花泽香菜配过哪些角色'"
D4=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "花泽香菜配过哪些角色", "depth": "fast", "output_style": "neutral"}')
echo "$D4" | print_reply
check "D4" "$D4" \
  "import sys,json; d=json.load(sys.stdin); assert d['query_intent']=='explore', f'intent={d[\"query_intent\"]}'"

# ═══════════════════════════════════════════════════════════════
# E. 多轮 Session
# ═══════════════════════════════════════════════════════════════

echo ""
echo "━━━ E. 多轮 Session ━━━"

SESSION_E1="smoke-e1-$(date +%s)"

echo ""
echo "E1-R1. 查巨人评分 (session=$SESSION_E1)"
E1R1=$(curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"进击的巨人评分多少\", \"depth\": \"fast\", \"output_style\": \"neutral\", \"session_id\": \"$SESSION_E1\"}")
echo "$E1R1" | print_reply
check "E1-R1" "$E1R1" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>20, f'reply too short'" \
  "import sys,json; d=json.load(sys.stdin); assert '8.22' in d['reply'] or '8.2' in d['reply'], 'score missing'"

echo "E1-R2. 隐式指代追问 — '那它的排名呢'"
E1R2=$(curl -s -X POST "$BASE" -H "$CT" \
  -d "{\"message\": \"那它的排名呢\", \"depth\": \"fast\", \"output_style\": \"neutral\", \"session_id\": \"$SESSION_E1\"}")
echo "$E1R2" | print_reply
check "E1-R2" "$E1R2" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>15, f'reply too short'"

# ═══════════════════════════════════════════════════════════════
# F. Render 质量
# ═══════════════════════════════════════════════════════════════

echo ""
echo "━━━ F. Render 质量 ━━━"

echo ""
echo "F1. 无 emoji — 'CLANNAD AFTER STORY 评分'"
F1=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "CLANNAD AFTER STORY 评分", "depth": "fast", "output_style": "bangumi"}')
echo "$F1" | print_reply
check "F1" "$F1" \
  "import sys,json,re; d=json.load(sys.stdin); r=d['reply']; assert not re.search(r'[\U0001F300-\U0001F9FF]', r), f'emoji found'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>20, 'reply too short'"

echo ""
echo "F2. 无 markdown table — '比较巨人鬼灭咒术的评分'"
F2=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "比较巨人鬼灭咒术的评分", "depth": "fast", "output_style": "bangumi"}')
echo "$F2" | print_reply
check "F2" "$F2" \
  "import sys,json; d=json.load(sys.stdin); r=d['reply']; assert '|---' not in r, f'table found'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>30, 'reply too short'"

echo ""
echo "F3. 人格差异可辨 — bangumi vs neutral 同问题"
echo "  [bangumi]"
F3_B=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐一部动画", "depth": "fast", "output_style": "bangumi"}')
echo "$F3_B" | print_reply
echo "  [neutral]"
F3_N=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "推荐一部动画", "depth": "fast", "output_style": "neutral"}')
echo "$F3_N" | print_reply
R_B=$(echo "$F3_B" | python3 -c "import sys,json; print(json.load(sys.stdin)['reply'])" 2>/dev/null || echo "")
R_N=$(echo "$F3_N" | python3 -c "import sys,json; print(json.load(sys.stdin)['reply'])" 2>/dev/null || echo "")
if [ "$R_B" != "$R_N" ] && [ -n "$R_B" ] && [ -n "$R_N" ]; then
  echo "  ✅ PASS (bangumi≠neutral)"
  PASS=$((PASS + 1))
else
  echo "  ❌ FAIL: 人格差异不可辨"
  FAIL=$((FAIL + 1))
fi

# ═══════════════════════════════════════════════════════════════
# G. 降级与边界
# ═══════════════════════════════════════════════════════════════

echo ""
echo "━━━ G. 降级与边界 ━━━"

echo ""
echo "G1. 空搜索不循环 — 'xyzzy_not_real_2025 导演'"
G1=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "xyzzy_not_a_real_anime_2025 导演是谁", "depth": "fast", "output_style": "neutral"}')
echo "$G1" | print_reply
check "G1" "$G1" \
  "import sys,json; d=json.load(sys.stdin); assert d['iterations']<=3, f'iters={d[\"iterations\"]} too many'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>5, 'reply too short'"

echo ""
echo "G2. deep 模式无 submit_facts — '汤浅政明代表作'"
G2=$(curl -s -X POST "$BASE" -H "$CT" \
  -d '{"message": "汤浅政明有哪些代表作", "depth": "deep", "output_style": "bangumi"}')
echo "$G2" | print_reply
check "G2" "$G2" \
  "import sys,json; d=json.load(sys.stdin); assert d['depth']=='deep', f'depth={d[\"depth\"]}'" \
  "import sys,json; d=json.load(sys.stdin); assert len(d['reply'])>30, f'reply too short'" \
  "import sys,json; d=json.load(sys.stdin); assert 'submit_facts' not in str(d['tools_used']), 'submit_facts found in deep'"

# ═══════════════════════════════════════════════════════════════
# 结果
# ═══════════════════════════════════════════════════════════════

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  结果: $PASS/$((PASS + FAIL)) passed"
if [ $FAIL -gt 0 ]; then
  echo "  ❌ $FAIL failures — 检查上方输出"
  exit 1
else
  echo "  ✅ 全部通过"
fi
echo "══════════════════════════════════════════════════════════════"
