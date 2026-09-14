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
    Tier 2  LLM-as-judge rubric —— 忠实性（建在 eval/reply_quality_judge.py）。
            判据是「回复里的断言能不能在 Agent 当时看到的东西里找到依据」，所以录制
            侧必须顺手把【工具返回原文】冻结进样本 —— 见下方「证据捕获」。
            约束见 eval/README.md「LLM-as-judge 的工程约束」。

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

# 证据捕获的两个上限。工具返回原文是 Tier 2 忠实性核对的【唯一】依据，但原文可能很大
# （检索类工具一次能吐回好几 KB）。截断/丢弃都【显式标记】，判官见了必须按 unknown 处理
# 而不是 unsupported —— 证据没看到 ≠ 模型在编。两个数写进样本 meta，且要进判官缓存键：
# 改了上限就是换了证据，旧判定一律作废。
#
# 2026-09-14 实测：本地检索（search_local_bangumi）一次 limit=10 能吐 30–37k 字符，
# 占满单场景预算 —— 首跑 30 条就有 10 次调用被截断/整条丢弃，所以当天加了录制侧的
# 【卡片化】。同日晚些时候产出侧自己改了索引卡，这两个上限于是**退成纯兜底**：
# 单次返回不可能再撑爆场景预算，命中它们等于有工具越界了。
_EVIDENCE_CALL_CHARS = 20000
# 单场景上限。实测分布：29/30 个场景 ≤ 23510 字，唯一的离群点是 C5-deep必调工具
# （"冷门番"太难，Agent 换了 6 次说法重试检索）59771 字。理论上限 = deep 迭代 6 轮 ×
# 单次卡片约 12k ≈ 72k，所以取 80000 —— 让截断在正常配置下【归零】，而不是天天触发。
# 卡片化后的最坏判官 prompt 约 70k 字 ≈ 11k token，判官吃得下。
_EVIDENCE_TOTAL_CHARS = 80000

# 证据口径版本。进样本 meta，也是 Tier 2 判官缓存键的一部分：换了口径就是换了证据，
# 旧判定一律作废。
#
# card-v1 → index-v1（2026-09-14）。v1 那层卡片（`_CARD_KEEP` / `_card_record`）是给
# 当时的 `search_local_bangumi` 兜体积用的 —— 工具原样吐 20,522 token，录制侧只能削。
# 当天工具改成**产出侧索引卡**（`tools/bgm_tools.py:_local_index_card`：2200 字整块预算
# + `{total, shown, results, note}` 信封）之后，录制侧这一层不但冗余，而且**有害**：
# v1 的保留字段集（有 `_source`/`_next`，没有 `role`/`career`/`entity_type`/`tags`）会把
# 工具刚给出来的角色身份、声优经历、标签再削一遍 —— 判官于是看不到模型明明看到过的字段。
#
# 所以 index-v1 的含义是：**录制侧不再投影任何工具**，盘上存的就是工具真实返回。
# 想改证据形状 = 改工具本身（CLAUDE.md 规则 7），不再是改这个文件。
# 代价：判官的"看不到"清单不再由预注册卡片给出，得由工具自己的 docstring 承担
# （`search_local_bangumi` 的 docstring 里写了 `shown < total` 与卡片字段）。
_EVIDENCE_POLICY = "index-v1"


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
            # 录制时被置 0（见 record()）。留在这里是为了让报告显形：0 = 这一轮录制
            # 没走限流，样本里不会出现"被自家中间件拒掉的空回复"。
            "RATE_LIMIT_PER_MINUTE": s.RATE_LIMIT_PER_MINUTE,
            "LLM_MODEL": s.LLM_MODEL,
            "LLM_TEMPERATURE": s.LLM_TEMPERATURE,
            "EMBEDDING_MODEL": s.EMBEDDING_MODEL,
        }
    except Exception as exc:  # 快照失败不该中断录制
        return {"_error": f"{type(exc).__name__}: {exc}"}


# 只有这些顶层键不算"拿到了数据" —— get_user_profile 无论成败都带着 username 回来
_EVIDENCE_META_KEYS = {"username"}


def _is_error_result(result) -> bool:
    """这次工具调用有没有【拿到数据】。忠实性判官靠它区分「模型在编」和「工具本来
    就没给数据」——两者的 unsupported 含义完全不同。

    两种失败形状都要认：
      · 顶层 ``{"_error": ...}`` —— 绝大多数工具（CLAUDE.md 的约定）
      · **只**剩 ``*_error`` 子键、没有任何实质字段 —— ``get_user_profile`` 这种并行
        取多段的工具，全段失败时就是这样。它看起来像个正常 dict（有 username、
        有若干字段），2026-09-14 首跑就骗过了一次：D2/D6 两轮全 404，却被计成"成功"。

    部分失败（既有数据又有 ``*_error``）**不算失败**：Agent 手上确实有东西可用。
    这种记进 soft_errors，让报告能说清"证据是残缺的"。
    """
    if not isinstance(result, dict):
        return False
    if "_error" in result:
        return True
    if not any(k.endswith("_error") for k in result):
        return False
    return not [k for k in result
                if not k.endswith("_error") and k not in _EVIDENCE_META_KEYS]


def _has_soft_error(result) -> bool:
    """拿到了数据、但有一段挂了（``get_user_profile`` 取 4 段挂了 1 段那种）。"""
    return (isinstance(result, dict) and not _is_error_result(result)
            and any(k.endswith("_error") for k in result))


def _scenario_needs_remote_tools(scen: dict) -> bool:
    """场景是否必须有【Bangumi API 工具】才能跑。

    require_tools 是硬需求；require_any_tools 是"每组任一"，只要组里有本地工具
    就算能跑。forbid_tools 里的 '*' 表示禁调全部工具 —— 那种场景反而最干净。

    ``requires_token`` 单独判：D2/D6 的 require_tools 是空表（它们要的
    get_user_profile / get_user_timeline 是**条件注册**的），只看 require_tools
    会漏判 —— 断网时它们照样跑，然后拿一整轮 _error 回复冒充正常样本。
    """
    if scen.get("requires_token"):
        return True
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


# 调用方的降级出口日志（`main.py:_render_final_reply` / `_render_no_text_followup`）。
# 含义是"用户看到的这段字走的是兜底路径，不是针对这一问的回答"—— 而不是"走了某一条
# 特定分支"。只认其中一条就会漏计另一半。
# 改 main.py 那几句日志文案时同步改这里。
#
# 2026-09-14 口径放宽一格：后两条是"模型一个字都没吐"的收尾，其中「交给人格层交代」
# 那条**是 render 写的**（写的是交代，不是答案）。把它排除在外就等于：修好一个缺陷，
# 指标上反而少了两条失败 —— 失败从"看得见"变成"看不见"。宁可口径宽一点，也不能让
# 失败消失。代价是这张表和旧冻结样本不再严格可比，见 eval/README.md 的同日说明。
_DEGRADE_MARKERS = (
    "降级为清理后的原始文本",  # 非 chat：有可示人的原文，清理后降级
    "chat 分支回落通用话术",  # chat：没有可示人的原文，用写死的兜底
    "模型未收尾",             # 模型没吐文本 → 结局交代（人格层交代 / 走神话术）
    "结局交代渲染失败",        # 上一条的 render 也挂了 → 回落写死的结局话术
)


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
        if any(marker in msg for marker in _DEGRADE_MARKERS):
            self.degraded = True


# ═══════════════════════════════════════════════════════════════════════
# 证据捕获 —— Tier 2（忠实性）的输入
# ═══════════════════════════════════════════════════════════════════════
#
# 忠实性判的是「回复里的断言能不能在【Agent 当时看到的东西】里找到依据」。所以证据
# 必须在【录制时】落盘：事后重放同样的工具调用，拿回来的是今天的 DB 和 API，不是
# 当时那一轮看到的 —— 两者的差异会被记成模型的幻觉，那是测量事故，不是缺陷。
#
# 做法是给工具套一层同名替身。图里装配的仍是 16 个工具，name / description /
# args_schema 一字不差（连给 LLM 看的工具清单都不变），只是每次调用顺手抄一份
# (参数, 返回)。生产代码一行不动 —— agent/graph.py 是模块级单例 build_graph()，
# 只要在 import main 之前把 tools.bgm_tools.get_agent_tools 换成返回替身的版本，
# 图里装的、以及各 pipeline 子图里装的（同一批对象）就都是替身。
#
# 没捕获的东西（写在这里免得将来自作聪明）：L2 记忆召回、L1 压缩后的上下文、
# system prompt。轴 3 的录制给每个场景一个 run-scoped user_id（见下方 isolation），
# 每局开局都是零记忆，所以"凭记忆说的话"这类断言在当前样本里不存在。


class _EvidenceSink:
    """一轮场景的工具调用流水。

    录制是【逐条串行】的（``record()`` 里 for + await），所以模块级单例够用；
    真要并行录制，这里得换成 contextvars。
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.records: list[dict] = []
        self.total_chars = 0

    def add(self, tool: str, args: dict, result, raised: str | None,
            duration_ms: float) -> None:
        rec: dict = {
            "i": len(self.records),
            "tool": tool,
            "args": args,
            "duration_ms": round(duration_ms, 1),
        }
        if raised:
            # 工具本该不抛异常（约定是返回 _error dict）。真抛了也记一笔，然后【原样
            # 上抛】—— 生产路径的 ToolNode(handle_tool_errors=...) 怎么处理，这一轮
            # 就还是怎么处理，替身不改变行为。
            rec["result"], rec["raised"] = None, raised
        else:
            # 不再有任何工具走录制侧投影（policy index-v1）—— 盘上存工具真实返回。
            # `carded` 键仍会被 `_evidence_stats` 统计：card-v1 时代的旧样本带它。
            text = self._as_text(result)
            if self.total_chars + len(text) > _EVIDENCE_TOTAL_CHARS:
                rec["result"] = {"_dropped": True,
                                 "reason": "场景证据总量超上限，这条只留工具名与参数"}
                rec["dropped"] = True
            elif len(text) <= _EVIDENCE_CALL_CHARS:
                rec["result"] = result
                self.total_chars += len(text)
            else:
                rec["result"] = {"_truncated": True, "_chars": len(text),
                                 "text": text[:_EVIDENCE_CALL_CHARS]}
                rec["truncated"] = True
                self.total_chars += _EVIDENCE_CALL_CHARS
        self.records.append(rec)

    @staticmethod
    def _as_text(result) -> str:
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result)


_EVIDENCE = _EvidenceSink()


def _evidence_stats(records: list[dict]) -> dict:
    return {
        "calls": len(records),
        "errors": sum(1 for r in records
                      if r.get("raised") or _is_error_result(r.get("result"))),
        "soft_errors": sum(1 for r in records if _has_soft_error(r.get("result"))),
        "carded": sum(1 for r in records if r.get("carded")),
        "truncated": sum(1 for r in records if r.get("truncated")),
        "dropped": sum(1 for r in records if r.get("dropped")),
    }


def _wrap_tool(tool):
    """造一个同名同 schema 的替身工具，调用时抄一份 (参数, 返回)。"""
    from langchain_core.tools import StructuredTool

    if tool.coroutine is None:
        # 本仓库 16 个工具全是 async。真冒出同步工具，宁可当场炸掉 ——
        # 静默漏掉它的返回，等于让判官拿"证据里没有"去判一句其实有据的话。
        raise RuntimeError(f"工具 {tool.name} 没有 async coroutine，证据捕获挂不上")

    async def _recorded(**kwargs):
        t0 = time.monotonic()
        try:
            result = await tool.coroutine(**kwargs)
        except BaseException as exc:  # noqa: BLE001 —— 记录后原样抛，不吞
            _EVIDENCE.add(tool.name, kwargs, None, f"{type(exc).__name__}: {exc}",
                          (time.monotonic() - t0) * 1000)
            raise
        _EVIDENCE.add(tool.name, kwargs, result, None, (time.monotonic() - t0) * 1000)
        return result

    return StructuredTool.from_function(
        coroutine=_recorded,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


def _install_evidence_capture() -> int:
    """把 ``get_agent_tools`` 换成"返回替身"的版本，返回包装了几个工具。

    ⚠ 必须在 ``from main import app`` 之前调用：``agent/graph.py`` 在模块导入时
    就把 ``get_agent_tools()`` 的结果装进图里了（``agent_app = build_graph()``
    模块级单例）。已经导入过就救不回来 —— 那时图里装的是原工具，静默漏证据比报错
    危险得多，所以这里直接抛。
    """
    if "agent.graph" in sys.modules:
        raise RuntimeError(
            "agent.graph 已导入（图里装的是原工具），证据捕获挂不上："
            "_install_evidence_capture() 必须早于 from main import app。"
        )
    import tools.bgm_tools as bgm_tools

    original = bgm_tools.get_agent_tools

    def _patched() -> list:
        return [_wrap_tool(t) for t in original()]

    bgm_tools.get_agent_tools = _patched
    return len(original())


async def _record_scenario(client, scen: dict, run_id: str, handler: _RenderSignalHandler) -> dict:
    handler.reset()
    _EVIDENCE.reset()
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
        out = {
            "scenario_id": scen["id"], "message": scen["message"],
            "depth": payload["depth"], "output_style": payload["output_style"],
            "expect_intent": scen.get("expect_intent"), "status_code": 0,
            "reply": "", "query_intent": "error", "tools_used": [],
            "latency_ms": 0.0, "render": {"hard_cutoff": False, "degraded": False},
            "error": f"{type(exc).__name__}: {exc}", "telemetry": None,
        }
    else:
        latency_ms = (time.monotonic() - t0) * 1000
        out = {
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

    # 证据在请求返回后收口 —— 此刻这一轮的工具调用已全部结束（render 在 graph 之后，
    # 且 render 不调工具）。请求本身炸了也照样带上：那半轮调过的工具是有信息量的。
    out["evidence"] = list(_EVIDENCE.records)
    out["evidence_meta"] = _evidence_stats(out["evidence"])
    return out


async def record(scenarios: list[dict], smoke: int, offline: bool, scenario_file: Path) -> dict:
    from core.config import get_settings
    settings = get_settings()
    settings.DEV_MODE = True  # 拿 telemetry
    # ★ 关掉应用自己的 IP 限流（middleware.rate_limit_middleware，默认 10 次/分钟）。
    # 录制是一条接一条打 /chat，跑得比 10 次/分钟快就会被自家中间件拒；被拒的那条
    # 会【当成正常样本冻结下来】（status 429 + 空回复），事后和有效样本长得一模一样。
    # 2026-09-14 实测栽了两次：00:35 和 00:40 两轮各只有 10/30 条成功，第 11 条起全 429。
    # 关掉它是诚实的：ASGITransport 是进程内调用，限流防的是外部刷 LLM 成本，防不到自己。
    # 这个偏离会进 settings_snapshot（见 _settings_snapshot），报告里看得见。
    settings.RATE_LIMIT_PER_MINUTE = 0

    # ★ 必须在 import main 之前 —— agent/graph.py 在模块导入时就 build_graph() 了
    n_tools = _install_evidence_capture()

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
            sample = await _record_scenario(client, scen, run_id, handler)
            samples.append(sample)
            ev = sample.get("evidence_meta") or {}
            print(f"  录制 {i + 1}/{len(to_run)}: {scen['id']}"
                  f"  (证据 {ev.get('calls', 0)} 次，失败 {ev.get('errors', 0)}，"
                  f"卡片 {ev.get('carded', 0)}，截断/丢弃 "
                  f"{ev.get('truncated', 0) + ev.get('dropped', 0)})")
            # ★ 失败即中止，绝不把废样本写进 frozen/。
            # 被限流/出错的样本长得和有效样本【一模一样】：status_code 换成 429、
            # reply 空、evidence 空，除此之外全是正常字段。留着它，将来有人直接拿去跑
            # Tier 2，测的就是一堆积压的拒绝。宁可这轮白跑，不可留废样本。
            status = sample.get("status_code")
            if status != 200:
                raise RuntimeError(
                    f"录制中止：场景 {scen['id']} 返回 HTTP {status}（已成功 {i} 条）。"
                    f"冻结样本【未写出】—— 废样本与有效样本外形一致，留着比没有更危险。"
                    f"若 status=429 请查 middleware.RATE_LIMIT_PER_MINUTE；"
                    f"其它状态码看服务端日志。"
                )

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
            "evidence_capture": {
                "enabled": True,
                "n_tools_wrapped": n_tools,
                "policy": _EVIDENCE_POLICY,
                "per_call_chars": _EVIDENCE_CALL_CHARS,
                "per_scenario_chars": _EVIDENCE_TOTAL_CHARS,
                "what": "工具返回（失败的 _error 返回与抛出的异常也在内），逐条样本【原样】冻结在 "
                        "evidence 里 —— index-v1 起录制侧不投影任何工具。检索类工具的体积由"
                        "产出侧自己兜（`search_local_bangumi` 返回索引卡，≤2200 字 + "
                        "{total, shown, results, note} 信封），故判官看到的 = 工具当时给出的",
                "not_captured": "L2 记忆召回 / L1 压缩后的上下文 / system prompt —— "
                                "本字段是【工具原始返回】，模型在后续轮次看到的是它被 L1 "
                                "截断/压缩后的样子；轴 3 单轮录制里两者通常一致",
                "cache_note": "policy + 两个上限进 Tier 2 判官缓存键：改任何一项＝换证据，"
                              "旧判定作废",
            },
            "known_limits": [
                "LLM 非确定（RENDER_TEMPERATURE=0.4, LLM_TEMPERATURE 见快照）→ 看分布与 delta，不看单条",
                "场景真实调用 Bangumi API（离线环境下工具的 _error 会进回复）",
                "Tier 1 只判格式；内容质量（忠实性）由 eval/reply_quality_judge.py 读本样本的 evidence 判",
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
    # ★ 回归哨兵，别删。2026-09-13 首跑轴 3 时抓到过一次真实泄漏（样本 B6-闲聊回应）：
    # render 失败 → 降级返回 _degrade_render_input(render_input)，而 chat 分支的
    # render_input 里写着【给模型看的指令】（main.py:_render_final_reply）：
    #   用户对你说：<用户原话>\n\n这是一段闲聊。自然地用你的角色性格回复。不要列数据、
    #   不要提搜索、就像朋友聊天一样。
    # _degrade_render_input 只清 emoji/markdown，不清这些脚手架 —— 于是内部提示词
    # 被原样吐给用户。这比 emoji 泄漏严重得多，单独成一类。
    #
    # 已修（main.py 把可示人文本 fallback 与喂模型的 render_input 拆开，chat 分支
    # 没有可降级的原文 → 回落 render.py 的兜底话术）。模式保留：修好了才更要留哨兵，
    # 它是唯一能证明"没复发"的东西。旧冻结样本里那条泄漏原文原封不动留着，
    # 重录前报告里的 any_leak=1 是历史事实，不是当前状态。
    "prompt脚手架": re.compile(
        r"用户对你说：|这是一段闲聊|不要列数据|不要提搜索|就像朋友聊天一样"
    ),
}

# 报错回复的特征（graph_smoke.py 同款判定）
# main.py `_extract_final_reply` 里那几条 canned 兜底回复的字面量。它们既不是
# 空串、也不以"抱歉"开头，所以不列在这里就会当成正常回复通过全部六个勾 ——
# A5-deep深入 的 27 字 "工具执行完成但未能生成文本回复" 就是这么漏掉的：
# 勾全绿，而那一轮其实是彻底的失败。改 main.py 的兜底文案时同步改这里。
# （不 import main：那会把 FastAPI app 整个拽进来，判定侧要的是纯函数。）
#
# 2026-09-14 起三句都成了哨兵：那条"有工具结果却没有 AI 文本"的路已不再返回 canned
# 文案，改由 `_render_no_text_followup` 给回复（降级口径见 `_DEGRADE_MARKERS`）。
# 一条都不删 —— 修好了才更要留，它们是"罐头话没回来"的唯一证据。
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

    # 证据统计只做搬运，不在 Tier 1 里下结论 —— 它的用处是 Tier 2 的闸门与分层
    # （"无可用证据"的回复，忠实性判出来的 unsupported 含义完全不同）。
    # 旧样本（2026-09-13 之前录的）没有 evidence 字段，evidence_present=False，
    # 报告里那一整块不显示，免得把"没捕获"显示成"没有证据"。
    ev = sample.get("evidence")
    ev_present = isinstance(ev, list)
    ev = ev or []
    ev_errors = sum(1 for r in ev
                    if r.get("raised") or _is_error_result(r.get("result")))
    ev_soft = sum(1 for r in ev if _has_soft_error(r.get("result")))

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
        # 键名是历史遗留（原名只数 render 挂掉那两种）；口径见 `_DEGRADE_MARKERS`，
        # 报出去的一律叫「降级回复」。
        "render_degraded": bool(render.get("degraded")),
        "leaks": leaks,
        "latency_ms": sample.get("latency_ms"),
        "tools_used": sample.get("tools_used") or [],
        "query_intent": sample.get("query_intent"),
        "evidence_present": ev_present,
        "evidence_calls": len(ev),
        "evidence_errors": ev_errors,
        "evidence_soft_errors": ev_soft,
        # 卡片化【不是】残缺：它是按预注册规则做的投影，判官据此把"依赖未列字段的断言"
        # 判 unknown 而不是 unsupported。截断/丢弃才是真的残缺。
        "evidence_carded": sum(1 for r in ev if r.get("carded")),
        "evidence_partial": sum(1 for r in ev if r.get("truncated") or r.get("dropped")),
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
            "evidence_present": any(r["evidence_present"] for r in rows),
            # 「没调工具」和「调了但全失败」是两回事，混在一起报等于把 chat 场景
            # 判成故障。前者是设计（禁工具/闲聊），后者才是警报。
            "no_tool_calls": sum(1 for r in rows
                                 if r["evidence_present"] and not r["evidence_calls"]),
            "all_calls_failed": sum(
                1 for r in rows
                if r["evidence_present"] and r["evidence_calls"]
                and r["evidence_calls"] == r["evidence_errors"]
            ),
            "evidence_calls": sum(r["evidence_calls"] for r in rows),
            "evidence_errors": sum(r["evidence_errors"] for r in rows),
            "evidence_soft_errors": sum(r["evidence_soft_errors"] for r in rows),
            "evidence_carded": sum(r["evidence_carded"] for r in rows),
            "evidence_partial": sum(r["evidence_partial"] for r in rows),
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
    row("降级回复", "render_degraded")
    row("超软上限", "over_soft_limit")
    row("超硬截断上界", "over_hard_limit")
    row("有任何格式泄漏", "any_leak")
    if o.get("evidence_present"):
        row("未调工具", "no_tool_calls")
        row("调了但全失败", "all_calls_failed")

    print()
    print(f"  字数 p50/p95/max: {o['chars_p50']} / {o['chars_p95']} / {o['chars_max']}")
    print(f"  软上限(prompt建议)={lim['soft_prompt_suggested']}  硬截断上界={lim['hard_cutoff']}")
    if o["leak_kinds"]:
        print(f"  泄漏明细: " + "、".join(f"{k}×{v}" for k, v in o["leak_kinds"].items()))
    if o.get("evidence_present"):
        print(f"  证据: 工具调用 {o['evidence_calls']} 次（失败 {o['evidence_errors']}，"
              f"部分失败 {o['evidence_soft_errors']}），卡片化 {o['evidence_carded']} 条，"
              f"截断/丢弃 {o['evidence_partial']} 条  ← Tier 2 忠实性核对的输入")

    print()
    print("  读法:")
    print("    · 「超硬截断上界」本该恒为 0（render 会截）；不是 0 就说明有回复走了")
    print("      降级路径 —— 那条路不过硬截断。它是【能真超上界】的唯一入口。")
    print("    · 「硬截断触发」≠ 违规：prompt 建议字数低于截断线（fast 200<280），")
    print("      中间这段是缓冲。但它意味着回复被砍过，可能不完整。")
    print("    · 字数对硬截断的【合规率】恒 100%，所以这里不报合规率 —— 报了也没信息量。")
    print("    · 「未调工具」与「调了但全失败」分开报，别混：前者是设计（禁工具/闲聊场景），")
    print("      后者才是故障。两者都是 Tier 2 的分层轴 —— 那批上判出的 unsupported")
    print("      含义与「有据不用」完全不同。")
    print("    · 「卡片化」不是残缺，是预注册的证据投影；index-v1 起录制侧不再投影，")
    print("      于是恒为 0 —— 检索类工具的体积已由产出侧自己兜。截断/丢弃才是真的残缺。")


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
                       ("硬截断触发", "hard_cutoff"), ("降级回复", "render_degraded"),
                       ("超软上限", "over_soft_limit"), ("超硬截断上界", "over_hard_limit"),
                       ("有任何格式泄漏", "any_leak")]:
        lines.append(mrow(label, key))
    if o.get("evidence_present"):
        lines.append(mrow("未调工具", "no_tool_calls"))
        lines.append(mrow("调了但全失败", "all_calls_failed"))

    lines += [
        f"\n**字数** p50={o['chars_p50']} p95={o['chars_p95']} max={o['chars_max']}"
        f"（软上限 {rep['limits']['soft_prompt_suggested']}，硬截断 {rep['limits']['hard_cutoff']}）",
    ]
    if o["leak_kinds"]:
        lines.append("\n**泄漏明细**：" + "、".join(f"{k}×{v}" for k, v in o["leak_kinds"].items()))
    if o.get("evidence_present"):
        cap = (m.get("evidence_capture") or {})
        lines.append(
            f"\n**证据**（Tier 2 忠实性核对的输入）：工具调用 {o['evidence_calls']} 次"
            f"（失败 {o['evidence_errors']}、部分失败 {o['evidence_soft_errors']}），"
            f"卡片化 {o['evidence_carded']} 条，截断/丢弃 {o['evidence_partial']} 条。"
            f"「未调工具」是设计（禁工具/闲聊场景），「调了但全失败」才是故障 —— "
            f"两者上判出的 unsupported 含义都与「有据不用」不同。"
        )
        if cap:
            card = cap.get("card") or {}
            if card:
                lines.append(
                    f"\n**证据策略** `{cap.get('policy')}`：检索类工具（"
                    f"{'、'.join('`' + t + '`' for t in cap.get('carded_tools', []))}）"
                    f"按预注册卡片投影 —— 保留 {', '.join('`' + k + '`' for k in card.get('keep', []))}，"
                    f"简介封顶 {card.get('summary_chars')} 字，infobox 前 {card.get('infobox_keys')} 键"
                    f"（单值 ≤{card.get('infobox_value_chars')} 字），标签只留 name。"
                    f"未列出的内容（{'；'.join(card.get('omitted', []))}）判官【看不到】，"
                    f"依赖它们的断言必须判 unknown，不得判 unsupported。"
                    f"单次上限 {cap.get('per_call_chars')} 字、单场景 {cap.get('per_scenario_chars')} 字。"
                )
            else:
                lines.append(
                    f"\n**证据策略** `{cap.get('policy')}`：**录制侧不投影**，盘上存的是工具"
                    f"【原样】返回。检索类工具的体积由产出侧自己兜（`search_local_bangumi` "
                    f"返回索引卡：≤2200 字 + `{{total, shown, results, note}}` 信封；"
                    f"`shown < total` 表示只给了前若干条）。"
                    f"单次上限 {cap.get('per_call_chars')} 字、单场景 "
                    f"{cap.get('per_scenario_chars')} 字 —— index-v1 起这两个数退成兜底，"
                    f"命中即说明有工具越界。"
                )

    if m.get("skipped"):
        lines += [f"\n## 未录制（{len(m['skipped'])} 条）\n"]
        for s in m["skipped"]:
            lines.append(f"- `{s['id']}` — {s['reason']}")

    has_ev = bool(o.get("evidence_present"))
    ev_head = " 证据(调用/失败/部分) |" if has_ev else ""
    lines += ["\n## 逐条\n",
              f"| 场景 | 人格 | depth | 字数 | 软上限 | 报错 | 超软 | 硬截断 | 降级 | 泄漏 |{ev_head}",
              "|---|---|---|---|---|---|---|---|---|---|" + ("---|" if has_ev else "")]
    for v in rep["verdicts"]:
        ev_cell = (f" {v.get('evidence_calls', 0)}/{v.get('evidence_errors', 0)}"
                   f"/{v.get('evidence_soft_errors', 0)} |" if has_ev else "")
        lines.append(
            f"| `{v['scenario_id']}` | {v.get('persona', '')} | {v['depth']} | {v['chars']} |"
            f" {v.get('soft_limit', '')} |"
            f" {'✓' if v['error_reply'] else ''} |"
            f" {'✓' if v['over_soft_limit'] else ''} |"
            f" {'✓' if v['hard_cutoff'] else ''} | {'✓' if v['render_degraded'] else ''} |"
            f" {'、'.join(v['leaks']) or ''} |{ev_cell}"
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
    g.add_argument("--tier2-dry-run", metavar="FROZEN_JSON",
                   help="Tier 2 证据投影 dry-run：只打印判官将看到多少字，不调 LLM、不花钱")
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

    if args.tier2_dry_run:
        from eval import reply_quality_judge as t2
        t2.print_dry_run(t2.dry_run_report(t2.load_frozen(args.tier2_dry_run)))
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
