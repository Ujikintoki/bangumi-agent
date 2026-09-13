"""层 B 判定器 —— 回归测试（离线、确定、不调 LLM、不碰数据库）。

**背景（2026-09-13，层 B 冒烟抓到）**：判定最贵的一环是"一批候选判不完"。
实测形态是推理模型陷入长思考，把 `max_tokens` 烧穿后 JSON mode 返回**空串**
（不是报错）——d428 拿 20 张卡思考 13281 字符仍无输出。当时的实现是"原样重试
缺的索引"，而原样重试只会原样失败：整批失败 = 20 个索引全缺 = 第二次还是 20 张。

修复后要锁住的契约：

1. **判不出来的批次二分再判**（20 → 10+10 → 5+5…），而不是原样重试；
   一路降到 `JUDGE_MIN_SPLIT` 还失败就认输 —— 认输要留空，不能编个判定充数；
2. **调用次数必须数得准**：报告要拿它外推全量成本，`call_llm` 内部重试会让
   一次调用实际打两次 API（所以判定路径传 `n_retry=1`）；
3. **缓存键的口径字段是冻死的**：`think=0` 现在是个字面量（思考已全项目强制关闭，
   2026-09-13 用户决策）。**别把它"清理"掉** —— schema 字符串一改 sha1 全变，
   现有 690+ 条判定全部失效重判，白花钱；
4. **卡片要给够证据**：person 卡片必须带「作品」列表 —— 作品式查询问的就是它，
   而公司简介（如 MADHOUSE 那段讲丸山正雄与名字由来的）根本不会提作品。
   实测缺了它，判官把该查询的【真身】判成 false。

全程离线：`call_llm` 被替换成假客户端，不联网、不花钱。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from eval import generate_rag_gt as G

# ── 1. 解析容错 ──


def test_parse_judgments_shapes():
    assert G._parse_judgments({"judgments": [{"i": 0, "v": "true"}]}, 1) == {0: "true"}
    # JSON true/false 当字符串读
    assert G._parse_judgments({"judgments": [{"i": 0, "v": True}]}, 1) == {0: "true"}
    # {"0": "true"} 这种字典也认
    assert G._parse_judgments({"judgments": {"0": "unknown"}}, 1) == {0: "unknown"}
    # 空返回 / 非 JSON 形状
    assert G._parse_judgments({}, 3) == {}
    assert G._parse_judgments({"judgments": "nope"}, 3) == {}


def test_parse_judgments_drops_bad_items():
    """越界/重复/非法值一律丢弃 —— 丢弃会进第二次补判，比强行当 false 诚实。"""
    assert G._parse_judgments({"judgments": [{"i": 9, "v": "true"}]}, 2) == {}
    assert G._parse_judgments({"judgments": [{"i": 0, "v": "maybe"}]}, 1) == {}
    assert G._parse_judgments(
        {"judgments": [{"i": 0, "v": "true"}, {"i": 0, "v": "false"}]}, 1) == {0: "true"}


# ── 2. 缓存键 ──


def test_cache_key_varies_with_every_input():
    base = G._judge_cache_key("查询A", "卡片X")
    assert base == G._judge_cache_key("查询A", "卡片X")  # 确定性
    assert base != G._judge_cache_key("查询B", "卡片X")
    assert base != G._judge_cache_key("查询A", "卡片Y")


def test_cache_key_schema_is_frozen():
    """`think=0` 是冻死的字面量 —— 改动它等于让全部已有判定失效重判。

    这里用「重新实现一遍 schema」的方式把它钉住：如果谁把 `think=0` 从 schema 里
    删了（或改成别的值），算出来的键就对不上，测试立刻红。这是**故意**的硬编码，
    不是重复实现 —— 它锁的正是一个不许再动的历史常量。
    """
    import hashlib as _hashlib

    schema = (f"{G.JUDGE_VERSION}|think=0"
              f"|{G.JUDGE_SUMMARY_CHARS}|{G.JUDGE_TAG_LIMIT}|{G.JUDGE_WORK_LIMIT}"
              f"|{_hashlib.sha1(G.JUDGE_RUBRIC.encode('utf-8')).hexdigest()[:8]}")
    expected = _hashlib.sha1(f"{schema}\n查询A\n卡片X".encode("utf-8")).hexdigest()
    assert G._judge_cache_key("查询A", "卡片X") == expected


def test_pack_is_adjustable_but_bounded():
    """批次常量只是一个起点；真正的机制是 _judge_members 的自适应降档。"""
    assert G.JUDGE_PACK >= 1
    assert G.JUDGE_MIN_SPLIT >= 1


# ── 3. 二分补判（假客户端）──


def _fake_llm(max_ok: int, sink: list | None = None):
    """模拟：一批超过 max_ok 张就"截断"（返回空 dict），否则全员判 true。

    sink 记的是每批的卡片数 —— 调用序列是二分契约的观测面。
    """

    def _fake(client, model, system, user, **kw):
        n = len(re.findall(r"^\[\d+\] ", user, re.M))
        if sink is not None:
            sink.append(n)
        if n > max_ok:
            return {}
        return {"judgments": [{"i": i, "v": "true"} for i in range(n)]}

    return _fake


Q_STUB = {"query": "测试查询", "entity_type": "subject"}
ITEMS_20 = [(i, f"卡片{i}") for i in range(20)]


def test_batch_splits_until_it_fits(monkeypatch):
    """整批失败 → 二分 → 20 条全部判出（深度优先：20,10,5,5,10,5,5）。"""
    sink: list = []
    monkeypatch.setattr(G, "call_llm", _fake_llm(5, sink))
    got, calls = G._judge_batch(None, "m", Q_STUB, ITEMS_20)
    assert len(got) == 20
    assert sink == [20, 10, 5, 5, 10, 5, 5]
    assert calls == len(sink), "调用次数要数得准，报告拿它外推成本"


def test_batch_single_call_when_it_fits(monkeypatch):
    monkeypatch.setattr(G, "call_llm", _fake_llm(20))
    got, calls = G._judge_batch(None, "m", Q_STUB, ITEMS_20)
    assert calls == 1
    assert len(got) == 20


def test_batch_gives_up_empty_not_fabricated(monkeypatch):
    """降到单张仍失败 → 认输，返回空（绝不编判定充数）。"""
    monkeypatch.setattr(G, "call_llm", _fake_llm(0))
    got, calls = G._judge_batch(None, "m", Q_STUB, ITEMS_20)
    assert got == {}
    assert calls > 0


def test_batch_skips_empty_input(monkeypatch):
    sink: list = []
    monkeypatch.setattr(G, "call_llm", _fake_llm(0, sink))
    got, calls = G._judge_batch(None, "m", Q_STUB, [])
    assert (got, calls, sink) == ({}, 0, [])


def test_call_llm_always_disables_thinking():
    """eval 走的是自己的裸 OpenAI client，够不到 agent.llm 工厂的默认值 ——
    思考必须在这里显式关掉。漏了它，判官就退回默认（=思考），批次 ≥10 会烧穿
    max_tokens 并静默返回空串。"""
    seen: dict = {}

    class _FakeCompletions:
        def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))],
            )

    class _FakeClient:
        chat = SimpleNamespace(completions=_FakeCompletions())

    assert G.call_llm(_FakeClient(), "m", "system", "user") == {"ok": 1}
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}


# ── 3b. 自适应降档（假 _judge_cards）──


def test_members_shrink_pack_after_a_hard_batch(monkeypatch):
    """难查询：第一批没能一次过之后，剩下的批次必须变小。

    假客户端在这里【故意不实现】真 `_judge_cards` 的二分契约（真的一直切到判完
    为止），只保留"这批过不了就返回空"这一条 —— 本测试锁的是 _judge_members 的
    降档决策，不是二分本身（二分由 test_batch_* 系列锁）。所以首批那 20 条留在
    未判定状态是预期的。
    """
    sizes: list = []

    def fake_cards(client, model, q, cards):
        sizes.append(len(cards))
        if len(cards) > 10:           # 模拟"这批太大，判不完"
            return {}, 1
        return {i: "true" for i in range(len(cards))}, 1

    members = [{"id": f"e{i}", "buckets": ["top20"]} for i in range(60)]
    recs = {m["id"]: {"entity_type": "subject", "subject_type": 2, "name": m["id"],
                      "name_cn": "", "popularity": 0, "tags": [], "works": [],
                      "summary": "", "role": "", "career": []} for m in members}
    monkeypatch.setattr(G, "_judge_cards", fake_cards)
    verdicts, calls, entries = G._judge_members(
        None, "m", {"id": "q1", "query": "查询", "entity_type": "subject"},
        members, {}, recs)

    assert sizes[0] == G.JUDGE_PACK, f"首批按常量走：{sizes}"
    assert sizes[1:] == [10, 10, 10, 10], f"降档后剩下的批次都是 10：{sizes}"
    assert verdicts[:20] == [None] * 20
    assert all(v == "true" for v in verdicts[20:])
    assert len(entries) == 40 and calls == len(sizes)


def test_members_keeps_pack_when_every_batch_succeeds(monkeypatch):
    """顺查询：不该降档 —— 降了就是白白多打几次 API。"""
    sizes: list = []

    def fake_cards(client, model, q, cards):
        sizes.append(len(cards))
        return {i: "false" for i in range(len(cards))}, 1

    members = [{"id": f"e{i}", "buckets": ["top20"]} for i in range(45)]
    recs = {m["id"]: {"entity_type": "subject", "subject_type": 2, "name": m["id"],
                      "name_cn": "", "popularity": 0, "tags": [], "works": [],
                      "summary": "", "role": "", "career": []} for m in members}
    monkeypatch.setattr(G, "_judge_cards", fake_cards)
    G._judge_members(None, "m", {"id": "q1", "query": "查询", "entity_type": "subject"},
                     members, {}, recs)
    assert sizes == [G.JUDGE_PACK, G.JUDGE_PACK, 45 - 2 * G.JUDGE_PACK], f"{sizes}"


def test_members_uses_cache_without_calling_llm(monkeypatch):
    """缓存命中就不该发请求 —— 修完短路重跑只该付新增候选的钱。"""
    def boom(*a, **kw):
        raise AssertionError("缓存命中却发了请求")

    members = [{"id": "e1", "buckets": ["top20"]}]
    recs = {"e1": {"entity_type": "subject", "subject_type": 2, "name": "甲",
                   "name_cn": "", "popularity": 0, "tags": [], "works": [],
                   "summary": "", "role": "", "career": []}}
    q = {"id": "q1", "query": "查询", "entity_type": "subject"}
    key = G._judge_cache_key(q["query"], G._candidate_card(recs["e1"]))
    monkeypatch.setattr(G, "_judge_cards", boom)
    verdicts, calls, entries = G._judge_members(None, "m", q, members, {key: "true"}, recs)
    assert verdicts == ["true"] and calls == 0 and entries == []


# ── 4. 卡片与标签 ──


def test_person_card_carries_works_not_popularity():
    """person 卡片必须带作品：作品式查询问的就是它，公司简介不会提。"""
    rec = {
        "id": "person_603", "entity_type": "person", "subject_type": None,
        "name": "MADHOUSE", "name_cn": "MADHOUSE", "popularity": 12345,
        "tags": [], "works": ["欺诈游戏", "钻石王牌 act2 第二季", "ghost"],
        "summary": "日本一家动画制作公司。", "role": "", "career": ["producer"],
    }
    card = G._candidate_card(rec)
    assert "作品：" in card
    assert "欺诈游戏" in card and "钻石王牌 act2 第二季" in card
    assert "职业：producer" in card
    # 热度会诱导"有名的就是相关的"，随机池平均更冷门 → 直接压基线、抬增益
    assert str(rec["popularity"]) not in card


def test_character_card_still_carries_works():
    rec = {
        "id": "character_1", "entity_type": "character", "subject_type": None,
        "name": "戦場ヶ原ひたぎ", "name_cn": "战场原黑仪", "popularity": 1,
        "tags": [], "works": ["化物语"], "summary": "「仅仅铜40克…」",
        "role": "主角", "career": [],
    }
    card = G._candidate_card(rec)
    assert "出自：化物语" in card
    assert "定位：主角" in card


def test_person_type_label_admits_companies():
    """person 桶里装着制作公司 —— 措辞不能把它排除掉，否则判官会卡在这个矛盾里。"""
    label = G._type_label({"entity_type": "person"})
    assert "公司" in label
    assert "以用户查询" in label  # 解释权交给查询原文
    assert "一部作品" in G._type_label({"entity_type": "subject", "subject_type": 2})
    assert "一个角色" in G._type_label({"entity_type": "character"})


# ── 5. 统计 ──


def test_stat_counts_failures_separately_from_unknown():
    members = [{"id": "a", "buckets": ["top20"]}, {"id": "b", "buckets": ["top20"]},
               {"id": "c", "buckets": ["random_full"]}, {"id": "d", "buckets": ["random_full"]}]
    verdicts = ["true", "unknown", "false", None]
    assert G._stat(members, verdicts, "top20") == {
        "n": 2, "judged": 1, "relevant": 1, "unknown": 1, "failed": 0}
    st = G._stat(members, verdicts, "random_full")
    assert st == {"n": 2, "judged": 1, "relevant": 0, "unknown": 0, "failed": 1}
    # 判定失败不能混进分母：它既不是相关也不是不相关
    assert G._rate(st) == 0.0


def test_rate_none_when_nothing_judged():
    assert G._rate({"judged": 0, "relevant": 0}) is None


def test_paired_bootstrap_degenerates_on_constant_diff():
    ci = G._paired_bootstrap_ci([(0.9, 0.1)] * 30)
    assert ci is not None and abs(ci[0] - 0.8) < 1e-9 and abs(ci[1] - 0.8) < 1e-9
    assert G._paired_bootstrap_ci([(0.5, 0.5)]) is None  # 样本 <2 没有区间可言


@pytest.mark.parametrize("bad", ["", "   "])
def test_parse_judgments_ignores_blank_values(bad):
    assert G._parse_judgments({"judgments": [{"i": 0, "v": bad}]}, 1) == {}
