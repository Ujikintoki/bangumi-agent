"""轴 3 Tier 2 · 忠实性判官 —— 证据投影（evidence-v2）与 dry-run

判什么
    回复里的「可核查的具体断言」能不能在【Agent 当时看到的工具返回】里找到依据。
    三值：supported / unsupported / unknown。吐槽、观点、寒暄不进分母。

为什么要「判官侧再投影」一层
    录制侧落盘的证据（card-v1，见 ``reply_quality_eval.py``）是判官的【唯一】依据。
    实测 2026-09-14 的 n=30 冻结样本：24 条有证据的场景共 20.1 万字，
    ``search_local_bangumi`` 一个工具占 62.5%；最坏的 ``C5-deep必调工具`` 一条
    6.97 万字 / 62 张卡片，而那条回复只提到其中 1 部作品。
    证据一长，判官的经典失败模式是「没看见就判没有」——把真话记成幻觉。
    投影就是把「判官实际看到的东西」变成【预注册的、可审计的口径】。

两条硬规矩
    ① 裁掉的内容判官看不到 ⇒ 依赖它的断言必须判 ``unknown``，**不得判 unsupported**。
       所以每一档投影都要能回答「你裁了什么」（``omitted`` 随判官 prompt 一起给）。
    ② ``evidence-v2`` 是【判官侧】口径，与录制侧的 ``card-v1`` 是两层：card-v1 决定
       盘上有什么，v2 决定判官看什么。改 v2 不动盘上样本，但必须进判官缓存键
       —— 换了投影就是换了证据，旧判定一律作废。

四档投影（2026-09-14 实测对比见 ``--tier2-dry-run``）
    ``raw``   原样，不投影（baseline，= 录制侧 card-v1 的产出）
    ``field`` 全卡字段收紧：砍纯导航字段、封 summary/tags/infobox 的顶
    ``index`` 未命中的卡片压成一行「定位记录」，命中的给全卡
    ``hit``   只留命中的卡片整卡

    ``index`` / ``hit`` 依赖「回复提到哪些条目」。**判据必须容忍简称**：``A5`` 的回复
    通篇说「EVA」，卡片名是「新世纪福音战士」，全名匹配会命中 0 张，把那张卡降级成
    定位行 —— 回复里关于它的断言就全被逼成 unknown。所以判据放宽到 n-gram，
    代价是过度匹配（回复说「巨人」会把「巨人の星」一起算命中）。**过度匹配是安全
    方向**：多留一张只是噪声，漏留一张会把有据的断言记成没依据，那是测量事故。

    另：``index`` 不是"只给判官看零头"—— 单条记录的工具返回（``get_bangumi_subject_detail``
    这类定向查询）**不参与筛选**，原样全给。所以"回复真正讨论的那部作品"的班底/简介
    通常来自定向查询，不受影响；被压成定位行的只是检索列表里的候选。
"""

from __future__ import annotations

import json
from typing import Any

# ═══════════════════════════════════════════════════════════════════════
# 投影口径
# ═══════════════════════════════════════════════════════════════════════

EVIDENCE_POLICY_V2 = "evidence-v2"

# 纯导航字段：给 Agent 看的「下一步调什么」，判官核断言时一文不值。
# 实测 ``_next`` 平均 76–114 字/次调用，C5 那条光它就占 ~4.7k 字。
_DROP_KEYS = ("_next",)

# 定位记录保留的字段：让判官能确认「这句话说的是哪条记录」，但不含剧情/班底。
_INDEX_KEEP = ("id", "name", "name_cn", "type", "date", "eps", "score", "rank", "info")

# 整卡（命中卡）的字段顶。比录制侧 card-v1 更紧，切掉的进 omitted。
_SUMMARY_CHARS = 150
_TAG_KEEP = 12
_INFOBOX_KEYS = 6
_INFOBOX_VALUE_CHARS = 60

_POLICIES = ("raw", "field", "index", "hit")


def _as_text(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


def _strip(d: dict, drop=_DROP_KEYS) -> dict:
    return {k: v for k, v in d.items() if k not in drop}


# ═══════════════════════════════════════════════════════════════════════
# 条目名 —— 供 index / hit 两档筛卡片
# ═══════════════════════════════════════════════════════════════════════


# 条目 vs 标签的判据：真条目有 id，标签是 ``{"name": "心理", "count": 12}``。
# 2026-09-14 踩过：只看「有没有 name」会把 30 个标签当成 30 条记录 —— 于是
# ``get_bangumi_subject_detail`` 的标签被投影成"卡片"，而「心理」「意识流」这种
# 标签名又被当成"回复点名的条目"，投影表和命中判定一起错。
_RECORD_ID_KEYS = ("id", "character_id", "subject_id", "person_id")


def _is_record(obj: Any) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in _RECORD_ID_KEYS)


def iter_record_lists(result: Any):
    """返回 result 里所有「条目列表」的 (键, 列表)。

    **投影与诊断共用它** —— dry-run 报的「命中卡/定位卡」必须和 ``project_result``
    实际投影的对象一一对应，两处各写一遍判断就等于给假数字。
    """
    if not isinstance(result, dict):
        return
    for k, v in result.items():
        if isinstance(v, list) and v and _is_record_list(v):
            yield k, v


def iter_records(result: Any):
    """result 里所有参与投影的记录（诊断用）。"""
    for _, lst in iter_record_lists(result):
        yield from (r for r in lst if isinstance(r, dict) and _is_record(r))


def _names_of(rec: dict) -> list[str]:
    """一条记录可能被回复用哪个名字称呼。"""
    out = []
    for k in ("name", "name_cn", "subject_name", "title"):
        v = rec.get(k)
        if isinstance(v, str) and len(v.strip()) >= 2:
            out.append(v.strip())
    return out


def _mentioned(rec: dict, reply: str) -> bool:
    """回复里出现这条记录吗 —— 全名命中，或回复用了简称。

    为什么要管简称（2026-09-14 实测踩到）：``A5-deep深入`` 的回复通篇说「EVA」，
    而卡片名是「新世纪福音战士」，全名包含判定命中 0 张 —— 于是那张卡被降级成
    定位行，回复里关于它的断言全被逼成 unknown。**判官看不到证据 ≠ 模型在编。**

    简称判据：名字的任意 n-gram 出现在回复里（中日文取 2 字，拉丁字母取 3 字 ——
    ``EVA`` 这类缩写靠它命中，「an」这类英文碎片不至于乱撞）。
    宁可多留：多留一张只是噪声，漏留一张会污染指标。
    """
    for n in _names_of(rec):
        if n in reply:
            return True
        k = 3 if n.isascii() else 2
        if len(n) > k and any(n[i:i + k] in reply for i in range(len(n) - k + 1)):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# 卡片级投影
# ═══════════════════════════════════════════════════════════════════════


def _tighten_card(card: dict) -> dict:
    """字段收紧：砍导航字段，封 summary / tags / infobox 的顶。"""
    out = _strip(card)
    if isinstance(out.get("summary"), str):
        out["summary"] = out["summary"][:_SUMMARY_CHARS]
    tags = out.get("tags")
    if isinstance(tags, list):
        out["tags"] = tags[:_TAG_KEEP]
    infobox = out.get("infobox")
    if isinstance(infobox, dict):
        capped = {}
        for i, (k, v) in enumerate(infobox.items()):
            if i >= _INFOBOX_KEYS:
                break
            capped[k] = v[:_INFOBOX_VALUE_CHARS] if isinstance(v, str) else v
        out["infobox"] = capped
    return out


def _index_card(card: dict) -> dict:
    """压成一行定位记录：这条作品是什么、哪年、几分。"""
    return {k: card[k] for k in _INDEX_KEEP if k in card}


def _project_card(card: dict, reply: str, policy: str, stats: dict) -> dict:
    if not isinstance(card, dict) or not _is_record(card):
        return card
    if policy == "raw":
        return card
    if policy == "field":
        return _tighten_card(card)
    hit = _mentioned(card, reply)
    if hit:
        stats["hit_cards"] += 1
        return _tighten_card(card)
    stats["index_cards"] += 1
    return _index_card(card) if policy == "index" else None


def _project_list(items: list, reply: str, policy: str, stats: dict) -> list:
    out = []
    for it in items:
        p = _project_card(it, reply, policy, stats)
        if p is not None:          # hit 档下未命中的整条丢弃
            out.append(p)
    return out


def project_result(result: Any, reply: str, policy: str, stats: dict) -> Any:
    """投影一次工具返回。非 ``{"results": [...]}`` 形状的（detail/opinions/…）原样放行。

    那些工具实测都 < 4000 字、且判官要核的班底/口碑信息全在里面，砍它们得不偿失。
    """
    if not isinstance(result, dict):
        return result
    out = _strip(result)
    for k, lst in iter_record_lists(result):
        out[k] = _project_list(lst, reply, policy, stats)
    for k, v in result.items():
        if isinstance(v, dict):
            out[k] = _strip(v)
    return out


def _is_record_list(items: list) -> bool:
    """是「条目列表」还是「标签/字符串列表」——只投影前者。

    看前三条里有没有带 id 的 dict：标签 ``{"name","count"}`` 与字符串列表都被排除。
    """
    return any(_is_record(i) for i in items[:3])


# ═══════════════════════════════════════════════════════════════════════
# 场景级投影
# ═══════════════════════════════════════════════════════════════════════


def project_sample(sample: dict, policy: str) -> tuple[list[dict], dict]:
    """把一个场景的证据投影成判官要看的样子。返回 (证据, 统计)。"""
    reply = sample.get("reply") or ""
    stats = {"calls": 0, "hit_cards": 0, "index_cards": 0, "chars": 0}
    ev = sample.get("evidence") or []
    out = []
    for e in ev:
        rec = _strip(e)
        rec["result"] = project_result(e.get("result"), reply, policy, stats)
        rec["_chars"] = len(_as_text(rec["result"]))
        stats["chars"] += rec["_chars"]
        stats["calls"] += 1
        out.append(rec)
    return out, stats


def omitted_note(policy: str) -> list[str]:
    """这一档裁掉了什么。判官 prompt 必须带上它 —— 否则「看不到」会被判成「不存在」。"""
    if policy == "raw":
        return []
    note = ["_next（工具导航提示，非数据）"]
    if policy == "field":
        return note + [
            f"summary 超 {_SUMMARY_CHARS} 字的部分",
            f"tags 第 {_TAG_KEEP} 个及其后",
            f"infobox 第 {_INFOBOX_KEYS} 键及其后",
            f"infobox 值超 {_INFOBOX_VALUE_CHARS} 字的部分",
        ]
    if policy == "index":
        return note + [
            "未在回复中被点名的条目：只保留 "
            + "/".join(_INDEX_KEEP) + "，其剧情简介、标签、infobox 一律未提供"
            "（依赖它们的断言判 unknown）",
        ]
    return note + ["未在回复中被点名的条目：整条未提供（依赖它们的断言判 unknown）"]


# 推荐档位。``hit`` 只是给对比用的下界，不作为生产口径 —— 它整条删证据，
# 会把「判官看不到」放大成「没有依据」，指标随之失真。
RECOMMENDED_POLICY = "index"


# ═══════════════════════════════════════════════════════════════════════
# dry-run —— 只打印，不调 LLM、不花钱
# ═══════════════════════════════════════════════════════════════════════


def dry_run_report(frozen: dict, policies: tuple[str, ...] = _POLICIES) -> dict:
    samples = frozen.get("samples") or []
    no_ev = [s["scenario_id"] for s in samples if not (s.get("evidence") or [])]
    rows = []
    for s in samples:
        if not (s.get("evidence") or []):
            continue
        row = {"scenario_id": s["scenario_id"], "depth": s.get("depth"),
               "reply_chars": len(s.get("reply") or ""), "by_policy": {}}
        for p in policies:
            _, st = project_sample(s, p)
            row["by_policy"][p] = st
        rows.append(row)
    return {"rows": rows, "no_evidence": no_ev, "policies": list(policies)}


def print_dry_run(rep: dict) -> None:
    policies = rep["policies"]
    rows = rep["rows"]
    print(f"\n{'=' * 100}\n判官侧证据投影 dry-run（不调 LLM）· 场景 {len(rows)} 条有证据"
          f" + {len(rep['no_evidence'])} 条无证据\n{'=' * 100}")
    head = f"{'scenario':<16}{'回复':>5}" + "".join(f"{p:>12}" for p in policies)
    print(head + f"{'命中卡':>7}{'定位卡':>7}")
    print("-" * 100)
    for r in rows:
        cells = "".join(f"{r['by_policy'][p]['chars']:>12,}" for p in policies)
        st = r["by_policy"]["index"]
        print(f"{r['scenario_id']:<16}{r['reply_chars']:>5}{cells}"
              f"{st['hit_cards']:>7}{st['index_cards']:>7}")
    print("-" * 100)
    totals = {p: sum(r["by_policy"][p]["chars"] for r in rows) for p in policies}
    print(f"{'合计（字）':<21}" + "".join(f"{totals[p]:>12,}" for p in policies))
    print(f"{'均摊/场景':<21}" + "".join(f"{totals[p] // len(rows):>12,}" for p in policies))
    if "raw" in totals:
        for p in policies:
            if p != "raw":
                print(f"  相对 raw：{p:<6} 压到 {100 * totals[p] / totals['raw']:.1f}%"
                      f"  （省 {totals['raw'] - totals[p]:,} 字）")
    print(f"\n无证据场景（判不了忠实性，须单列不进分母）{len(rep['no_evidence'])} 条："
          f"{'、'.join(rep['no_evidence'])}")
    for p in policies:
        note = omitted_note(p)
        if note:
            mark = "  ← 推荐" if p == RECOMMENDED_POLICY else ""
            print(f"\n[{p}] 裁掉了什么：{mark}")
            for n in note:
                print(f"    - {n}")
    print(f"\n盘上样本不动（录制侧 card-v1 决定盘上有什么）；投影只决定【判官看什么】。")


def load_frozen(path: str) -> dict:
    from pathlib import Path
    return json.loads(Path(path).read_text(encoding="utf-8"))
