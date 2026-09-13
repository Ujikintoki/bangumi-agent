"""
轴 3 · 回复质量 —— 生成 / 判定分离

为什么要分两半
    生成（跑一次 /chat）贵、有抖动、要联网；判定（读一段现成回复打勾）便宜、
    确定性、可离线。把生成结果【冻结成样本】落盘之后，判定就变成对一份死文件的
    纯函数：同一份样本可以反复判、换 rubric 重判、事后复查，都不必再跑一次生成。
    这是本轴能出"靠谱数据"的前提 —— 判定的数字不随 LLM 抖动。

密闭性（本轴特有的硬要求）
    L2 记忆召回是按 ``user_id`` 检索的（``agent/memory/long_term.py`` 里
    ``SessionMemory.user_id == user_id``），``session_id`` **不参与**召回查询。
    而 e2e 场景一直共用 ``user_id="eval"``，于是：第 N 次运行一开局就带着第 N-1 次
    的成绩，同一轮里排在前面的场景也会污染后面的。
    轴 2 不受影响（纯检索，不生成文本），但轴 3 判的就是文本 —— 带着上轮记忆生成
    的回复，质量分数没法归因。
    所以录制侧给【每个场景】一个 run-scoped 的 user_id，跑完即与历史完全隔离。

两层
    Tier 1  确定性，¥0，每次跑：空回复 / 报错 / 硬截断 / 降级 / 字数 / 格式泄漏。
            ⚠ 字数对【硬截断上界】的合规率恒为 100%（render 会把回复截到 <= 280/480），
            所以那个数没有信息量。真正有信息量的是三个：
              ① 硬截断触发率  —— LLM 写超了、被砍了（回复不完整）
              ② 软上限超标率  —— 超过 prompt 建议字数但还没被砍（内容偏长）。
                                  上限是 (人格, depth) 的函数：base 200/350
                                  （persona/render.py:_WORD_LIMIT），kawaii 的
                                  fast 覆盖成 250（profiles.py:360）
              ③ 降级率        —— render 挂了走 _degrade_render_input，
                                  那条路径【不过硬截断】，是唯一能真超上界的情况
            「报错」认的是 main.py 的 canned 兜底文案，不只是"抱歉"开头 ——
            否则像"工具执行完成但未能生成文本回复"这种彻底失败的轮次会全勾通过。
    Tier 2  LLM-as-judge rubric —— 待建。约束见 eval/README.md「LLM-as-judge 的工程约束」。

用法::

    python -m eval.reply_quality_eval --record                # 录制冻结样本（全部场景）
    python -m eval.reply_quality_eval --record --offline      # 只录不依赖 Bangumi API 的场景
    python -m eval.reply_quality_eval --record --smoke 3      # 冒烟：只跑前 N 条
    python -m eval.reply_quality_eval --judge <frozen.json>   # 判定（离线、免费、可反复跑）
    python -m eval.reply_quality_eval --list                  # 列出已有冻结样本
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS_DIR = Path(__file__).resolve().parent / "results"
FROZEN_DIR = RESULTS_DIR / "frozen"

# 本地检索工具 —— 只有它不依赖 Bangumi API（走 pgvector + embedding）
_LOCAL_TOOLS = {"search_local_bangumi"}


# ═══════════════════════════════════════════════════════════════════════
# 通用
# ═══════════════════════════════════════════════════════════════════════


def _git_hash() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(RESULTS_DIR.parent.parent), timeout=5,
        ).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def _settings_snapshot() -> dict:
    """记 settings 快照。

    这些值不在 git 里 —— 代码没变数字也在变，只记 githash 复现不了一次实验
    （eval/README.md「方法论约定 §3」）。
    """
    try:
        from core.config import get_settings
        s = get_settings()
        return {
            "MEMORY_ENABLED": s.MEMORY_ENABLED,
            "MEMORY_RECALL_TOP_K": s.MEMORY_RECALL_TOP_K,
            "MEMORY_RECALL_THRESHOLD": s.MEMORY_RECALL_THRESHOLD,
            "MEMORY_RECENCY_FALLBACK_THRESHOLD": s.MEMORY_RECENCY_FALLBACK_THRESHOLD,
            "MEMORY_TIME_DECAY_HALF_LIFE_DAYS": s.MEMORY_TIME_DECAY_HALF_LIFE_DAYS,
            "LLM_MODEL": s.LLM_MODEL,
            "LLM_TEMPERATURE": s.LLM_TEMPERATURE,
            "EMBEDDING_MODEL": s.EMBEDDING_MODEL,
        }
    except Exception as exc:  # 快照失败不该中断录制
        return {"_error": f"{type(exc).__name__}: {exc}"}


def _scenario_needs_remote_tools(scen: dict) -> bool:
    """场景是否必须有【Bangumi API 工具】才能跑。

    require_tools 是硬需求；require_any_tools 是"每组任一"，只要组里有本地工具
    就算能跑。forbid_tools 里的 '*' 表示禁调全部工具 —— 那种场景反而最干净。
    """
    require = set(scen.get("require_tools") or [])
    if require - _LOCAL_TOOLS:
        return True
    for group in scen.get("require_any_tools") or []:
        if not (set(group) & _LOCAL_TOOLS):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# 录制侧（贵、有抖动）—— 跑 /chat，把回复冻结成样本
# ═══════════════════════════════════════════════════════════════════════


class _RenderSignalHandler(logging.Handler):
    """抓 render 层的两条 WARNING —— 它们是回复质量的直接证据。

    「硬截断」和「降级」都只以日志形式存在：``ChatResponse`` 里没有对应字段，
    telemetry 也只有节点耗时和 token 数（``agent/devtools.py``）。所以想测这两件事，
    只有挂 handler 听日志这一条路。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.hard_cutoff = False
        self.degraded = False

    def reset(self) -> None:
        self.hard_cutoff = False
        self.degraded = False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "硬截断" in msg:
            self.hard_cutoff = True
        if "降级为清理后的原始文本" in msg:
            self.degraded = True


async def _record_scenario(client, scen: dict, run_id: str, handler: _RenderSignalHandler) -> dict:
    handler.reset()
    payload = {
        "message": scen["message"],
        "depth": scen.get("depth", "fast"),
        "output_style": scen.get("output_style", "bangumi"),
        "session_id": f"eval-{scen['id']}",
        # ★ 密闭性的全部秘密在这一行：user_id 带 run_id 和场景 id，
        #   所以 L2 召回永远查不到历史运行、也查不到同一轮的别的场景。
        "user_id": f"eval-{run_id}-{scen['id']}",
    }
    t0 = time.monotonic()
    try:
        resp = await client.post("/chat", json=payload)
        body = resp.json()
        status_code = resp.status_code
    except Exception as exc:
        return {
            "scenario_id": scen["id"], "message": scen["message"],
            "depth": payload["depth"], "output_style": payload["output_style"],
            "expect_intent": scen.get("expect_intent"), "status_code": 0,
            "reply": "", "query_intent": "error", "tools_used": [],
            "latency_ms": 0.0, "render": {"hard_cutoff": False, "degraded": False},
            "error": f"{type(exc).__name__}: {exc}", "telemetry": None,
        }
    latency_ms = (time.monotonic() - t0) * 1000

    return {
        "scenario_id": scen["id"],
        "message": scen["message"],
        "depth": payload["depth"],
        "output_style": payload["output_style"],
        "expect_intent": scen.get("expect_intent"),
        "status_code": status_code,
        "reply": body.get("reply", "") or "",
        "query_intent": body.get("query_intent", "unknown"),
        "tools_used": body.get("tools_used", []) or [],
        "iterations": body.get("iterations", 0),
        "latency_ms": round(latency_ms, 1),
        "render": {"hard_cutoff": handler.hard_cutoff, "degraded": handler.degraded},
        "telemetry": body.get("telemetry"),
    }


async def record(scenarios: list[dict], smoke: int, offline: bool, scenario_file: Path) -> dict:
    from core.config import get_settings
    settings = get_settings()
    settings.DEV_MODE = True  # 拿 telemetry

    from main import app  # 必须在 DEV_MODE 置位之后
    from httpx import ASGITransport, AsyncClient

    run_id = time.strftime("%Y%m%d-%H%M%S")

    handler = _RenderSignalHandler()
    for name in ("bgm-agent.render", "bgm-agent"):
        logging.getLogger(name).addHandler(handler)

    to_run = scenarios[:smoke] if smoke else scenarios
    skipped: list[dict] = []
    if offline:
        kept = []
        for s in to_run:
            if _scenario_needs_remote_tools(s):
                skipped.append({"id": s["id"], "reason": "需要 Bangumi API 工具（--offline 跳过）"})
            else:
                kept.append(s)
        to_run = kept

    samples: list[dict] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://eval") as client:
        for i, scen in enumerate(to_run):
            samples.append(await _record_scenario(client, scen, run_id, handler))
            print(f"  录制 {i + 1}/{len(to_run)}: {scen['id']}")

    for name in ("bgm-agent.render", "bgm-agent"):
        logging.getLogger(name).removeHandler(handler)

    return {
        "meta": {
            "axis": "轴 3 · 回复质量（生成侧冻结样本）",
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_id": run_id,
            "git_hash": _git_hash(),
            "scenario_file": str(scenario_file),
            "n_recorded": len(samples),
            "skipped": skipped,
            "offline_mode": offline,
            "isolation": (
                f"user_id=eval-{run_id}-<scenario_id> —— L2 召回按 user_id 检索"
                "（session_id 不参与查询），故每个场景开局都是零记忆"
            ),
            "settings_snapshot": _settings_snapshot(),
            "known_limits": [
                "LLM 非确定（RENDER_TEMPERATURE=0.4, LLM_TEMPERATURE 见快照）→ 看分布与 delta，不看单条",
                "场景真实调用 Bangumi API（离线环境下工具的 _error 会进回复）",
                "Tier 2（LLM-judge rubric）未建，本样本只够跑 Tier 1",
            ],
        },
        "samples": samples,
    }


# ═══════════════════════════════════════════════════════════════════════
# 判定侧（便宜、确定性、离线）—— 读冻结样本打勾
# ═══════════════════════════════════════════════════════════════════════

# 泄漏模式与 main.py:_degrade_render_input 的清理范围【一一对应】——
# 那边清什么，这边就该查什么，否则要么漏报要么误报。
# 改动 _degrade_render_input 时同步改这里（数字类常量直接从 render.py import，
# 见 _limits()，就是为了不让它们和上面那份清单一样靠人脑同步）。
_LEAK_PATTERNS = {
    "markdown表格": re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE),
    "markdown标题": re.compile(r"^\s*#{1,6}\s+\S", re.MULTILINE),
    "markdown分隔线": re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE),
    "emoji": re.compile(r"[\U0001F300-\U0001F9FF☀-➿⭐✀-➿️]"),
    "代码围栏": re.compile(r"^\s*```", re.MULTILINE),
    # <function_calls> / <invoke> 这类工具调用 XML 泄漏（agent/guardrails.py 同款）
    "XML工具调用": re.compile(r"<\s*(?:function_calls|invoke|parameter|xml)[\s>]", re.IGNORECASE),
    # render 失败 → 降级返回 _degrade_render_input(render_input)，而 chat 分支的
    # render_input 里写着【给模型看的指令】（main.py:_render_final_reply）：
    #   用户对你说：<用户原话>\n\n这是一段闲聊。自然地用你的角色性格回复。不要列数据、
    #   不要提搜索、就像朋友聊天一样。
    # _degrade_render_input 只清 emoji/markdown，不清这些脚手架 —— 于是内部提示词
    # 会被原样吐给用户。这比 emoji 泄漏严重得多，单独成一类。
    "prompt脚手架": re.compile(
        r"用户对你说：|这是一段闲聊|不要列数据|不要提搜索|就像朋友聊天一样"
    ),
}

# 报错回复的特征（graph_smoke.py 同款判定）
# main.py 里那几条 canned 兜底回复（`:690`/`:694`/`:696` 的字面量）。它们既不是
# 空串、也不以"抱歉"开头，所以不列在这里就会当成正常回复通过全部六个勾 ——
# A5-deep深入 的 27 字 "工具执行完成但未能生成文本回复" 就是这么漏掉的：
# 勾全绿，而那一轮其实是彻底的失败。改 main.py 的兜底文案时同步改这里。
# （不 import main：那会把 FastAPI app 整个拽进来，判定侧要的是纯函数。）
_ERROR_MARKERS = (
    "查询处理超时",
    "查询达到最大处理轮次",
    "工具执行完成但未能生成文本回复",
)
_ERROR_PREFIX = "抱歉"


def _limits() -> tuple[dict, dict]:
    """字数上下界 —— 单一事实源，避免判定侧抄错数字。

    软上限是 **(人格, depth)** 的函数，不是 depth 的函数：`render.py:77-79` 先取
    `_WORD_LIMIT[depth]`，再用 `character.word_limit_override[depth]` 覆盖
    （kawaii 配了 `{"fast": "250"}`，见 `profiles.py:360`）。只读那张平表的话，
    kawaii 的 222 字会被判成"超软上限"—— 而生产 prompt 里写的就是 250。
    那是判定侧误报，不是模型写超了。

    硬截断上界没有 per-persona 覆盖，仍是 depth 的平表。
    """
    from agent.persona.profiles import CHARACTER_REGISTRY
    from agent.persona.render import _HARD_CUTOFF_MAX_CHARS, _WORD_LIMIT

    base = {k: int(v) for k, v in _WORD_LIMIT.items()}
    soft: dict[tuple[str, str], int] = {}
    for style, character in CHARACTER_REGISTRY.items():
        override = character.word_limit_override or {}
        for d, limit in base.items():
            soft[(style, d)] = int(override.get(d, limit))
    hard = {k: v for k, v in _HARD_CUTOFF_MAX_CHARS.items()}
    return soft, hard


def _is_error_reply(reply: str) -> bool:
    return reply.startswith(_ERROR_PREFIX) or any(m in reply for m in _ERROR_MARKERS)


def judge_sample(sample: dict, soft: dict, hard: dict) -> dict:
    """给单条样本打 Tier 1 的勾。纯函数，无 IO、无 LLM。"""
    reply = sample.get("reply") or ""
    depth = sample.get("depth", "fast")
    style = sample.get("output_style", "bangumi")
    # 认不出的人格退回默认人格那一列，而不是让 soft_limit=None 静默把这条判成合规
    soft_limit = soft.get((style, depth), soft.get(("bangumi", depth)))
    hard_limit = hard.get(depth)

    leaks = sorted(k for k, pat in _LEAK_PATTERNS.items() if pat.search(reply))
    render = sample.get("render") or {}

    return {
        "scenario_id": sample.get("scenario_id"),
        "depth": depth,
        # 人格与生效的软上限都写进判决里 —— 上限是 per-persona 的，
        # 只报一个全局数就看不出"这条到底拿哪条线量的"
        "persona": style,
        "soft_limit": soft_limit,
        "chars": len(reply),
        "empty": not reply.strip(),
        "error_reply": _is_error_reply(reply) if reply else False,
        "over_soft_limit": bool(soft_limit and len(reply) > soft_limit),
        # 硬截断未触发时，回复必须真的在上界内 —— 这条是【可以】失败的：
        # 走降级路径的回复不过硬截断，真的会超。
        "over_hard_limit": bool(hard_limit and len(reply) > hard_limit),
        "hard_cutoff": bool(render.get("hard_cutoff")),
        "render_degraded": bool(render.get("degraded")),
        "leaks": leaks,
        "latency_ms": sample.get("latency_ms"),
        "tools_used": sample.get("tools_used") or [],
        "query_intent": sample.get("query_intent"),
    }


def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "—"


def judge(frozen: dict) -> dict:
    """判定整批冻结样本。"""
    soft, hard = _limits()
    samples = frozen.get("samples") or []
    verdicts = [judge_sample(s, soft, hard) for s in samples]

    def agg(rows: list[dict]) -> dict:
        n = len(rows)
        if not n:
            return {"n": 0}
        chars = [r["chars"] for r in rows]
        leak_kinds: dict[str, int] = {}
        for r in rows:
            for k in r["leaks"]:
                leak_kinds[k] = leak_kinds.get(k, 0) + 1
        return {
            "n": n,
            "empty": sum(r["empty"] for r in rows),
            "error_reply": sum(r["error_reply"] for r in rows),
            "hard_cutoff": sum(r["hard_cutoff"] for r in rows),
            "render_degraded": sum(r["render_degraded"] for r in rows),
            "over_soft_limit": sum(r["over_soft_limit"] for r in rows),
            "over_hard_limit": sum(r["over_hard_limit"] for r in rows),
            "any_leak": sum(bool(r["leaks"]) for r in rows),
            "leak_kinds": dict(sorted(leak_kinds.items())),
            "chars_p50": round(statistics.median(chars), 1),
            "chars_p95": round(sorted(chars)[min(n - 1, int(n * 0.95))], 1),
            "chars_max": max(chars),
        }

    by_depth = {d: agg([r for r in verdicts if r["depth"] == d])
                for d in sorted({r["depth"] for r in verdicts})}

    # 软上限是 per-persona 的：base 收成 depth 平表，只把偏离 base 的覆盖单独列出来，
    # 否则 report 里会是一长串 ("bangumi","fast") 这种元组键（json 还序列化不了）
    base: dict[str, int] = {}
    for (_style, d), v in soft.items():
        base.setdefault(d, v)
    soft_report: dict[str, int] = dict(base)
    for (style, d), v in soft.items():
        if v != base[d]:
            soft_report[f"{style}/{d}"] = v

    return {
        "meta": frozen.get("meta", {}),
        "judged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "overall": agg(verdicts),
        "by_depth": by_depth,
        "limits": {"soft_prompt_suggested": soft_report, "hard_cutoff": hard},
        "verdicts": verdicts,
    }


def _print_judge(rep: dict) -> None:
    o = rep["overall"]
    n = o["n"]
    lim = rep["limits"]

    print("\n" + "=" * 72)
    print("  轴 3 · 回复质量 — Tier 1（确定性，免费）")
    print("=" * 72)
    m = rep["meta"]
    print(f"  样本: {m.get('n_recorded')} 条 | 录制于 {m.get('recorded_at')} | git {m.get('git_hash')}")
    print(f"  隔离: {m.get('isolation', '')[:70]}")
    print()
    print(f"  {'指标':<18}{'总体':>10}{'':>4}" + "".join(f"{d:>12}" for d in rep['by_depth']))
    print("  " + "-" * (32 + 12 * len(rep["by_depth"])))

    def row(label: str, key: str) -> None:
        cells = "".join(f"{_pct(rep['by_depth'][d].get(key, 0), rep['by_depth'][d]['n']):>12}"
                        for d in rep["by_depth"])
        print(f"  {label:<18}{_pct(o[key], n):>10}{'':>4}{cells}")

    row("空回复", "empty")
    row("报错回复", "error_reply")
    row("硬截断触发", "hard_cutoff")
    row("render 降级", "render_degraded")
    row("超软上限", "over_soft_limit")
    row("超硬截断上界", "over_hard_limit")
    row("有任何格式泄漏", "any_leak")

    print()
    print(f"  字数 p50/p95/max: {o['chars_p50']} / {o['chars_p95']} / {o['chars_max']}")
    print(f"  软上限(prompt建议)={lim['soft_prompt_suggested']}  硬截断上界={lim['hard_cutoff']}")
    if o["leak_kinds"]:
        print(f"  泄漏明细: " + "、".join(f"{k}×{v}" for k, v in o["leak_kinds"].items()))

    print()
    print("  读法:")
    print("    · 「超硬截断上界」本该恒为 0（render 会截）；不是 0 就说明有回复走了")
    print("      降级路径 —— 那条路不过硬截断。它是【能真超上界】的唯一入口。")
    print("    · 「硬截断触发」≠ 违规：prompt 建议字数低于截断线（fast 200<280），")
    print("      中间这段是缓冲。但它意味着回复被砍过，可能不完整。")
    print("    · 字数对硬截断的【合规率】恒 100%，所以这里不报合规率 —— 报了也没信息量。")


def _save_judge(rep: dict, frozen_path: Path) -> tuple[Path, Path]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = RESULTS_DIR / f"reply_quality-judge-{stamp}-{rep['meta'].get('git_hash', 'nogit')}"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    o = rep["overall"]
    n = o["n"]
    m = rep["meta"]
    lines = [
        "# 轴 3 · 回复质量 — Tier 1 判定报告",
        f"\n**判定于** {rep['judged_at']} | **样本** `{frozen_path.name}`"
        f" | **录制于** {m.get('recorded_at')} | **git** {m.get('git_hash')}",
        f"\n> 生成/判定分离：本报告只读冻结样本，不重新生成 —— 同一份样本可反复判定。",
        f"\n> 隔离：{m.get('isolation')}",
        f"\n## 设置快照\n",
        "| 项 | 值 |", "|---|---|",
    ]
    for k, v in (m.get("settings_snapshot") or {}).items():
        lines.append(f"| `{k}` | `{v}` |")
    lines += [
        f"\n## Tier 1 指标（n={n}）\n",
        "| 指标 | 总体 | " + " | ".join(rep["by_depth"]) + " |",
        "|---|---|" + "---|" * len(rep["by_depth"]),
    ]

    def mrow(label: str, key: str) -> str:
        cells = " | ".join(_pct(rep["by_depth"][d].get(key, 0), rep["by_depth"][d]["n"])
                           for d in rep["by_depth"])
        return f"| {label} | {_pct(o[key], n)} | {cells} |"

    for label, key in [("空回复", "empty"), ("报错回复", "error_reply"),
                       ("硬截断触发", "hard_cutoff"), ("render 降级", "render_degraded"),
                       ("超软上限", "over_soft_limit"), ("超硬截断上界", "over_hard_limit"),
                       ("有任何格式泄漏", "any_leak")]:
        lines.append(mrow(label, key))

    lines += [
        f"\n**字数** p50={o['chars_p50']} p95={o['chars_p95']} max={o['chars_max']}"
        f"（软上限 {rep['limits']['soft_prompt_suggested']}，硬截断 {rep['limits']['hard_cutoff']}）",
    ]
    if o["leak_kinds"]:
        lines.append("\n**泄漏明细**：" + "、".join(f"{k}×{v}" for k, v in o["leak_kinds"].items()))

    if m.get("skipped"):
        lines += [f"\n## 未录制（{len(m['skipped'])} 条）\n"]
        for s in m["skipped"]:
            lines.append(f"- `{s['id']}` — {s['reason']}")

    lines += ["\n## 逐条\n",
              "| 场景 | 人格 | depth | 字数 | 软上限 | 报错 | 超软 | 硬截断 | 降级 | 泄漏 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for v in rep["verdicts"]:
        lines.append(
            f"| `{v['scenario_id']}` | {v.get('persona', '')} | {v['depth']} | {v['chars']} |"
            f" {v.get('soft_limit', '')} |"
            f" {'✓' if v['error_reply'] else ''} |"
            f" {'✓' if v['over_soft_limit'] else ''} |"
            f" {'✓' if v['hard_cutoff'] else ''} | {'✓' if v['render_degraded'] else ''} |"
            f" {'、'.join(v['leaks']) or ''} |"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="轴 3 · 回复质量（生成/判定分离）")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", action="store_true", help="跑 /chat 并冻结样本（要联网、要钱）")
    g.add_argument("--judge", metavar="FROZEN_JSON", help="对冻结样本判定（离线、免费）")
    g.add_argument("--list", action="store_true", help="列出已有冻结样本")
    parser.add_argument("--data", default="eval/data/e2e_scenarios.json", help="场景集")
    parser.add_argument("--smoke", type=int, default=0, help="只跑前 N 条（0=全部）")
    parser.add_argument("--offline", action="store_true",
                        help="跳过需要 Bangumi API 工具的场景（本机 api.bgm.tv 不通时用）")
    args = parser.parse_args()

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    if args.list:
        FROZEN_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(FROZEN_DIR.glob("*.json"))
        if not files:
            print(f"（{FROZEN_DIR} 下还没有冻结样本）")
        for f in files:
            meta = json.loads(f.read_text(encoding="utf-8")).get("meta", {})
            print(f"  {f.name}  n={meta.get('n_recorded')}  {meta.get('recorded_at')}"
                  f"  git={meta.get('git_hash')}")
        return

    if args.judge:
        frozen = json.loads(Path(args.judge).read_text(encoding="utf-8"))
        rep = judge(frozen)
        _print_judge(rep)
        jp, mp = _save_judge(rep, Path(args.judge))
        print(f"\n已归档: {jp}\n报告:   {mp}")
        return

    # --record
    data_path = Path(args.data)
    scenarios = json.loads(data_path.read_text(encoding="utf-8"))["scenarios"]
    print(f"加载 {len(scenarios)} 条场景（smoke={args.smoke or '全'}，offline={args.offline}）")

    frozen = asyncio.run(record(scenarios, args.smoke, args.offline, data_path))

    FROZEN_DIR.mkdir(parents=True, exist_ok=True)
    out = FROZEN_DIR / f"reply_quality-{frozen['meta']['run_id']}-{frozen['meta']['git_hash']}.json"
    out.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n冻结样本已写入: {out}")

    # 录完立刻判一遍 —— 生成侧已经付过钱了，判定免费，顺手出数
    rep = judge(frozen)
    _print_judge(rep)
    jp, mp = _save_judge(rep, out)
    print(f"\n已归档: {jp}\n报告:   {mp}")


if __name__ == "__main__":
    main()
