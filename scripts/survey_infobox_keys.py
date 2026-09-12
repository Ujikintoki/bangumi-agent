#!/usr/bin/env python3
"""条目 infobox 白名单巡检 — 照真实数据决定「加哪个键 / 删哪个键」。

用法::

    source .venv/bin/activate

    # 随机抽 40 部动画（走真实 API，原始响应缓存到 /tmp，重复跑不再打网络）
    python scripts/survey_infobox_keys.py

    # 换样本量 / 换条目类型（1=书籍, 2=动画）
    python scripts/survey_infobox_keys.py --limit 100
    python scripts/survey_infobox_keys.py --type 1

    # 只看指定条目（跳过采样）
    python scripts/survey_infobox_keys.py --ids 8 328609 10380

    # 试算：把这些键临时加进白名单，看记录会胀到多大
    python scripts/survey_infobox_keys.py --what-if 演出 分镜

输出四段：
  1. 白名单命中 —— 哪些键真的在用（命中率极低的键可以删）
  2. 被丢的键   —— 按出现率降序 + 示例值（看着决定要不要加）
  3. 死键       —— 白名单里 0 命中的键（可能是别的条目类型在用，别急着删）
  4. 尺寸       —— 整条记录 token 中位/最大，以及超 L1 单条上限的条数

改完白名单记得同步 test/test_sanitizers.py:test_infobox_scope_boundary
（那条测试就是用来提醒你「这次改动是有意的吗」）。

改键的判据（产品定位）：我们回答「这部作品谁做的」，不回答
「第 7 集的原画师是谁」。见 clients/sanitizers.py:_SUBJECT_INFOBOX_KEEP。
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 数据层（sanitizer）本身不该感知记忆层，但本脚本的活就是横跨两层对账 ——
# 「过滤后的记录装得进 L1 单条上限吗」。上限取自 L1 真实常量而非在脚本里
# 抄一个数字，否则调整上限时这里会静默漂移。
from agent.memory.short_term import _MAX_SINGLE_MESSAGE_TOKENS, count_tokens
from clients import sanitizers
from clients.sanitizers import sanitize_subject_detail

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_CACHE = Path("/tmp/survey_infobox_raw.json")

# 采样池：scripts/data/ 下按条目类型分文件的 ID 清单（由 rag.cli.discover 产出）
_SAMPLE_FILES = {1: "subject_ids_type1.json", 2: "subject_ids_type2.json"}

# 示例值打印宽度（只影响显示，不影响统计）
_EXAMPLE_WIDTH = 46


# ═══════════════════════════════════════════════════════════════════
# 取数
# ═══════════════════════════════════════════════════════════════════


def _load_sample(
    type_id: int, limit: int, seed: int, ids: list[int] | None
) -> list[tuple[int, str]]:
    """返回待扫描的 [(subject_id, 显示名), ...]。"""
    if ids:
        return [(i, str(i)) for i in ids]

    path = DATA_DIR / _SAMPLE_FILES[type_id]
    pool = json.loads(path.read_text(encoding="utf-8"))
    picked = random.Random(seed).sample(pool, min(limit, len(pool)))
    return [(item["id"], item.get("name_cn") or item.get("name") or "?") for item in picked]


async def _fetch_raw(
    sample: list[tuple[int, str]], cache_path: Path, concurrency: int
) -> dict[str, dict]:
    """拉取原始条目响应（缓存到 cache_path）。

    直接用 ``client._get`` 拿 raw —— 白名单是拿 **raw key** 去匹配的，
    而 ``get_subject_detail`` 的返回值已经过 sanitizer 过滤，看不见被丢的键。
    """
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"  ⚠ 缓存 {cache_path} 不可读，忽略")
            cache = {}

    todo = [sid for sid, _ in sample if str(sid) not in cache]
    if todo:
        from clients.client import BangumiClient

        sem = asyncio.Semaphore(concurrency)

        async with BangumiClient() as client:

            async def one(sid: int) -> tuple[str, dict]:
                async with sem:
                    try:
                        return str(sid), await client._get(f"/p1/subjects/{sid}")
                    except Exception as exc:  # 网络抖动不该炸掉整轮扫描
                        return str(sid), {"_error": f"{type(exc).__name__}: {exc}"}

            results = await asyncio.gather(*(one(sid) for sid in todo))
        for sid, raw in results:
            if "_error" in raw:
                print(f"  ⚠ {sid}: {raw['_error']}")
                continue
            cache[sid] = raw
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    return {str(sid): cache[str(sid)] for sid, _ in sample if str(sid) in cache}


# ═══════════════════════════════════════════════════════════════════
# 统计
# ═══════════════════════════════════════════════════════════════════


def _values(item: dict) -> list[str]:
    """infobox 单项 → 非空值列表。"""
    return [v for v in ((x.get("v") or "").strip() for x in item.get("values") or []) if v]


def _collect(records: dict[str, dict]) -> dict:
    """扫一遍 raw infobox，统计各键的出现次数与示例值。"""
    kept_keys = sanitizers._SUBJECT_INFOBOX_KEEP
    hits: dict[str, Counter] = {"keep": Counter(), "drop": Counter()}
    examples: dict[str, dict[str, str]] = {"keep": {}, "drop": {}}
    lengths: dict[str, list[int]] = defaultdict(list)

    for raw in records.values():
        for item in raw.get("infobox") or []:
            key = (item.get("key") or "").strip()
            vals = _values(item)
            if not key or not vals:
                continue
            side = "keep" if key in kept_keys else "drop"
            hits[side][key] += 1
            examples[side].setdefault(key, " / ".join(vals))
            if side == "drop":
                # 均长按「若加进白名单」的形态算：单值 120 字封顶后
                lengths[key].append(min(len(" / ".join(vals)), 120))

    return {"hits": hits, "examples": examples, "lengths": lengths}


def _sizes(records: dict[str, dict], extra_keys: list[str]) -> dict:
    """走 sanitize_subject_detail，统计整条记录尺寸（token / infobox 字符）。"""
    original = sanitizers._SUBJECT_INFOBOX_KEEP
    if extra_keys:
        sanitizers._SUBJECT_INFOBOX_KEEP = original | set(extra_keys)
    try:
        totals: list[int] = []
        infobox_chars: list[int] = []
        for raw in records.values():
            record = sanitize_subject_detail(raw)
            totals.append(count_tokens(json.dumps(record, ensure_ascii=False)))
            infobox = record.get("infobox") or {}
            infobox_chars.append(sum(len(k) + len(v) for k, v in infobox.items()))
    finally:
        sanitizers._SUBJECT_INFOBOX_KEEP = original

    return {
        "n": len(totals),
        "total_median": int(statistics.median(totals)) if totals else 0,
        "total_max": max(totals, default=0),
        "over_limit": sum(1 for t in totals if t > _MAX_SINGLE_MESSAGE_TOKENS),
        "infobox_median": int(statistics.median(infobox_chars)) if infobox_chars else 0,
        "infobox_max": max(infobox_chars, default=0),
    }


# ═══════════════════════════════════════════════════════════════════
# 输出
# ═══════════════════════════════════════════════════════════════════


def _row(key: str, count: int, total: int, example: str, avg_len: int | None = None) -> str:
    ex = example if len(example) <= _EXAMPLE_WIDTH else example[: _EXAMPLE_WIDTH - 1] + "…"
    head = f"  {key:<12} {count:>3}/{total:<3} {count / total:>6.1%}"
    if avg_len is not None:
        head += f" 均长{avg_len:>4}"
    return f"{head}  {ex}"


def _section(title: str) -> None:
    print(f"\n── {title} ──")


def _report(records: dict[str, dict], stats: dict, what_if: list[str]) -> None:
    total = len(records)
    kept, dropped = stats["hits"]["keep"], stats["hits"]["drop"]

    _section(f"1. 白名单命中（{len(sanitizers._SUBJECT_INFOBOX_KEEP)} 键 / 样本 {total} 部）")
    for key, count in kept.most_common():
        print(_row(key, count, total, stats["examples"]["keep"][key]))

    dead = sorted(sanitizers._SUBJECT_INFOBOX_KEEP - set(kept))
    if dead:
        print(f"\n  ⚠ 死键（本样本 0 命中）: {' · '.join(dead)}")
        for key in dead:
            near = difflib.get_close_matches(key, list(dropped), n=3, cutoff=0.34)
            if near:
                print(
                    f"    {key} ←→ 被丢的形近键: "
                    + " · ".join(f"{n}({dropped[n]}部)" for n in near)
                )
        print("    形近但字不同（繁简/日汉字，如 企划↔企画）＝ 白名单写错了字：")
        print("      精确匹配不会报错，只会静默丢弃 —— 这是最该先排除的一种")
        print("    换媒介再扫一遍（--type 1 = 书籍）才有资格判死刑：")
        print("      书籍用的是 作者/出版社/插图 一套完全不同的 key")

    _section(f"2. 被丢的键（{len(dropped)} 个，按出现率降序）")
    for key, count in dropped.most_common():
        avg = int(statistics.mean(stats["lengths"][key])) if stats["lengths"][key] else 0
        print(_row(key, count, total, stats["examples"]["drop"][key], avg_len=avg))
    print("\n  加键：改 clients/sanitizers.py:_SUBJECT_INFOBOX_KEEP（一行）")
    print("  减键：删掉即可；先看第 1 段的命中率，别删还在用的")
    print("  改完同步 test/test_sanitizers.py:test_infobox_scope_boundary")

    _section("3. 尺寸")
    base = _sizes(records, [])
    print(f"  整条 token     中位 {base['total_median']} / 最大 {base['total_max']}")
    print(f"  infobox 字符   中位 {base['infobox_median']} / 最大 {base['infobox_max']}")
    print(
        f"  超 L1 单条上限 {_MAX_SINGLE_MESSAGE_TOKENS}：{base['over_limit']}/{base['n']}"
        + ("  ← 有记录会被截断成坏 JSON，必须处理" if base["over_limit"] else "  ✓")
    )

    if what_if:
        added = [k for k in what_if if k in dropped]
        unknown = [k for k in what_if if k not in dropped]
        if unknown:
            print(f"  （--what-if 忽略未出现的键：{' '.join(unknown)}）")
        test = _sizes(records, added)
        print(f"\n  若加 {' '.join(added) or '（无）'}：")
        print(
            f"    整条 token 中位 {base['total_median']} → {test['total_median']}"
            f"，最大 {base['total_max']} → {test['total_max']}"
            f"，超限 {base['over_limit']} → {test['over_limit']}"
        )
        print(
            f"    infobox 字符 最大 {base['infobox_max']} → {test['infobox_max']}"
            "（>600 会触发整块预算，值最长的键被整键丢弃）"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="条目 infobox 白名单巡检")
    parser.add_argument("--limit", type=int, default=40, help="采样条目数（默认 40）")
    parser.add_argument("--type", type=int, default=2, choices=sorted(_SAMPLE_FILES),
                        help="条目类型：1=书籍 2=动画（默认 2）")
    parser.add_argument("--seed", type=int, default=0, help="采样随机种子（默认 0）")
    parser.add_argument("--ids", type=int, nargs="+", help="直接指定条目 ID（跳过采样）")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="原始响应缓存路径")
    parser.add_argument("--concurrency", type=int, default=4, help="并发请求数（默认 4）")
    parser.add_argument("--what-if", nargs="+", default=[], metavar="KEY",
                        help="试算：把这些键临时加进白名单后的尺寸")
    args = parser.parse_args()

    sample = _load_sample(args.type, args.limit, args.seed, args.ids)
    print(f"扫描 {len(sample)} 个条目（缓存 {args.cache}）…")
    records = asyncio.run(_fetch_raw(sample, args.cache, args.concurrency))
    if not records:
        print("没有拿到任何数据 —— 检查 BANGUMI_ACCESS_TOKEN 与网络")
        return 1

    names = dict(sample)
    print("样本: " + " · ".join(names[int(sid)] for sid in list(records)[:8])
          + (f" … 共 {len(records)} 部" if len(records) > 8 else ""))

    _report(records, _collect(records), args.what_if)
    return 0


if __name__ == "__main__":
    sys.exit(main())
