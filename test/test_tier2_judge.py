"""轴 3 Tier 2 判官 —— 回归测试（离线、确定、不调 LLM、不花钱）。

锁的是什么
    判官本体（``eval/reply_quality_judge.py``）出的数已经进台账：主指标
    ``unsupported 34.8% (48/138)``、κ ``0.889``（n=39）。这些数**只有在口径不变时**
    才有意义，所以这份文件锁的是「口径」而不是「数」：

1. **指纹分离**（§3.2a 事故的哨兵）。切句指纹只管切句 rubric、核对指纹只管核对
   rubric。2026-09-14 两段共用一个指纹 —— 改核对口径把 30 个场景的切句缓存一起
   作废、整批重切，而 LLM 切句不是逐字可复现的 ⇒ 人勾的 39 条答案里 19 条错位到
   别的断言上，κ 一度算出 0.748（**涨的是错位的功劳**）。这条红 = 事故复发的门开了。
2. **报不出出处不算 supported**（``supported`` + 空 cite → ``unknown`` 且单独计数）。
3. **分母规则**：``unknown`` 与零证据场景不进主指标；工具自述（规则 5）只在口径 B
   里同时摘出分子分母。
4. **标注/κ 工具**：答案行怎么读、读不出来的必须吵（静默丢答案会**非随机**地少算
   几条）、重判后只刷判官那一列、人工那几格一个字不动。

第 4 组尤其要紧：判官判定与人工判定一旦错位，κ 是个**看起来完全正常的错数** ——
比报错危险得多（D 组同款教训）。

全程离线：``call_llm`` 换成假判官，缓存 / 标注表 / 答案纸全部落在 ``tmp_path``。
"""

from __future__ import annotations

import json
import re

import pytest

from eval import reply_quality_judge as J

# ═══════════════════════════════════════════════════════════════════════
# A · 解析容错
# ═══════════════════════════════════════════════════════════════════════


def test_parse_assertions_shapes():
    """切句结果：字符串与 {text:...} 都认，空/异物丢弃，认不出来返回 None。"""
    assert J._parse_assertions(
        {"assertions": ["甲", "  乙  ", {"text": "丙"}]}) == ["甲", "乙", "丙"]
    # 空串、没有 text 的 dict、数字 —— 丢掉，不占位
    assert J._parse_assertions(
        {"assertions": ["甲", "", "   ", {"nope": 1}, 5, None]}) == ["甲"]
    # 认不出来返回 None（调用方据此二分/放弃），不是空列表 —— 两者语义不同
    assert J._parse_assertions({}) is None
    assert J._parse_assertions({"assertions": "nope"}) is None
    assert J._parse_assertions([]) is None


def test_parse_assertions_caps_at_max():
    """上限防模型抽风把一段修辞切成一堆碎片撑爆分母。

    ⚠ `MAX_ASSERTIONS` **不在缓存指纹里**：改它只对新切句生效，盘上已有切句按旧
    上限复用。要真正重切得换 `_SPLIT_RUBRIC`（那是进指纹的）。
    """
    out = J._parse_assertions({"assertions": [f"断言{i}" for i in range(50)]})
    assert len(out) == J.MAX_ASSERTIONS


def test_parse_checks_three_verdicts():
    """三值 + 出处 + tool_self；同一编号重复时报**第一条**。"""
    got, no_cite = J._parse_checks({"judgments": [
        {"i": 0, "v": "supported", "cite": "孤独摇滚", "why": "w"},
        {"i": 1, "v": " UNSUPPORTED ", "why": "w"},          # 大小写/空白容忍
        {"i": 2, "v": "unknown", "why": "w"},
        {"i": 3, "v": "unsupported", "why": "w", "tool_self": True},   # JSON 布尔
        {"i": 4, "v": "unsupported", "why": "w", "tool_self": "yes"},  # 真值字符串
    ]})
    assert [got[i]["v"] for i in range(5)] == [
        "supported", "unsupported", "unknown", "unsupported", "unsupported"]
    assert got[3]["tool_self"] is True and got[4]["tool_self"] is True
    assert got[0]["tool_self"] is False
    assert no_cite == 0
    # 同一编号重复：**后一条覆盖前一条**（`out[i] = ...` 是赋值，不是 setdefault）。
    # 这是既定行为、不是契约 —— 真出现重复说明模型在复读，两种取法都说得通。记在
    # 这里只为「读代码的人不用猜」。要改成留第一条属于改口径，得先拍板。
    dup, _ = J._parse_checks({"judgments": [
        {"i": 0, "v": "supported", "cite": "甲"}, {"i": 0, "v": "unsupported"}]})
    assert dup[0]["v"] == "unsupported"
    # 非法值 / 编号读不出 / 非 dict 条目 —— 一律丢弃（进二分补判，不硬塞一个值）
    drop, _ = J._parse_checks({"judgments": [
        {"i": 0, "v": "maybe"}, {"i": "x", "v": "supported"}, "nope", {"v": "supported"}]})
    assert drop == {}
    # 非 dict / 非 list
    assert J._parse_checks({}) == ({}, 0)
    assert J._parse_checks({"judgments": "nope"}) == ({}, 0)


def test_supported_without_cite_is_downgraded():
    """硬规矩：报 supported 却报不出出处 → 降级 unknown 并**单独计数**。

    主指标因此既不被灌水也不被稀释。正常 `no_cite` 恒为 0，不为 0 说明 rubric
    没被遵守，那一批数字要存疑。
    """
    got, no_cite = J._parse_checks({"judgments": [
        {"i": 0, "v": "supported", "cite": "", "why": "看着对"},
        {"i": 1, "v": "supported", "cite": "   ", "why": "w"},
        {"i": 2, "v": "unsupported", "why": "w"},      # 无出处不算错，unsupported 不降
    ]})
    assert got[0]["v"] == "unknown" and got[1]["v"] == "unknown"
    assert no_cite == 2
    assert got[0]["why"].startswith("[无出处→降级]"), "降级要留痕，否则事后查不出为什么少了个 supported"
    assert got[2]["v"] == "unsupported" and no_cite == 2, "只有 supported 受这条规矩管"


EVIDENCE_TEXT = '{"name_cn":"孤独摇滚！","eps":12,"rating_total":36196}'


def test_cite_rulers_pass_derived_and_catch_fabrication():
    """两把尺子：严格那把量「照抄原句」，宽松那把量「规则 1 放宽后的依据改写」。

    宽松尺子为什么必须有：规则 1 放宽后，「从字段值推出来 / 对数字做算术 / 标签同义
    改写」都算 supported，这三类的 cite 按定义就不是原句（实测 8 条核不到里 7 条是
    这种，**不是判官在编**，是尺子量错了）。伪造仍然拦得住 —— 编出来的数字拆开照样
    在证据里找不到。
    """
    ev = EVIDENCE_TEXT
    # 严格尺子：省略号拼两段（判官描述跨字段证据时的诚实写法），每段 ≥4 字都要落在证据里
    assert J._cite_ok('"name_cn": "孤独摇滚！" ... "eps": 12', ev)
    assert not J._cite_ok('"name_cn": "孤独摇滚！" ... "eps": 13', ev), "改一个数字就露馅"
    # 严格尺子量不到「依据改写」—— 那正是宽松尺子存在的理由
    derived = "rating_total: 36196"
    assert not J._cite_ok(derived, ev)
    assert J._cite_ok_derived(derived, ev)
    # 伪造：证据写 12 话，报 13 话 —— 两把尺子都拦得住
    for fab in ('"eps": 13', "13话", "集数 13"):
        assert not J._cite_ok(fab, ev), f"严格尺子漏了 {fab!r}"
        assert not J._cite_ok_derived(fab, ev), f"宽松尺子漏了 {fab!r}"
    # 单字符碎片对证据没有区分力，不算数（否则「7」这种能在任何证据里蒙到）
    assert not J._cite_ok_derived("7", ev)


# ═══════════════════════════════════════════════════════════════════════
# B · 缓存键与指纹分离（§3.2a 事故的哨兵）
# ═══════════════════════════════════════════════════════════════════════


def test_check_rubric_does_not_invalidate_split_cache(monkeypatch):
    """★ 事故哨兵：改核对 rubric **不许**动切句指纹。

    两段共用指纹时，改核对口径 → 切句缓存全废 → 30 个场景整批重切 → 切出来的第 k
    条断言不是原来那条 → 人按 (场景, 序号) 勾的答案悄悄对到别的断言上，κ 从 0.683
    涨到 0.748。涨的是错位的功劳。
    """
    split_before = J._split_fingerprint()
    check_before = J._rubric_fingerprint()
    monkeypatch.setattr(J, "_CHECK_RUBRIC", J._CHECK_RUBRIC + "\n（改一个字）")

    assert J._rubric_fingerprint() != check_before, "核对口径改了，核对指纹必须跟着变"
    assert J._split_fingerprint() == split_before, (
        "核对口径改一个字就把切句缓存带崩了 —— 断言身份会漂，人工标注全部错位")
    # 落到缓存键上：核对键变、切句键不变
    meta, payload = {"policy": "index-v1"}, "S1\n问题\n回复"
    assert J._cache_key("split", meta, payload) == _key_with(
        split_before, meta, "split", payload), "切句键必须还用旧指纹"
    assert J._cache_key("check", meta, payload) != _key_with(
        check_before, meta, "check", payload), "核对键必须跟着新指纹走"


def test_split_rubric_does_not_invalidate_check_cache(monkeypatch):
    """反向同样成立：改切句 rubric 只作废切句，核对指纹一个字不动。

    （真跑起来核对键仍会变 —— 断言文本变了、payload 就变了。但那是 payload 的功劳，
    不是把两段重新耦合起来。这里测的是键函数本身。）
    """
    split_before = J._split_fingerprint()
    check_before = J._rubric_fingerprint()
    monkeypatch.setattr(J, "_SPLIT_RUBRIC", J._SPLIT_RUBRIC + "\n（改一个字）")
    assert J._split_fingerprint() != split_before, "切句口径改了，切句指纹必须跟着变"
    assert J._rubric_fingerprint() == check_before, "反向耦合同样是事故"


def test_cache_key_varies_with_stage_and_policy():
    """键的四个输入（指纹 / stage / policy / payload）各自都要起作用。"""
    meta, payload = {"policy": "index-v1"}, "S1\n问题\n回复"
    k = J._cache_key("split", meta, payload)
    assert k == J._cache_key("split", meta, payload), "同一输入必须确定性"
    assert k != J._cache_key("check", meta, payload), "两段不能共键"
    assert k != J._cache_key("split", {"policy": "evidence-v2"}, payload), "换证据口径要作废"
    assert k != J._cache_key("split", {}, payload), "policy 缺失（'?'）不能撞上有值的"
    assert k != J._cache_key("split", meta, payload + "x"), "换样本/换回复要作废"


def _key_with(fp: str, meta: dict, stage: str, payload: str) -> str:
    """重实现一遍 `_cache_key` 的 schema —— 故意硬编码，锁的是不许再动的历史常量。"""
    import hashlib

    schema = fp + "|" + str((meta or {}).get("policy", "?"))
    return hashlib.sha1(f"{schema}\n{stage}\n{payload}".encode("utf-8")).hexdigest()


def test_fingerprint_schema_is_frozen():
    """指纹 schema 冻死 —— 改它 = 盘上全部判定缓存失效、整批重判（要花钱）。

    这里「重新实现一遍 schema」把它钉住：格式一改（丢 `cap=`、丢 `|split`、换
    sha1 长度），算出来的串就对不上，测试立刻红。**这是故意的硬编码**，不是重复实现。
    """
    import hashlib

    split_h = hashlib.sha1(J._SPLIT_RUBRIC.encode("utf-8")).hexdigest()[:8]
    check_h = hashlib.sha1(J._CHECK_RUBRIC.encode("utf-8")).hexdigest()[:8]
    cap = J._MAX_SINGLE_MESSAGE_TOKENS
    assert J._split_fingerprint() == f"{J.JUDGE_VERSION}|split|cap={cap}|{split_h}"
    assert J._rubric_fingerprint() == f"{J.JUDGE_VERSION}|cap={cap}|{check_h}"
    assert J._split_fingerprint() != J._rubric_fingerprint()


# ═══════════════════════════════════════════════════════════════════════
# C · 核对批次（判不完就二分）与分母规则
# ═══════════════════════════════════════════════════════════════════════


def _fake_batch(max_ok: int, sink: list | None = None):
    """假判官：一批超过 max_ok 条就返回空（模拟思考烧穿 max_tokens → JSON mode 空串）。"""
    def _fake(client, model, system, user, **kw):
        ids = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", user, re.M)]
        if sink is not None:
            sink.append(len(ids))
        if len(ids) > max_ok:
            return {}
        return {"judgments": [{"i": i, "v": "unsupported", "why": "w"} for i in ids]}
    return _fake


ITEMS_20 = [(i, f"断言{i}") for i in range(20)]


def test_check_batch_splits_until_it_fits(monkeypatch):
    """整批失败 → 二分 → 20 条全判出（深度优先：20,10,5,5,10,5,5）。

    原样重试只会原样失败 —— 实测失败形态是模型陷入长推理把 max_tokens 烧穿，唯一
    有效的降载手段是减少同屏断言数。
    """
    sink: list = []
    monkeypatch.setattr(J, "call_llm", _fake_batch(5, sink))
    got, calls, no_cite = J._check_batch(None, "m", EVIDENCE_TEXT, ITEMS_20)
    assert len(got) == 20
    assert sink == [20, 10, 5, 5, 10, 5, 5]
    assert calls == len(sink), "调用次数要数得准，报告拿它外推成本"
    assert no_cite == 0


def test_check_batch_gives_up_instead_of_fabricating(monkeypatch):
    """一路降到 `JUDGE_MIN_SPLIT` 还失败就**认输**：留空，不编判定充数。

    留空的断言在报告里记 unknown（「判官未给出判定」）—— 宁可少算依据，不少算分母。
    """
    sink: list = []
    monkeypatch.setattr(J, "call_llm", _fake_batch(0, sink))
    got, calls, _ = J._check_batch(None, "m", EVIDENCE_TEXT, ITEMS_20)
    assert got == {}, "判不出来就留空 —— 编一个判定充数比少算更糟"
    assert calls == len(sink) == 39, "20 条一路二分会终止，不会无限递归"
    assert min(sink) == 1 and sink.count(1) == len(ITEMS_20), (
        "递归底是单条批次，每条最多被单独问一次（20 条 ≈ 2n 次调用就是上限的样子）")


def test_check_batch_empty_and_min_split(monkeypatch):
    """空批次不调 API；单条批次失败**不再二分**（`JUDGE_MIN_SPLIT` 是下界）。"""
    sink: list = []
    monkeypatch.setattr(J, "call_llm", _fake_batch(0, sink))
    assert J._check_batch(None, "m", EVIDENCE_TEXT, []) == ({}, 0, 0)
    assert sink == []
    got, calls, _ = J._check_batch(None, "m", EVIDENCE_TEXT, [(0, "断言0")])
    assert got == {} and calls == 1 and sink == [1]
    assert J.JUDGE_MIN_SPLIT >= 2





def _a(i: int, v: str | None, **kw) -> dict:
    return dict({"i": i, "text": f"断言{i}", "v": v, "why": "", "cite": ""}, **kw)


def _sc(sid: str, assertions: list[dict], calls: int = 1, depth: str = "fast",
        **kw) -> dict:
    return dict({"scenario_id": sid, "depth": depth,
                 "evidence": {"calls": calls}, "assertions": assertions}, **kw)


def test_unknown_and_zero_evidence_stay_out_of_denominator():
    """分母 = supported + unsupported。unknown 不进，零证据场景整条不进。"""
    rep = [_sc("S1", [_a(0, "supported"), _a(1, "unsupported")]),
           _sc("S2", [_a(0, "unsupported"), _a(1, "unknown")], calls=1, depth="deep"),
           # 零证据：断言照切（好知道编了几条），但 v=None、不进任何分母
           _sc("S3", [_a(0, None), _a(1, None)], calls=0), ]
    agg = J._aggregate(rep)
    assert (agg["n_scenarios"], agg["n_judged"], agg["n_no_evidence"]) == (3, 2, 1)
    assert agg["denom"] == 3, "unknown 不许进分母"
    assert agg["unsupported"] == 2 and agg["supported"] == 1
    assert agg["unsupported_rate"] == pytest.approx(2 / 3)
    assert agg["unknown_rate"] == pytest.approx(1 / 4), "unknown 率的分母是**判过的**断言（4 条）"
    # 零证据场景单列，连断言文本一起留下（报告里要能读到它编了什么）
    assert [s["scenario_id"] for s in agg["no_evidence_scenarios"]] == ["S3"]
    assert agg["no_evidence_scenarios"][0]["n_assertions"] == 2
    # 零证据场景的断言 `v` 是 None —— 它们在三个计数里都不出现，只能靠上面那份单列看到
    assert agg["all_assertions_incl_no_evidence"] == {
        "supported": 1, "unsupported": 2, "unknown": 1}
    # 有 unsupported 的场景占比（次指标）只看判过的场景
    assert agg["scenarios_with_unsupported"] == 2
    assert agg["scenarios_with_unsupported_rate"] == pytest.approx(1.0)
    # 机械故障计数要透传
    assert agg["no_cite_downgrades"] == 0


def test_no_cite_downgrades_are_summed():
    agg = J._aggregate([_sc("S1", [_a(0, "unknown")], no_cite=2),
                        _sc("S2", [_a(0, "unknown")], no_cite=1)])
    assert agg["no_cite_downgrades"] == 3, "降级数要能在报告里读到，否则数字存疑时查不出原因"


def test_tool_self_comes_out_of_both_sides():
    """口径 B：工具自述（规则 5）从分子分母**同时**摘掉。

    这类断言讲的是「工具本身是什么」，模型是从工具定义知道的 —— 不是幻觉，是另一条
    信息通路。所以它既不该算幻觉，也不该留在分母里稀释。
    """
    rep = [_sc("S1", [_a(0, "unsupported", tool_self=True),
                      _a(1, "unsupported"),
                      _a(2, "supported")])]
    agg = J._aggregate(rep)
    assert agg["unsupported_rate"] == pytest.approx(2 / 3)
    assert (agg["tool_self_total"], agg["tool_self_unsupported"]) == (1, 1)
    assert agg["unsupported_rate_excl_tool_self"] == pytest.approx(1 / 2)
    assert agg["tool_self_scenarios"] == [
        {"scenario_id": "S1", "assertions": ["断言0"]}], "工具自述要单列，报告里看得到是哪几条"


def test_by_depth_layers_are_independent():
    rep = [_sc("S1", [_a(0, "unsupported"), _a(1, "supported")], depth="fast"),
           _sc("S2", [_a(0, "unsupported")], depth="deep"),
           _sc("S3", [_a(0, "supported")], depth="deep")]
    by = J._aggregate(rep)["by_depth"]
    assert by["fast"]["denom"] == 2 and by["fast"]["unsupported_rate"] == pytest.approx(0.5)
    assert by["deep"]["denom"] == 2 and by["deep"]["unsupported_rate"] == pytest.approx(0.5)
    assert by["deep"]["scenarios_with_unsupported"] == 1


# ═══════════════════════════════════════════════════════════════════════
# D · 端到端（假判官 + 缓存命中）
# ═══════════════════════════════════════════════════════════════════════

_FAKE_EVIDENCE = [{"tool": "search_bangumi_subject",
                   "result": {"name_cn": "孤独摇滚！", "eps": 12, "rating_total": 36196}}]


def _frozen(policy: str = "index-v1", samples: list[dict] | None = None) -> dict:
    return {"meta": {"git_hash": "deadbeef", "recorded_at": "2026-09-14T00:00:00",
                     "evidence_capture": {"policy": policy}},
            "samples": samples if samples is not None else [
                {"scenario_id": "S1", "depth": "fast", "message": "孤独摇滚几话",
                 "reply": "《孤独摇滚！》有12话。", "evidence": _FAKE_EVIDENCE},
                {"scenario_id": "S2", "depth": "deep", "message": "随便聊聊",
                 "reply": "这部挺好的。", "evidence": []},
            ]}


def _fake_llm(assertions: list[str], verdicts: dict[int, str], calls: list | None = None):
    """假判官：切句返回固定断言，核对按编号查表。call 序列记进 calls。"""
    def _fake(client, model, system, user, **kw):
        if calls is not None:
            calls.append("split" if system == J._SPLIT_RUBRIC else "check")
        if system == J._SPLIT_RUBRIC:
            return {"assertions": list(assertions)}
        ids = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", user, re.M)]
        return {"judgments": [{"i": i, "v": verdicts.get(i, "unknown"),
                               "cite": "孤独摇滚" if verdicts.get(i) == "supported" else "",
                               "why": "w"} for i in ids]}
    return _fake


def _offline(monkeypatch, tmp_path, calls=None, verdicts=None, assertions=None):
    monkeypatch.setattr(J, "CACHE_FILE", tmp_path / "cache.jsonl")
    monkeypatch.setattr(J, "_client_and_model", lambda: (None, "fake-model"))
    monkeypatch.setattr(J, "call_llm", _fake_llm(
        assertions or ["断言甲", "断言乙"],
        verdicts if verdicts is not None else {0: "supported", 1: "unsupported"},
        calls))


def test_judge_tier2_end_to_end_and_cache_hit(monkeypatch, tmp_path):
    """跑一遍出数、再跑一遍全走缓存 —— 缓存命中**不许**再打 API。"""
    calls: list = []
    _offline(monkeypatch, tmp_path, calls)
    frozen = _frozen()

    rep1 = J.judge_tier2(frozen, verbose=False)
    assert calls == ["split", "check", "split"], "S2 零证据：切句照做，核对不做"
    o1 = rep1["overall"]
    assert (o1["n_judged"], o1["n_no_evidence"]) == (1, 1)
    assert o1["denom"] == 2 and o1["unsupported_rate"] == pytest.approx(0.5)
    assert o1["judge_calls"] == 3 and o1["cache_hits"] == 0
    assert rep1["meta"]["evidence_policy"] == "index-v1"
    assert rep1["meta"]["rubric_fingerprint"] == J._rubric_fingerprint()

    calls.clear()
    rep2 = J.judge_tier2(frozen, verbose=False)
    assert calls == [], "缓存命中还打 API = 白花钱，而且两次数可能不一样"
    o2 = rep2["overall"]
    assert o2["judge_calls"] == 0 and o2["cache_hits"] == 3
    assert (o2["denom"], o2["unsupported"], o2["supported"]) == (
        o1["denom"], o1["unsupported"], o1["supported"]), "缓存复用必须给出同一个数"

    # 缓存是 append-only 的行式记录，键不重复
    lines = [json.loads(s) for s in (tmp_path / "cache.jsonl").read_text(
        encoding="utf-8").splitlines() if s.strip()]
    assert len(lines) == 3 and len({ln["k"] for ln in lines}) == 3
    assert {ln["kind"] for ln in lines} == {"split", "check"}


def test_policy_change_also_invalidates_split_residual_hazard(monkeypatch, tmp_path):
    """⚠ 残留风险（已知、未修）：换证据口径会把**切句**也一起重跑。

    `_cache_key` 把 `policy` 拼进了**两段**的键，而切句只取决于 (message, reply) ——
    policy 对它是无关输入。于是「同一样本换 policy 重跑」会重切一遍，而 LLM 切句不是
    逐字可复现的：断言身份漂了，人按 (场景, 序号) 勾的答案就可能对到别的断言上
    （§3.2a 同款）。rubric 那一路已经用指纹分离堵死，policy 这一路没有。

    为什么现在不动它：真实流程里换 policy 都伴随**重录**（新样本 → 必然重标），
    危险的是「不重录、只改 policy 重跑」这条窄路；而把 policy 从切句键里摘掉会让盘上
    已有的切句缓存**全部作废、整批重切**—— 正是这条要防的事，且要花钱。留给拍板。

    这个测试锁的是**现状**：真去摘 policy 的话它会红，提醒你先把上面这段读完。
    """
    calls: list = []
    _offline(monkeypatch, tmp_path, calls)
    J.judge_tier2(_frozen("index-v1"), verbose=False)
    calls.clear()
    J.judge_tier2(_frozen("evidence-v2"), verbose=False)
    assert calls == ["split", "check", "split"], (
        "S1 切句 + S1 核对 + S2 切句 —— 切句被 policy 带崩了（见 docstring）")


# ═══════════════════════════════════════════════════════════════════════
# E · 标注 / κ 工具
# ═══════════════════════════════════════════════════════════════════════


def test_read_verdict_word_boundaries_and_annotations():
    """答案行容忍人话：前后补理由都认，但 `unsupported` 不许被读成 `supported`。"""
    assert J._read_verdict("supported") == "supported"
    assert J._read_verdict("  Unsupported（证据里写的是 12 话）") == "unsupported"
    assert J._read_verdict("我判 supported，因为…") == "supported"
    assert J._read_verdict("大概是 unsupported 吧") == "unsupported"
    assert J._read_verdict("") is None
    assert J._read_verdict("没想好") is None
    # 词表本身：三值，且没有互为前缀的词（有的话长度倒序扫是唯一保险）
    assert set(J.TIER2_VERDICTS) == {"supported", "unsupported", "unknown"}


def test_count_answered_sees_lines_past_the_first():
    """★ 防呆哨兵：`_ANSWER_RE` 少了 `re.M` 就恒返回 0。

    恒 0 的后果不是「少算几条」—— 是**重出标注表时不报警、静默抹掉一小时的活**
    （D 组踩过同一个坑）。所以这里断言的是「表头的后面那些答案行也算数」。
    """
    text = ("# 表头\n\n说明若干。\n\n"
            "**#1 断言**：甲\n\n**你的判定**：supported\n\n"
            "**#2 断言**：乙\n\n**你的判定**：\n\n"
            "**#3 断言**：丙\n\n**你的判定**：unsupported\n")
    assert J._ANSWER_RE.search(text).group(1) == "supported"
    hits = sum(1 for m in J._ANSWER_RE.finditer(text) if m.group(1))
    assert hits == 2, "只填了 2 条（第 2 条留空）—— re.M 丢了这里会变成 0"


def test_parse_annotation_answers_and_unreadable(monkeypatch, tmp_path):
    """读答案：整行找独立判词；答案写上一行也认；读不出来必须**报出来**。"""
    sheet = "\n".join([
        "# 表头（答案行不在串首 —— 锁 re.M）", "",
        "**#1 断言**：甲", "", "**你的判定**：supported", "",
        "**#2 断言**：乙", "", "**你的判定**：unsupported（证据写的是 12 话）", "",
        "**#3 断言**：丙", "", "unsupported", "", "**你的判定**：", "",
        "**#4 断言**：丁", "", "**你的判定**：我判 unknown", "",
        "**#5 断言**：戊", "", "**你的判定**：大概是 supported 吧", "",
        "**#6 断言**：己", "", "**你的判定**：没想好", "",
    ])
    monkeypatch.setattr(J, "ANNOTATION_SHEET", tmp_path / "sheet.md")
    (tmp_path / "sheet.md").write_text(sheet, encoding="utf-8")

    assert J._parse_annotation_answers() == {
        1: "supported", 2: "unsupported", 3: "unsupported",
        4: "unknown", 5: "supported"}, "第 6 条读不出来，第 3 条从上一行回读"
    assert J._unreadable_answers() == [6], (
        "读不出的答案要吵 —— 静默丢会让 κ 非随机地少算几条（写得啰嗦的那些）")


def test_parse_annotation_answers_missing_sheet(monkeypatch, tmp_path):
    monkeypatch.setattr(J, "ANNOTATION_SHEET", tmp_path / "nope.md")
    assert J._parse_annotation_answers() == {}
    assert J._unreadable_answers() == []
    assert J._count_answered() == 0


def _key(items: list[dict], fp: str | None = None) -> dict:
    return {
        "version": "tier2-anno-1", "created": "2026-09-14 00:00",
        "source_report": "旧报告.md",
        "rubric_fingerprint": fp if fp is not None else J._rubric_fingerprint(),
        "model": "m",
        "sample": {"n": len(items), "seed": 1, "per_scenario": 2,
                   "strata": {"supported": 1, "unsupported": 1},
                   "population": {"n": 4, "supported": 2, "unsupported": 2},
                   "warning": "分层样本，κ 要连着这句一起报"},
        "items": items,
    }


def _item(i: int, judge: str, scenario: str = "S1", depth: str = "fast",
          idx: int = 0) -> dict:
    return {"i": i, "scenario": scenario, "depth": depth, "idx": idx,
            "text": f"断言{i}", "judge": judge, "cite_verified": None}


def test_refresh_annotation_key_touches_only_the_judge_column(monkeypatch, tmp_path):
    """改 rubric 重判后：只刷判官那一列。人工判定在表里，**一个字都不动**。"""
    monkeypatch.setattr(J, "ANNOTATION_KEY", tmp_path / "key.json")
    monkeypatch.setattr(J, "ANNOTATION_SHEET", tmp_path / "sheet.md")
    key_path, sheet_path = tmp_path / "key.json", tmp_path / "sheet.md"
    key_path.write_text(json.dumps(_key([
        _item(1, "supported", scenario="S1", idx=0),      # 改判
        _item(2, "unsupported", scenario="S1", idx=1),    # 没变
        _item(3, "supported", scenario="已消失", idx=0),   # 报告里没有 → 不参与
    ], fp="旧指纹"), ensure_ascii=False), encoding="utf-8")
    sheet_path.write_text("**#1 断言**：甲\n\n**你的判定**：unsupported\n",
                          encoding="utf-8")
    before = sheet_path.read_bytes()

    rep = {"meta": {"rubric_fingerprint": J._rubric_fingerprint()},
           "scenarios": [{"scenario_id": "S1", "assertions": [
               {"i": 0, "text": "断言1", "v": "unsupported", "tool_self": True,
                "cite_verified": False},
               {"i": 1, "text": "断言2", "v": "unsupported"}]}]}
    changed, matched, total = J.refresh_annotation_key(rep, "新报告.md")
    assert (changed, matched, total) == (1, 2, 3)
    assert sheet_path.read_bytes() == before, "人工判定一个字都不许动"

    out = json.loads(key_path.read_text(encoding="utf-8"))
    it = {x["i"]: x for x in out["items"]}
    assert it[1]["judge"] == "unsupported" and it[1]["judge_prev"] == "supported"
    assert "judge_prev" not in it[2], "没改判的条目不许写 judge_prev"
    assert it[3] == _item(3, "supported", scenario="已消失", idx=0), "对不上的条目原样留着"
    assert it[1]["tool_self"] is True and it[1]["cite_verified"] is False
    assert out["rubric_fingerprint"] == J._rubric_fingerprint()
    assert out["source_report"] == "新报告.md" and out["refreshed_at"]


def test_refresh_annotation_key_without_key(monkeypatch, tmp_path):
    monkeypatch.setattr(J, "ANNOTATION_KEY", tmp_path / "nope.json")
    assert J.refresh_annotation_key({"meta": {}}, "r.md") == (0, 0, 0)


def _kappa_fixture(monkeypatch, tmp_path, judges: list[str], humans: list[str],
                   depths: list[str] | None = None) -> dict | None:
    depths = depths or ["fast"] * len(judges)
    items = [_item(i, v, scenario="S1", idx=i, depth=depths[i - 1])
             for i, v in enumerate(judges, 1)]
    (tmp_path / "key.json").write_text(
        json.dumps(_key(items), ensure_ascii=False), encoding="utf-8")
    (tmp_path / "sheet.md").write_text("\n".join(
        f"**#{i} 断言**：断言{i}\n\n**你的判定**：{v}\n"
        for i, v in enumerate(humans, 1)), encoding="utf-8")
    monkeypatch.setattr(J, "ANNOTATION_KEY", tmp_path / "key.json")
    monkeypatch.setattr(J, "ANNOTATION_SHEET", tmp_path / "sheet.md")
    return J.judge_human_kappa()


def test_judge_human_kappa_reads_sheet_and_key(monkeypatch, tmp_path):
    """κ 是纯函数：key 里是判官判定，表里是人工判定，对齐即可。"""
    k = _kappa_fixture(monkeypatch, tmp_path,
                       judges=["supported", "supported", "unsupported", "unsupported"],
                       humans=["supported", "unsupported", "unsupported", "unsupported"])
    # po = 3/4；pe = (2/4)(1/4) + (2/4)(3/4) = 0.5 ⇒ κ = (0.75−0.5)/0.5 = 0.5
    assert k["n"] == 4 and k["po"] == pytest.approx(0.75) and k["pe"] == pytest.approx(0.5)
    assert k["kappa"] == pytest.approx(0.5)
    assert k["matrix"]["unsupported"]["unsupported"] == 2
    # 按判官判定分组的一致率 —— 主指标那一边的可读形式
    assert k["by_judge_verdict"]["supported"]["agree"] == 1
    assert k["by_judge_verdict"]["unsupported"]["agree"] == 2
    assert k["by_judge_verdict"]["unsupported"]["agree_rate"] == pytest.approx(1.0)
    a = k["annotation"]
    assert (a["n_items"], a["n_answered"], a["n_scored"]) == (4, 4, 4)
    assert a["n_unreadable"] == []
    assert a["matches_current_rubric"] is True
    assert a["strata"] == {"supported": 1, "unsupported": 1}
    assert a["warning"], "分层警告必须随 κ 一起报出来"


def test_judge_human_kappa_flags_stale_rubric(monkeypatch, tmp_path):
    """校准的是**上一版**判官时要看得出来（`matches_current_rubric`）。"""
    k = _kappa_fixture(monkeypatch, tmp_path,
                       judges=["supported", "unsupported"], humans=["supported", "unsupported"])
    assert k["annotation"]["matches_current_rubric"] is True
    key = json.loads((tmp_path / "key.json").read_text(encoding="utf-8"))
    key["rubric_fingerprint"] = "faithfulness-v1|cap=2000|旧指纹"
    (tmp_path / "key.json").write_text(json.dumps(key, ensure_ascii=False), encoding="utf-8")
    k2 = J.judge_human_kappa()
    assert k2["annotation"]["matches_current_rubric"] is False
    assert k2["annotation"]["rubric_fingerprint_current"] == J._rubric_fingerprint()


def test_judge_human_kappa_undefined_and_missing(monkeypatch, tmp_path):
    """两边都只用一个标签 ⇒ κ 无定义（不是 0 也不是 1）；文件不在 ⇒ None，不炸。"""
    k = _kappa_fixture(monkeypatch, tmp_path,
                       judges=["supported"] * 3, humans=["supported"] * 3)
    assert k["kappa"] is None and k["band"] == "undefined"
    assert k["by_judge_verdict"]["supported"]["agree_rate"] == pytest.approx(1.0)

    monkeypatch.setattr(J, "ANNOTATION_KEY", tmp_path / "nope.json")
    assert J.judge_human_kappa() is None


def test_annotation_sampling_is_reproducible_and_capped():
    """同 seed 同结果 —— 抽样一漂，人勾过的表就对不上 key，κ 变成错位的功劳。"""
    items = [{"scenario_id": f"S{i // 4}", "depth": "fast", "i": i % 4,
              "text": f"断言{i}", "v": "unsupported" if i % 2 else "supported",
              "why": "", "cite": ""} for i in range(40)]
    g1 = J._sample_for_annotation(items, 12, 2, 20260914)
    g2 = J._sample_for_annotation(items, 12, 2, 20260914)
    assert g1 == g2, "同 seed 必须可复现"
    flat = [(sid, x["i"]) for sid, g in g1 for x in g]
    assert len(flat) == len(set(flat)) == 12, "不许重复抽同一条断言"
    used: dict[str, int] = {}
    for sid, _ in flat:
        used[sid] = used.get(sid, 0) + 1
    assert max(used.values()) <= 2, "同一场景至多 2 条（共用一份证据，抽多了是重复计数）"
    # 两层都抽到（分层抽样不是「全抽 unsupported」）
    vals = {x["v"] for _, g in g1 for x in g}
    assert vals == {"supported", "unsupported"}
    # 组内按断言原序（回复的自然顺序），场景之间洗过牌
    for _, g in g1:
        assert [x["i"] for x in g] == sorted(x["i"] for x in g)
