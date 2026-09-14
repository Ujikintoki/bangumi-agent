"""轴 3 Tier 2 · 忠实性判官 —— 模型视角证据 + 两段式核对

判什么
    回复里的「可核查的具体断言」，能不能在【Agent 当时实际读到的工具返回】里找到依据。
    三值：supported / unsupported / unknown。吐槽、观点、寒暄不进分母。

为什么要「模型视角」这一层
    判官的唯一依据是冻结样本里的证据，而**判官看到的必须等于模型当时看到的** ——
    多一个字、少一个字，指标都会歪：

    · 序列化对齐。LangChain 把工具返回的 dict 变成 ``ToolMessage.content`` 走的是
      ``langchain_core.tools.base._stringify``，即 ``json.dumps(content, ensure_ascii=False)``。
      2026-09-14 拿 46 条真实证据逐条对账，**byte 级 46/46 一致**。这里必须走同一条
      序列化，否则「字段在不在」都可能对不上。
    · 截断对齐。超长的工具返回会被 L1 ``_truncate_oversized_messages`` 掐到
      ``_MAX_SINGLE_MESSAGE_TOKENS``（**弃尾保头**，尾部补「...[内容已截断]」）。
      新样本 46 条证据里 **12 条超限**（``search_bangumi_subject`` 8 条、
      ``get_subject_opinions`` 4 条 —— 正是 ROADMAP P1 挂着的两个）。判官若看完整版，
      模型**没**看到的内容会被算成"有据"，忠实性被系统性高估、unsupported 率退化成
      下界。所以这里跑一遍生产同款 ``_truncate_message_content``。

**截断不再产生 unknown（2026-09-14 订正，与 `41a4770` 的口径相反）**
    `41a4770` 曾假设「判官看完整版、模型看截断版」，于是立了条硬规矩：裁掉的内容必须判
    ``unknown``。**这个前提是错的。** ``manage_memory`` 在**每一轮** LLM 调用之前都跑
    （``agent/nodes/reasoning.py:152``、``pipeline.py:74``），``_truncate_oversized_messages``
    在工具结果进入模型上下文**之前**就砍掉了尾部 —— 模型从来没有见过完整版。

    既然 ``model_view`` 复现了同一条截断，**判官视角 ≡ 模型视角**，两侧完全对齐：
    证据里没有 ⇒ 模型当时手上也没有 ⇒ **就是 unsupported**。把截断算成 unknown 会凭空
    制造一批"判不了"，把分母挖空。

    实测印证：判官拿到旧 rubric 后**拒绝执行**那条规矩（A5 有 3 条截断证据，仍把 9 条
    无据断言全判 unsupported）—— 模型比 rubric 讲道理。这里把规矩改对，别让"判得对"
    依赖模型的临场判断。

    所以 ``unknown`` 现在只剩两种来源，门槛很高：证据**整块**缺失（录制侧 ``_EVIDENCE_*``
    上限丢弃了某次调用，新样本为 0）与断言本身无从核对。截断/失败的计数仍然照打 ——
    它们是**证据完整度**的读数（模型当时拿到的上下文有多残），只是不再决定三值。

两段式（切句 → 核对）
    一、切句：LLM 把回复切成「可核查的具体断言」，**先落盘再核对**。分母因此可审计 ——
        步骤 4 的 κ 校准要人工勾的就是这批断言。
    二、核对：逐条三值，且**判 supported 必须报出处**（照抄证据里的原文片段）。
        报不出出处的不算 supported（降级 unknown 并单独计数，见 ``_parse_checks``）。

    两段的缓存键都含 rubric 指纹 + 证据指纹 —— 改 rubric 或换样本，旧判定自动失效。

指标
    主：断言级 ``unsupported / (supported + unsupported)``，**unknown 不进分母**。
    次：有 unsupported 断言的场景占比。
    单列：**零证据场景**（Agent 一次工具都没调）—— 那里的事实断言无从核起，不进
        任何分母，只报条数（``no_evidence``）。「调了但全失败」不单列：它的证据块全是
        「失败」，按规矩 ③ 自然全判 unknown，不会污染主指标。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from openai import OpenAI

from agent.memory.short_term import (
    _MAX_SINGLE_MESSAGE_TOKENS,
    _TRUNCATION_MARKER,
    _truncate_message_content,
    count_tokens,
)
from eval.generate_rag_gt import USAGE, call_llm

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CACHE_FILE = Path(__file__).resolve().parent / "data" / "reply_quality_judge_cache.jsonl"

# 判官口径版本。进缓存键：改了 rubric 或证据口径，旧判定一律作废重判。
JUDGE_VERSION = "faithfulness-v1"

# 温度 0 是刻意的：这是判定不是生成，两次跑出两个数就没法当测量用了。
# max_tokens 4096：JSON mode 下被截断的返回是**空串**（不报错、静默丢光整批），
# D 组的实测教训（2048 判一批会烧穿成空）。思考全项目常关。
JUDGE_TEMPERATURE = 0.0
JUDGE_MAX_TOKENS = 4096
SPLIT_MAX_TOKENS = 2048

# 判不出来就二分再判，而不是原样重试（原样重试只会原样失败）。见 D 组 _judge_batch。
JUDGE_MIN_SPLIT = 2

# 单场景断言数上限。回复硬截断在 480 字，正常切不出这么多；设它是防模型抽风把
# 一段修辞切成一堆碎片，把分母撑爆。
MAX_ASSERTIONS = 15


# ═══════════════════════════════════════════════════════════════════════
# 模型视角证据
# ═══════════════════════════════════════════════════════════════════════


def serialize(result: Any) -> str:
    """工具返回 dict → 模型当时读到的字符串。

    与 ``langchain_core.tools.base._stringify`` 同一条路（实测 46/46 byte 一致）。
    ``default=str`` 只是防御：冻结样本是 JSON round-trip 过的，不会真的走到。
    """
    return json.dumps(result, ensure_ascii=False, default=str)


def model_view(result: Any) -> tuple[str, bool]:
    """工具返回 → 模型当时【实际读到】的那段文本，含生产同款截断。

    Returns:
        (文本, 是否被截断)。截断过的文本以「...[内容已截断]」收尾。
    """
    text = serialize(result)
    cut = _truncate_message_content(
        ToolMessage(content=text, tool_call_id="tier2", name="evidence"),
        _MAX_SINGLE_MESSAGE_TOKENS,
    )
    return cut.content, cut.content != text


def _is_failed(result: Any) -> bool:
    """这次工具调用有没有拿到数据（与 ``reply_quality_eval._is_error_result`` 同义）。

    判官要能区分「模型在编」和「工具本来就没给数据」。两处形状都要认：顶层
    ``_error``，以及只剩 ``*_error`` 子键、没有任何实质字段（``get_user_profile``
    全段失败时就是这样 —— 它看着像个正常 dict，2026-09-14 首跑骗过一次）。
    """
    if not isinstance(result, dict):
        return False
    if "_error" in result:
        return True
    if not any(k.endswith("_error") for k in result):
        return False
    return not [k for k in result if not k.endswith("_error") and k != "username"]


def build_evidence(sample: dict) -> tuple[str, dict]:
    """把一个场景的证据拼成判官 prompt 里的正文，返回 (文本, 统计)。

    每个证据块头部标状态 —— **判官靠它决定 unsupported 还是 unknown**，
    所以状态必须是实测出来的，不能默认「完整」。
    """
    ev = sample.get("evidence") or []
    stats = {"calls": 0, "complete": 0, "truncated": 0, "failed": 0,
             "chars": 0, "tokens": 0, "max_tokens": 0}
    blocks: list[str] = []
    for n, e in enumerate(ev, 1):
        stats["calls"] += 1
        if _is_failed(e.get("result")):
            stats["failed"] += 1
            body = serialize(e.get("result"))
            head = (f"### 证据 {n} · {e.get('tool')} · 失败"
                    f"（这次调用没拿到数据；模型当时也没有）")
        else:
            body, truncated = model_view(e.get("result"))
            if truncated:
                stats["truncated"] += 1
                head = (f"### 证据 {n} · {e.get('tool')} · 已截断"
                        f"（太长，尾部被砍掉，末尾标了 {_TRUNCATION_MARKER!r}；"
                        f"**模型当时看到的也是这一份**）")
            else:
                stats["complete"] += 1
                head = f"### 证据 {n} · {e.get('tool')} · 完整"
        tok = count_tokens(body)
        stats["chars"] += len(body)
        stats["tokens"] += tok
        stats["max_tokens"] = max(stats["max_tokens"], tok)
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks), stats


def evidence_digest(evidence_text: str) -> str:
    """证据指纹 —— 进核对阶段的缓存键。样本重录/换口径，旧判定自动失效。"""
    return hashlib.sha1(evidence_text.encode("utf-8")).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════════════
# rubric —— 两段各一段，都是判官口径本身，改动必须进缓存键
# ═══════════════════════════════════════════════════════════════════════


_SPLIT_RUBRIC = """你是「断言切分器」。任务：把一条 AI 助手的回复切成【可核查的具体断言】，
**不做任何判定** —— 对不对不归你管。

只输出 JSON：{"assertions": [{"i": 0, "text": "..."}, {"i": 1, "text": "..."}]}

【要收】—— 关于现实世界、有真假可言的事实陈述：
  · 作品 / 角色 / 人物 / 会社 / 制作组的名称、身份、彼此关系
  · 年份、集数、评分、排名、销量、时长 —— 任何数字
  · 剧情、设定、班底、声优、标签、口碑
  · 「某作品属于某系列」「某人参与过某作品」这类关系

【不要收】—— 没有真假可言的：
  · 主观评价与吐槽：「这部过誉了」「我更喜欢另一部」「太离谱了」
  · 对用户的寒暄、反问、追问、提议：「要不要我帮你查查？」
  · 元话语：「我查了一下」「工具返回了 10 条」「资料里没写」
    ★ 例外：元话语本身在陈述一个事实时算断言（「站里有 10 条相关条目」）。
  · 纯修辞、感叹、语气词、称呼

【切法】
  · 一条只放**一个**事实。「A 是 B 的导演，1995 年播出」要切成两条。
  · 必须**自足**：把「它」「这部」「老师」换成具体名字，让断言脱离回复也读得懂。
    回复说「新房昭之执导的这部」，断言写成「《化物语》由新房昭之执导」。
  · 用回复**自己的措辞**，不改写、不补充回复没说过的信息。
  · 名字、数字、年份原样保留。
  · 回复把话反着说（「我一直以为是 2010 年，其实是 2009 年」）时，只取它**最终断言**
    的那个事实（2009 年）。
  · 疑问句里的事实前提算断言（「这部是 2009 年的吧？」→「这部是 2009 年的」）。
  · 切不出任何断言是**正常结果**，输出 {"assertions": []}，不要硬凑。
  · 最多 15 条；超了优先保留含具体名字/数字的。

不要输出 JSON 以外的任何内容。"""


# ⚠ 下面两个 rubric 字符串**就是发给模型的 prompt**，进 `_rubric_fingerprint()`。
# 给维护者看的注解一律写成 `#` 注释放在字符串**外面** —— 往字符串里加一个字（哪怕
# 只是备注）都会改指纹、让全部缓存判定作废、白花钱重判一遍。2026-09-14 踩过一次。
#
# ★ 「作品解读照判 unsupported」是**用户 2026-09-14 拍板的口径**（三个选项里选的
#   「算，保持现状」），不是疏漏：本轴量的是「回复里有多少内容是从检索来的」。
#   别把它"修"成把解读排除出分母。
_CHECK_RUBRIC = """你是「忠实性核对员」。给你【AI 助手当时看到的工具返回】和【从它回复里切出来的
断言】，逐条判断这条断言有没有依据。

**你不许使用自己的百科知识。** 你只知道下面给出的证据 —— 哪怕你确信某句话是对的，
证据里没有它就是没有依据。

**关键前提：下面这份证据就是模型当时实际读到的原文。** 超长的工具返回在进入模型上下文
【之前】就被砍掉了尾部（末尾标着「...[内容已截断]」），你手里是同一份砍过的。所以
「你没看到」就等于「模型当时也没看到」—— 两边完全对齐，**不存在「判官看不到但模型
看得到」的盲区**。这一点决定了 unknown 的门槛，别搞反。

三值：

  supported   证据里能直接找到依据。**必须在 cite 里逐字引用证据中的片段**（照抄，
              不要改写）。报不出出处就不算 supported。
  unsupported 证据里找不到依据 —— **模型当时手上就没有它**，这句是凭模型自己的记忆
              或推测写出来的。
  unknown     你**自己判不了**。门槛很高，见规则 3。

判定规则，**按顺序**：
  1. 在证据里找依据。找到 → supported（附 cite）。
     ★ **「找到依据」不等于「证据里有这句话的原文」。** 下面三种都算找到：
     · **从字段值直接推出来的** —— 证据写 "name": "碧蓝之海 第三季"，断言说「是续作」，
       算（「第三季」这个词本身就是续作的意思）。但**断言不能比证据强**：标签只写
       「恋爱」而断言说「纯爱天花板」，不算。
     · **对证据里的数字做算术** —— 断言说「8分和9分两档占了近三分之二」，证据给了各档
       人数、加起来除以总数对得上，算。cite 里把参与计算的那几个数字都引上。
     · **标签/字段的同义改写** —— 证据 tags 里有「治愈」，断言说「温柔向」，算。
       同义指**意思相当**（治愈≈温柔、日常≈轻松），不是沾边。
     上面三种都**指得出依据在哪**，所以 cite 照填 —— 引的是依据，不是那条断言的原文。
  2. 找不到 → unsupported。
     ★ **不要**因为「有证据块被截断或失败」就改判 unknown。被砍掉的尾部模型也没看到，
       失败的那次调用模型也没拿到数据 —— 这两种情况下「证据里没有」直接等于「模型
       没有」，照样是 unsupported。
  3. 只有这两种情形才判 unknown：
     · 本条断言依赖的证据**整块不在**（回复明显在引用某次检索，而证据里根本没有那次
       调用的返回）—— 那是录制侧的缺口，不能算在模型头上。
     · 断言本身读不懂、指代不清、或自相矛盾，无从核对。
  4. 否定式断言（「没有」「不是」「搜不到」）：证据里明确写了相反或缺失的事实才算
     supported（例如返回 {"results": [], "total": 0} 支持「没搜到」）。
  5. **工具自述**（单独归类，**不改判定值**）：断言在陈述【工具本身是什么 / 能做什么 /
     不能做什么】时（「我手上只有趋势榜」「这个榜单反映的是热度，不是评分涨幅」
     「工具只返回了前 10 条」）—— 照常按上面 1–4 判，另外把 `tool_self` 打 true。
     这类通常判 unsupported（工具返回里不描述自己，模型是从工具定义知道的 —— 那是
     另一条信息通路，判官看不到）；但**证据里如果就有**（例如信封写着
     shown=10 / total=91，断言说「只返回了前 10 条」），照样判 supported。
     报告会把这一类单列。

【判 unsupported 前先自问】
  · 这条断言要的信息，**当时的工具返回本来会带吗**？工具只给了索引卡（名字/年份/
    评分/标签/一句话简介），断言却在讲具体班底 —— 那就没有依据 → unsupported。
  · ★ 断言是**主观解读**时（「这部作品想问的是…」「它的内核是…」「导演真正想拍的是…」）
    —— 照样判 unsupported。作品解读也是关于作品的陈述，证据里没有依据就是模型自己
    带的。**不要**因为听起来像"观点"就放过它（纯口味表态如「我个人更喜欢这部」不会
    送到你这里，切句阶段已经滤掉了；能送到你这里的都是被判定为可核查的陈述）。
  · 断言里的**每个**数字和名字都要核到。核不到任何一个 → 至少不是 supported。
  · 同一个东西可能以简称、别称、日文原名、中文译名出现（「EVA」/「新世纪福音战士」/
    「新世紀エヴァンゲリオン」是同一个；回复说「巨人」而证据里只有「進撃の巨人」）——
    算找到依据。

【特别注意】
  · 证据里**任何**一条记录、**任何**一个字段都能当依据，不限于回复点名的那条。
  · `search_bangumi_subject` 被截断时砍掉的是**列表尾部**（更靠后的候选条目）——
    那也是模型没看到的。别把「列表里没有」读成「可能在被砍掉的部分里」。
  · `search_local_bangumi` 的返回带信封 {total, shown, note}：shown < total 表示工具
    只返回了前若干条候选。同上，模型当时也只看到这些。
  · total / shown / note / _error 本身就是数据，可以当依据引用。

输出 JSON：
{"judgments": [{"i": 0, "v": "supported", "why": "≤30字理由",
                "cite": "证据里的原文片段；supported 必填，其余留空",
                "tool_self": false}]}

`tool_self` 是布尔值，只在断言属于规则 5 时填 true（**不影响 v**）。
必须覆盖给出的每一条断言，i 用断言编号。不要输出 JSON 以外的任何内容。"""


def _rubric_fingerprint() -> str:
    """判官【核对】口径指纹 —— 进缓存键 + 进报告 meta。改一个字，旧判定全部失效。

    只含 `_CHECK_RUBRIC`。**不要再把 `_SPLIT_RUBRIC` 混进来**（2026-09-14 踩过）：
    切句那种事只由 `_SPLIT_RUBRIC` 决定，核对口径改一个字不该把切句结果也作废 ——
    切句一重跑，第 k 条断言就不是原来那条了，而**人工标注按 (场景, 序号) 存档**，
    于是人勾的答案会悄悄对到别的断言上。第一版就是这么错的：改 rubric 重判后
    39 条里 19 条错位，κ 反而从 0.683 涨到 0.748 —— 涨的是错位的功劳，不是修得好。

    证据口径（样本 meta 的 ``policy``）**不在这里**，它在 ``_cache_key`` 里从样本读 ——
    故意不 import ``reply_quality_eval``：那会形成环（对方在 ``main()`` 里 import 本模块），
    而且是把两层耦合起来。判官只需要知道"盘上这份样本是哪个 policy 录的"。
    """
    h = hashlib.sha1(_CHECK_RUBRIC.encode("utf-8")).hexdigest()[:8]
    return f"{JUDGE_VERSION}|cap={_MAX_SINGLE_MESSAGE_TOKENS}|{h}"


def _split_fingerprint() -> str:
    """判官【切句】口径指纹 —— 只含 `_SPLIT_RUBRIC`，与核对口径解耦。

    见 `_rubric_fingerprint` 的注释：这两个指纹分开，是为了「改核对口径重判」时
    切句结果原样复用 —— 断言的身份（场景 + 序号 + 文本）不漂，人工标注就还有效。
    """
    h = hashlib.sha1(_SPLIT_RUBRIC.encode("utf-8")).hexdigest()[:8]
    return f"{JUDGE_VERSION}|split|cap={_MAX_SINGLE_MESSAGE_TOKENS}|{h}"


# ═══════════════════════════════════════════════════════════════════════
# 缓存（append-only，一行一条）
# ═══════════════════════════════════════════════════════════════════════


def _cache_key(stage: str, sample_meta: dict, payload: str) -> str:
    """两段的指纹各管各的 —— 切句的键不许被核对口径的改动带崩（见 `_rubric_fingerprint`）。"""
    fp = _split_fingerprint() if stage == "split" else _rubric_fingerprint()
    schema = fp + "|" + str((sample_meta or {}).get("policy", "?"))
    return hashlib.sha1(f"{schema}\n{stage}\n{payload}".encode("utf-8")).hexdigest()


def load_cache() -> dict[str, dict]:
    """读回已判定的结果（键 → 记录）。文件不存在就是空缓存，不是错误。"""
    cache: dict[str, dict] = {}
    if not CACHE_FILE.exists():
        return cache
    for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("k"):
            cache[rec["k"]] = rec
    return cache


def append_cache(rec: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ═══════════════════════════════════════════════════════════════════════
# 第一段 · 切句
# ═══════════════════════════════════════════════════════════════════════


def _split_prompt(message: str, reply: str) -> str:
    return (f"用户问的是：\n{message}\n\n"
            f"助手回复：\n{reply}\n\n"
            f"把这条回复切成可核查的具体断言。")


def _parse_assertions(raw: dict) -> list[str] | None:
    """解析切句结果。认不出来返回 None（调用方据此二分/放弃）。"""
    items = raw.get("assertions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return None
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            text = it.strip()
        elif isinstance(it, dict):
            text = str(it.get("text") or "").strip()
        else:
            continue
        if text:
            out.append(text)
    return out[:MAX_ASSERTIONS]


def split_assertions(client: OpenAI, model: str, message: str, reply: str,
                     n_retry: int = 1) -> list[str]:
    """回复 → 可核查断言列表。切不出来返回空列表（那是合法结果，不是失败）。"""
    raw = call_llm(client, model, _SPLIT_RUBRIC, _split_prompt(message, reply),
                   temperature=JUDGE_TEMPERATURE, max_tokens=SPLIT_MAX_TOKENS,
                   n_retry=n_retry)
    return _parse_assertions(raw) or []


# ═══════════════════════════════════════════════════════════════════════
# 第二段 · 三值核对
# ═══════════════════════════════════════════════════════════════════════


def _check_prompt(evidence_text: str, assertions: list[tuple[int, str]]) -> str:
    lines = ["【证据】", evidence_text or "（本场景 Agent 没有调用任何工具，没有证据。）",
             "", "【待核对的断言】"]
    for i, text in assertions:
        lines.append(f"{i}. {text}")
    return "\n".join(lines)


def _parse_checks(raw: dict) -> tuple[dict[int, dict], int]:
    """解析核对结果，返回 ({断言编号: {v, why, cite}}, 无出处的 supported 数)。

    判官报了 supported 却没给 cite —— 按硬规矩「报不出出处不算 supported」降级成
    unknown。这样主指标（unsupported 率）既不被灌水也不被稀释，同时把这种机械故障
    单独计数报出来（正常应该恒为 0，不为 0 说明 rubric 没被遵守，数字要存疑）。
    """
    items = raw.get("judgments") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {}, 0
    out: dict[int, dict] = {}
    no_cite = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            i = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        v = str(it.get("v") or "").strip().lower()
        if v not in ("supported", "unsupported", "unknown"):
            continue
        cite = str(it.get("cite") or "").strip()
        why = str(it.get("why") or "").strip()
        if v == "supported" and not cite:
            no_cite += 1
            v, why = "unknown", "[无出处→降级] " + why
        out[i] = {"v": v, "why": why, "cite": cite,
                  "tool_self": bool(it.get("tool_self"))}
    return out, no_cite


_CITE_JOINERS = ("...", "…", "⋯")


def _cite_ok(cite: str, evidence_text: str) -> bool:
    """判官引的出处能不能在证据里找到。

    判官描述跨字段的证据时会用省略号拼两段（实测：``'"name_cn": "孤独摇滚！" ...
    "eps": 12'`` —— 名字在一处、集数在另一处）。那是**诚实的引用**，只是不连续。
    所以按省略号拆段，**每一段**都要逐字落在证据里，段长 ≥4 才算数。

    比对前去掉全部空白：LLM 照抄时偶尔多一个空格，不该记成编造。

    这不是判定，是给 κ 校准（步骤 4）留的审计线索 —— 若 supported 的 cite 大面积
    核不到，说明判官在编出处，那些 ✓ 一个都不能信。
    """
    text = "".join(evidence_text.split())
    parts = [cite]
    for j in _CITE_JOINERS:
        parts = [p for chunk in parts for p in chunk.split(j)]
    parts = ["".join(p.split()) for p in parts]
    parts = [p for p in parts if len(p) >= 4]
    return bool(parts) and all(p in text for p in parts)


# 拆片段用：JSON 标点、中英分隔符、连接词。规则 1 放宽后，判官引的是**依据**而不是
# 断言原句，出处常常是「字段名 + 值」的形式化改写（`rating_total: 36196`）或
# 重排过的标签表（`tags: ["京阿尼", "K-ON!", ...]`），逐字匹配必然落空。
_CITE_SEPS = ("...", "…", "⋯", "\n", " ", "\t", ",", "，", ";", "；", "、", "|",
              "/", "→", "->", "：", ":", '"', "'", "[", "]", "{", "}", "(", ")",
              "（", "）", "·", "+", "与", "和", "及")


def _cite_ok_derived(cite: str, evidence_text: str) -> bool:
    """宽松复核：把出处拆成片段，**每一片**都要能在证据里找到（不看顺序、不看连续）。

    为什么需要它：`_cite_ok` 是给「判官照抄原句」那版 rubric 写的。规则 1 放宽之后
    「从字段值推出来的 / 对数字做算术 / 标签同义改写」都算 supported，这三类的 cite
    按定义就不是原句 —— 实测 8 条核不到里 7 条是这种，**不是判官在编**，是尺子量错了。

    伪造仍然拦得住：编出来的数字/名字（实测过 `13话` 而证据写 12话）拆出来照样
    在证据里找不到。它拦不住的只有「用证据里真实存在的碎片拼出一句假话」—— 那种
    拼接本身正是规则 1 允许的跨字段引用，判官在这层管不了，交给 κ。
    """
    text = "".join(evidence_text.split())
    frags = [cite]
    for sep in _CITE_SEPS:
        frags = [p for chunk in frags for p in chunk.split(sep)]
    frags = ["".join(p.split()) for p in frags]
    # 单字符片段（`7`、`!`）对证据没有区分力，不算数；其余每片都得命中。
    frags = [p for p in frags if len(p) >= 2]
    return bool(frags) and all(p in text for p in frags)


def _check_batch(client: OpenAI, model: str, evidence_text: str,
                 items: list[tuple[int, str]]) -> tuple[dict[int, dict], int, int]:
    """判一批断言，返回 ({编号: 判定}, API 调用次数, 无出处数)。

    判不出来的**二分再判**，不原样重试：实测失败形态是模型陷入长推理把 max_tokens
    烧穿，JSON mode 下返回空串 —— 唯一有效的降载手段是减少同屏断言数。
    """
    if not items:
        return {}, 0, 0
    raw = call_llm(client, model, _CHECK_RUBRIC, _check_prompt(evidence_text, items),
                   temperature=JUDGE_TEMPERATURE, max_tokens=JUDGE_MAX_TOKENS,
                   n_retry=1)
    got, no_cite = _parse_checks(raw)
    missing = [it for it in items if it[0] not in got]
    if not missing or len(items) < JUDGE_MIN_SPLIT:
        return got, 1, no_cite

    n_calls, n_nocite = 1, no_cite
    mid = len(missing) // 2
    for half in (missing[:mid], missing[mid:]):
        sub, calls, nc = _check_batch(client, model, evidence_text, half)
        got.update(sub)
        n_calls += calls
        n_nocite += nc
    return got, n_calls, n_nocite


# ═══════════════════════════════════════════════════════════════════════
# 判定主体
# ═══════════════════════════════════════════════════════════════════════


def _client_and_model() -> tuple[OpenAI, str]:
    """裸 OpenAI client —— 不走 agent.llm 工厂（本模块与生产链路无关）。

    与 eval/generate_rag_gt.py:492 同一套取值，判定侧两条链路口径必须一致。
    """
    from core.config import get_settings
    s = get_settings()
    return OpenAI(api_key=s.LLM_API_KEY, base_url=s.LLM_BASE_URL or None), s.LLM_MODEL


def judge_tier2(frozen: dict, limit: int = 0, verbose: bool = True) -> dict:
    """对冻结样本跑 Tier 2 忠实性判定。返回报告 dict。

    Args:
        limit: 只判前 N 条场景（0=全部）。冒烟用 —— 先花几分钱看一眼口径对不对，
            再跑满。缓存是 append-only 的，中断/重跑都不浪费已判的部分。
    """
    samples = frozen.get("samples") or []
    if limit:
        samples = samples[:limit]
    meta = frozen.get("meta") or {}
    sample_policy = (meta.get("evidence_capture") or {}).get("policy", "?")
    key_meta = {"policy": sample_policy}

    client, model = _client_and_model()
    cache = load_cache()
    t0 = time.time()

    scenarios: list[dict] = []
    for n, s in enumerate(samples, 1):
        sid = s["scenario_id"]
        message = s.get("message") or ""
        reply = s.get("reply") or ""
        evidence_text, ev_stats = build_evidence(s)
        digest = evidence_digest(evidence_text)
        row: dict[str, Any] = {
            "scenario_id": sid, "depth": s.get("depth"),
            "reply_chars": len(reply), "evidence": ev_stats,
            "assertions": [], "judge_calls": 0, "cached": 0,
        }

        if not reply.strip():
            row["note"] = "空回复，无可核断言"
            scenarios.append(row)
            if verbose:
                print(f"  [{n}/{len(samples)}] {sid:<18} 空回复")
            continue

        # ── 第一段：切句 ──────────────────────────────────────────
        sk = _cache_key("split", key_meta, f"{sid}\n{message}\n{reply}")
        hit = cache.get(sk)
        if hit and "assertions" in hit:
            texts = hit["assertions"]
            row["cached"] += 1
        else:
            texts = split_assertions(client, model, message, reply)
            row["judge_calls"] += 1
            append_cache({"k": sk, "kind": "split", "scenario": sid,
                          "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          "model": model, "assertions": texts})
            cache[sk] = {"k": sk, "kind": "split", "assertions": texts}

        # 零证据场景：断言照切（好知道它到底编了几条），但**不核对、不进分母**。
        if not ev_stats["calls"]:
            row["assertions"] = [{"i": i, "text": t, "v": None,
                                  "why": "本场景 Agent 未调用任何工具，无证据可核"}
                                 for i, t in enumerate(texts)]
            row["note"] = "零证据场景（未调工具）—— 单列，不进任何分母"
            scenarios.append(row)
            if verbose:
                print(f"  [{n}/{len(samples)}] {sid:<18} 断言 {len(texts):>2}  ★零证据")
            continue

        # ── 第二段：三值核对 ──────────────────────────────────────
        items = list(enumerate(texts))
        ck = _cache_key("check", key_meta,
                        f"{sid}\n{json.dumps(texts, ensure_ascii=False)}\n{digest}")
        hit = cache.get(ck)
        if hit and "verdicts" in hit:
            raw_v = {int(k): v for k, v in hit["verdicts"].items()}
            no_cite = hit.get("no_cite", 0)
            row["cached"] += 1
        else:
            raw_v, calls, no_cite = _check_batch(client, model, evidence_text, items)
            row["judge_calls"] += calls
            append_cache({"k": ck, "kind": "check", "scenario": sid,
                          "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model,
                          "digest": digest, "no_cite": no_cite,
                          "verdicts": {str(k): v for k, v in raw_v.items()}})
            cache[ck] = {"k": ck, "kind": "check", "verdicts":
                         {str(k): v for k, v in raw_v.items()}, "no_cite": no_cite}

        # 判官漏判的（二分到底仍没有）记 unknown —— 宁可少算依据，不少算分母。
        row["assertions"] = [
            dict(raw_v.get(i, {"v": "unknown", "why": "判官未给出判定", "cite": ""}),
                 i=i, text=t)
            for i, t in items
        ]
        row["no_cite"] = no_cite
        for a in row["assertions"]:
            if a["v"] == "supported":
                # 逐字核 + 宽松核都记：前者跨版本可比，后者反映规则 1 放宽后的真实
                # 引用形态。两个都是 False 才算「判官报了个证据里没有的出处」。
                strict = _cite_ok(a.get("cite", ""), evidence_text)
                a["cite_verified"] = strict
                a["cite_derived"] = strict or _cite_ok_derived(
                    a.get("cite", ""), evidence_text)
        scenarios.append(row)
        if verbose:
            cnt = {v: sum(1 for a in row["assertions"] if a["v"] == v)
                   for v in ("supported", "unsupported", "unknown")}
            print(f"  [{n}/{len(samples)}] {sid:<18} 断言 {len(texts):>2}"
                  f"  ✓{cnt['supported']} ✗{cnt['unsupported']} ?{cnt['unknown']}"
                  f"  证据 {ev_stats['calls']} 次"
                  f"（截断 {ev_stats['truncated']}/失败 {ev_stats['failed']}）")

    rep = {
        "meta": {
            "judged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sample_git_hash": meta.get("git_hash"),
            "sample_recorded_at": meta.get("recorded_at"),
            "evidence_policy": sample_policy,
            "judge_version": JUDGE_VERSION,
            "rubric_fingerprint": _rubric_fingerprint(),
            "model": model,
            "temperature": JUDGE_TEMPERATURE,
            "max_single_message_tokens": _MAX_SINGLE_MESSAGE_TOKENS,
            "limit": limit,
            "elapsed_s": round(time.time() - t0, 1),
        },
        "scenarios": scenarios,
        "overall": _aggregate(scenarios),
        "usage": dict(USAGE),
    }
    return rep


def _aggregate(scenarios: list[dict]) -> dict:
    """汇总。分母规则见模块 docstring —— unknown 与零证据场景都【不进】主指标。"""
    judged = [s for s in scenarios if s.get("evidence", {}).get("calls")]
    no_ev = [s for s in scenarios if not (s.get("evidence") or {}).get("calls")]

    def tally(rows):
        c = {"supported": 0, "unsupported": 0, "unknown": 0}
        for s in rows:
            for a in s["assertions"]:
                if a["v"] in c:
                    c[a["v"]] += 1
        return c

    t = tally(judged)
    denom = t["supported"] + t["unsupported"]
    all_c = tally(scenarios)

    by_depth: dict[str, dict] = {}
    for d in sorted({s.get("depth") for s in judged if s.get("depth")}):
        sub = [s for s in judged if s.get("depth") == d]
        c = tally(sub)
        dd = c["supported"] + c["unsupported"]
        by_depth[d] = {
            **c, "n_scenarios": len(sub), "denom": dd,
            "unsupported_rate": (c["unsupported"] / dd) if dd else None,
            "scenarios_with_unsupported": sum(
                1 for s in sub if any(a["v"] == "unsupported" for a in s["assertions"])),
            "scenarios_with_unsupported_rate": (
                sum(1 for s in sub if any(a["v"] == "unsupported" for a in s["assertions"]))
                / len(sub)) if sub else None,
        }

    with_unsup = [s for s in judged
                  if any(a["v"] == "unsupported" for a in s["assertions"])]
    # 工具自述（规则 5）：断言在讲「工具本身是什么」，模型是从工具定义知道的 ——
    # 判官只看得到工具返回，所以这类通常判 unsupported。**它不是幻觉**，是另一条
    # 信息通路，所以单列一个次口径：把它们从分子分母里同时摘掉看主指标。
    ts = [a for s in judged for a in s["assertions"] if a.get("tool_self")]
    ts_denom = denom - len([a for a in ts if a["v"] in ("supported", "unsupported")])
    ts_unsup = sum(1 for a in ts if a["v"] == "unsupported")
    return {
        "n_scenarios": len(scenarios),
        "n_judged": len(judged),
        "n_no_evidence": len(no_ev),
        "no_evidence_scenarios": [
            {"scenario_id": s["scenario_id"], "n_assertions": len(s["assertions"]),
             "assertions": [a["text"] for a in s["assertions"]]} for s in no_ev],
        **t,
        "denom": denom,
        "unsupported_rate": (t["unsupported"] / denom) if denom else None,
        "tool_self_total": len(ts),
        "tool_self_unsupported": ts_unsup,
        "tool_self_scenarios": [
            {"scenario_id": s["scenario_id"],
             "assertions": [a["text"] for a in s["assertions"] if a.get("tool_self")]}
            for s in judged if any(a.get("tool_self") for a in s["assertions"])],
        "unsupported_rate_excl_tool_self": (
            (t["unsupported"] - ts_unsup) / ts_denom) if ts_denom else None,
        "unknown_rate": (t["unknown"] / sum(t.values())) if sum(t.values()) else None,
        "all_assertions_incl_no_evidence": all_c,
        "scenarios_with_unsupported": len(with_unsup),
        "scenarios_with_unsupported_rate": (len(with_unsup) / len(judged)) if judged else None,
        "no_cite_downgrades": sum(s.get("no_cite", 0) for s in scenarios),
        "cite_unverified": sum(1 for s in scenarios for a in s["assertions"]
                               if a["v"] == "supported" and a.get("cite_verified") is False),
        "cite_unverified_loose": sum(1 for s in scenarios for a in s["assertions"]
                                     if a["v"] == "supported"
                                     and a.get("cite_derived") is False),
        "judge_calls": sum(s.get("judge_calls", 0) for s in scenarios),
        "cache_hits": sum(s.get("cached", 0) for s in scenarios),
        "by_depth": by_depth,
        "truncated_evidence_calls": sum(
            s.get("evidence", {}).get("truncated", 0) for s in scenarios),
        "failed_evidence_calls": sum(
            s.get("evidence", {}).get("failed", 0) for s in scenarios),
    }


# ═══════════════════════════════════════════════════════════════════════
# dry-run —— 只打印，不调 LLM、不花钱
# ═══════════════════════════════════════════════════════════════════════


def dry_run_report(frozen: dict) -> dict:
    """判官将看到多少证据 —— 免费预览，用来在花钱前确认口径。"""
    samples = frozen.get("samples") or []
    rows, no_ev = [], []
    for s in samples:
        text, st = build_evidence(s)
        row = {"scenario_id": s["scenario_id"], "depth": s.get("depth"),
               "reply_chars": len(s.get("reply") or ""), "judge_chars": len(text) if st["calls"] else 0,
               **st}
        rows.append(row)
        if not st["calls"]:
            no_ev.append(s["scenario_id"])
    return {"rows": rows, "no_evidence": no_ev}


def print_dry_run(rep: dict) -> None:
    rows = rep["rows"]
    judged = [r for r in rows if r["calls"]]
    print(f"\n{'=' * 104}")
    print(f"Tier 2 判官「模型视角」证据 dry-run（不调 LLM、不花钱）· "
          f"场景 {len(rows)} 条（有证据 {len(judged)} + 零证据 {len(rep['no_evidence'])}）")
    print(f"{'=' * 104}")
    print(f"{'scenario':<18}{'回复':>5}{'调用':>5}{'截断':>5}{'失败':>5}"
          f"{'判官字数':>10}{'tok':>8}{'最大tok':>9}   超限")
    print("-" * 104)
    for r in rows:
        flag = "  ⚠️ 超 L1 单条上限" if r["max_tokens"] > _MAX_SINGLE_MESSAGE_TOKENS else ""
        print(f"{r['scenario_id']:<18}{r['reply_chars']:>5}{r['calls']:>5}{r['truncated']:>5}"
              f"{r['failed']:>5}{r['judge_chars']:>10,}{r['tokens']:>8,}"
              f"{r['max_tokens']:>9,}{flag}")
    print("-" * 104)
    tot_c = sum(r["chars"] for r in rows)
    tot_t = sum(r["tokens"] for r in rows)
    print(f"{'合计':<18}{'':>5}{sum(r['calls'] for r in rows):>5}"
          f"{sum(r['truncated'] for r in rows):>5}{sum(r['failed'] for r in rows):>5}"
          f"{tot_c:>10,}{tot_t:>8,}")
    if judged:
        print(f"{'均摊/有证据场景':<18}{'':>5}{'':>5}{'':>5}{'':>5}"
              f"{tot_c // len(judged):>10,}{tot_t // len(judged):>8,}")
    print(f"\n零证据场景（判不了忠实性，须单列不进分母）{len(rep['no_evidence'])} 条："
          f"{'、'.join(rep['no_evidence']) or '（无）'}")
    print(f"\n判官看到的就是模型当时读到的：工具返回按 "
          f"`json.dumps(..., ensure_ascii=False)` 序列化（与 LangChain `_stringify` 同路，"
          f"实测 46/46 byte 一致），超 {_MAX_SINGLE_MESSAGE_TOKENS} token 的走生产同款"
          f" `_truncate_message_content`（弃尾保头，末尾补 {_TRUNCATION_MARKER!r}）。")
    print("「截断」「失败」两个计数进判官 prompt，但**不**产生 unknown —— 判官看的就是模型"
          "当时读到的那一份，「证据里没有」直接等于「模型当时没有」。它们只是证据完整度的读数。")


def load_frozen(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════════════════════════════


def _pct(v, n) -> str:
    return f"{100 * v / n:.1f}%" if n else "—"


def print_tier2(rep: dict) -> None:
    o, m = rep["overall"], rep["meta"]
    print(f"\n{'=' * 104}")
    print(f"轴 3 Tier 2 · 忠实性判定（模型 {m['model']}，温度 {m['temperature']}）")
    print(f"{'=' * 104}")
    print(f"  场景 {o['n_scenarios']} 条 = 可判 {o['n_judged']} + 零证据 {o['n_no_evidence']}"
          f"（零证据不进任何分母）")
    print(f"  断言 {sum(o['all_assertions_incl_no_evidence'].values())} 条"
          f"（可判场景内 {o['denom'] + o['unknown']} 条）")

    print(f"\n  【主指标】断言级 unsupported 率 = "
          f"{o['unsupported']} / ({o['supported']} + {o['unsupported']}) = "
          f"{_pct(o['unsupported'], o['denom'] or 1)}")
    print(f"            supported {o['supported']}  unsupported {o['unsupported']}"
          f"  unknown {o['unknown']}（unknown 不进分母）")
    print(f"  【次指标】有 unsupported 断言的场景 "
          f"{o['scenarios_with_unsupported']} / {o['n_judged']} = "
          f"{_pct(o['scenarios_with_unsupported'], o['n_judged'] or 1)}")
    if o.get("tool_self_total"):
        ts_d = (o["denom"] - o["tool_self_total"])
        print(f"  【口径 B】摘掉「工具自述」后 = "
              f"{o['unsupported'] - o['tool_self_unsupported']} / {ts_d} = "
              f"{_pct(o['unsupported'] - o['tool_self_unsupported'], ts_d or 1)}"
              f"   （工具自述 {o['tool_self_total']} 条，其中 "
              f"{o['tool_self_unsupported']} 条判 unsupported）")

    if o["by_depth"]:
        print(f"\n  {'按 depth':<12}{'场景':>5}{'断言':>6}{'✓':>5}{'✗':>5}{'?':>5}"
              f"{'✗率':>9}{'有✗场景':>10}")
        for d, v in o["by_depth"].items():
            print(f"  {d:<12}{v['n_scenarios']:>5}{v['denom'] + v['unknown']:>6}"
                  f"{v['supported']:>5}{v['unsupported']:>5}{v['unknown']:>5}"
                  f"{_pct(v['unsupported'], v['denom'] or 1):>9}"
                  f"{_pct(v['scenarios_with_unsupported'], v['n_scenarios']):>10}")
    if o["by_depth"]:
        print(f"  {'合计':<12}{o['n_judged']:>5}{o['denom'] + o['unknown']:>6}"
              f"{o['supported']:>5}{o['unsupported']:>5}{o['unknown']:>5}"
              f"{_pct(o['unsupported'], o['denom'] or 1):>9}"
              f"{_pct(o['scenarios_with_unsupported'], o['n_judged'] or 1):>10}")

    if o.get("tool_self_scenarios"):
        print(f"\n  【单列】工具自述 {o['tool_self_total']} 条"
              f"（断言在讲「工具本身是什么」—— 模型从工具定义知道的，"
              f"判官只看得到工具返回，所以通常判 unsupported。**不是幻觉**）：")
        for s in o["tool_self_scenarios"]:
            print(f"    · {s['scenario_id']}：{'；'.join(s['assertions'][:2])}")

    if o["no_evidence_scenarios"]:
        print(f"\n  【单列·不进分母】零证据场景 {len(o['no_evidence_scenarios'])} 条"
              f"（Agent 一次工具都没调，回复里的事实断言无从核起）：")
        for s in o["no_evidence_scenarios"]:
            print(f"    · {s['scenario_id']}：{s['n_assertions']} 条断言"
                  + (f" —— {'；'.join(s['assertions'][:2])}" if s["assertions"] else ""))

    print(f"\n  证据完整度：{o['truncated_evidence_calls']} 次调用被 L1 截断、"
          f"{o['failed_evidence_calls']} 次调用失败 —— 这是【模型当时拿到的上下文有多残】"
          f"的读数，不是 unknown 的来源（判官看到的截断版就是模型看到的那一份）。")
    if o["no_cite_downgrades"] or o["cite_unverified"]:
        print(f"  ⚠️ 判官报 supported 却没给出处被降级的：{o['no_cite_downgrades']} 条"
              f"（这个应恒为 0）；出处核不到原文的：逐字 {o['cite_unverified']} 条 / "
              f"宽松 {o['cite_unverified_loose']} 条。")
        print(f"     逐字核不到**不一定是错** —— 规则 1 允许「推出来的依据」，"
              f"那类出处按定义不是原句（`rating_total: 36196` 这种改写）。"
              f"要看的是宽松那一列：它才是「判官报了个证据里根本没有的东西」。")
    u = rep["usage"]
    print(f"\n  开销：HTTP {u['http_calls']} 次，prompt {u['prompt_tokens']:,} tok，"
          f"completion {u['completion_tokens']:,} tok；"
          f"判定调用 {o['judge_calls']} 次、缓存命中 {o['cache_hits']} 次，"
          f"耗时 {m['elapsed_s']}s")

    print("\n  读法:")
    print("    · 主指标的分母是 supported + unsupported，**unknown 不进分母**。unknown 门槛")
    print("      很高：判官看到的证据就是模型当时读到的那一份（L1 截断判官侧复现），")
    print("      「证据里没有」直接等于「模型当时没有」—— 所以截断与工具失败都【不】产生")
    print("      unknown，它们只是【模型当时上下文有多残】的读数。")
    print("    · 「零证据场景」单列：Agent 没调工具却讲了事实，属于另一类问题（该类场景")
    print("      本身可能就是设计成不调工具的闲聊），混进分母会把两个问题搅成一个数。")
    print("    · ★ unsupported **不等于「编的」**，它只说明【这句在工具返回里找不到依据】。")
    print("      实测三类：① 模型自带知识（常识、剧情梗概）—— 内容多半是对的，只是没依据；")
    print("      ② 对作品的解读（主题、导演意图）—— 工具返回里本来就没有这类东西；③ 真幻觉。")
    print("      本轴量的是「回复里有多少内容是从检索来的」，**不是「编造了多少」**。")
    print("    · 判 supported 必须报出处；报不出的降级 unknown 并计数（这个应恒为 0）。")
    print("      出处再拿回证据核两遍：逐字核（可用省略号拼两段）+ 宽松核（拆片段，")
    print("      每片都要在证据里出现）。**逐字核不到不等于错** —— 规则 1 允许「推出来的")
    print("      依据」，那类出处按定义不是原句。宽松核不到才是真的报了证据里没有的东西。")
    print("    · ★「工具自述」（规则 5）：断言在讲【工具本身是什么 / 能做什么】时会被打上")
    print("      tool_self —— 模型是从工具定义知道这些的，判官只看得到工具返回，所以这类")
    print("      通常判 unsupported。**它不是幻觉**，是另一条信息通路。故单列，并给出")
    print("      「口径 B」：把这一类从分子分母同时摘掉后的主指标。两个数一起报。")
    print("    · ⚠ 本轴数字在 κ 校准（人工标注）完成前**不得对外引用** —— "
          "eval/README.md 约束 1。")


def save_tier2(rep: dict, frozen_path: Path) -> tuple[Path, Path]:
    # 延迟 import：避免与 reply_quality_eval 形成环
    from eval.reply_quality_eval import RESULTS_DIR as RD
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = RD / f"reply_quality-tier2-{stamp}-{rep['meta'].get('sample_git_hash', 'nogit')}"
    RD.mkdir(parents=True, exist_ok=True)
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    o, m = rep["overall"], rep["meta"]
    lim = m.get("limit")
    lines = [
        "# 轴 3 · 回复质量 — Tier 2 忠实性判定报告",
        f"\n**判定于** {m['judged_at']} | **样本** `{frozen_path.name}`"
        f" | **录制于** {m['sample_recorded_at']} | **git** {m['sample_git_hash']}",
        "\n> 生成/判定分离：本报告只读冻结样本，不重新生成 —— 同一份样本可反复判定、"
        "换 rubric 重判。缓存 append-only 落 `eval/data/reply_quality_judge_cache.jsonl`。",
        f"\n> 判官口径 `{m['rubric_fingerprint']}`（含 rubric 指纹与证据口径，进缓存键）"
        f" | 模型 `{m['model']}` 温度 {m['temperature']}"
        + (f" | **冒烟：只判了前 {lim} 条**" if lim else ""),
        f"\n> 判官看到的就是模型当时读到的：证据按 `json.dumps(..., ensure_ascii=False)` "
        f"序列化（与 LangChain `_stringify` 同路，46/46 byte 对账一致），超 "
        f"{m['max_single_message_tokens']} token 的走生产同款 `_truncate_message_content`"
        f"（弃尾保头）。`manage_memory` 在每一轮 LLM 调用前都跑，所以**模型从未见过完整版**"
        f"—— 判官视角 ≡ 模型视角，「证据里没有」直接等于「模型当时没有」。"
        f"证据块头的「完整/已截断/失败」是证据完整度的读数，**不**用来产生 unknown。",
        f"\n## 场景规模\n",
        f"- 场景 {o['n_scenarios']} 条 = 可判 **{o['n_judged']}** + 零证据 "
        f"**{o['n_no_evidence']}**（单列，不进任何分母）",
        f"- 断言 {sum(o['all_assertions_incl_no_evidence'].values())} 条，"
        f"其中可判场景内 {o['denom'] + o['unknown']} 条",
        f"- 证据侧：{o['truncated_evidence_calls']} 次调用被 L1 截断、"
        f"{o['failed_evidence_calls']} 次调用失败",
        "\n## 指标\n",
        "| 指标 | 值 | 定义 |",
        "|---|---|---|",
        f"| **断言级 unsupported 率**（主） | **{_pct(o['unsupported'], o['denom'] or 1)}**"
        f" | `unsupported / (supported + unsupported)`，unknown 不进分母 |",
        f"| 有 unsupported 断言的场景占比（次） | "
        f"{_pct(o['scenarios_with_unsupported'], o['n_judged'] or 1)}"
        f" | `{o['scenarios_with_unsupported']} / {o['n_judged']}` |",
        f"| supported / unsupported / unknown | {o['supported']} / {o['unsupported']}"
        f" / {o['unknown']} | |",
        (f"| （口径 B）摘掉工具自述后 | "
         f"**{_pct(o['unsupported'] - o['tool_self_unsupported'], (o['denom'] - o['tool_self_total']) or 1)}**"
         f" | 工具自述 {o['tool_self_total']} 条不进分子分母 |"
         if o.get("tool_self_total") else
         "| （口径 B）摘掉工具自述后 | — | 本样本无工具自述断言 |"),
        f"| unknown 占全部可判断言 | {_pct(o['unknown'], (o['denom'] + o['unknown']) or 1)}"
        f" | 门槛很高：仅证据整块缺失或断言无从核对 |",
        f"| 证据完整度（L1 截断 / 失败 调用数） | {o['truncated_evidence_calls']} / "
        f"{o['failed_evidence_calls']} | 模型当时上下文有多残；**不**产生 unknown |",
        f"| 判官漏判（二分到底仍无判定） | "
        f"{sum(1 for s in rep['scenarios'] for a in s['assertions'] if a.get('why') == '判官未给出判定')}"
        f" | 记 unknown |",
        f"| supported 无出处被降级 | {o['no_cite_downgrades']} | 正常应恒为 0 |",
    ]
    if o["by_depth"]:
        lines += ["\n## 按 depth\n", "| depth | 场景 | 断言 | ✓ | ✗ | ? | ✗率 | 有✗场景 |",
                  "|---|---|---|---|---|---|---|---|"]
        for d, v in o["by_depth"].items():
            lines.append(f"| {d} | {v['n_scenarios']} | {v['denom'] + v['unknown']} | "
                         f"{v['supported']} | {v['unsupported']} | {v['unknown']} | "
                         f"{_pct(v['unsupported'], v['denom'] or 1)} | "
                         f"{_pct(v['scenarios_with_unsupported'], v['n_scenarios'])} |")
    if o["no_evidence_scenarios"]:
        lines += ["\n## 单列 · 零证据场景（不进分母）\n",
                  "Agent 一次工具都没调。回复里的事实断言无从核起 —— 混进分母会把"
                  "「没调工具」与「有据不用」搅成一个数。\n",
                  "| 场景 | 断言数 | 断言 |", "|---|---|---|"]
        for s in o["no_evidence_scenarios"]:
            cells = "<br>".join(a.replace("|", "\\|") for a in s["assertions"]) or "—"
            lines.append(f"| `{s['scenario_id']}` | {s['n_assertions']} | {cells} |")

    lines += ["\n## 逐条\n",
              "| 场景 | depth | 证据(调用/截断/失败) | 断言 | ✓ | ✗ | ? |",
              "|---|---|---|---|---|---|---|"]
    for s in rep["scenarios"]:
        ev = s.get("evidence") or {}
        c = {v: sum(1 for a in s["assertions"] if a["v"] == v)
             for v in ("supported", "unsupported", "unknown")}
        lines.append(f"| `{s['scenario_id']}` | {s.get('depth', '')} | "
                     f"{ev.get('calls', 0)}/{ev.get('truncated', 0)}/{ev.get('failed', 0)} | "
                     f"{len(s['assertions'])} | {c['supported']} | {c['unsupported']} | "
                     f"{c['unknown']} |")

    lines += ["\n## 断言明细\n", "κ 校准（人工标注）就从这里抽样。\n"]
    for s in rep["scenarios"]:
        if not s["assertions"]:
            continue
        lines.append(f"\n### `{s['scenario_id']}`\n")
        for a in s["assertions"]:
            mark = {"supported": "✓", "unsupported": "✗", "unknown": "?"}.get(a["v"], "—")
            lines.append(f"- {mark} **{a['text']}**")
            if a["v"] is not None:
                lines.append(f"  - `{a['v']}` — {a.get('why', '')}")
            if a.get("cite"):
                lines.append(f"  - 出处：`{a['cite'][:200]}`")

    lines += [
        "\n## 这个数是什么、不是什么\n",
        "**unsupported 不等于「编的」** —— 它只说明【这句在工具返回里找不到依据】。"
        "实测能分出三类：",
        "1. **模型自带的知识**（常识、剧情梗概、角色关系）—— 内容多半是对的，只是没有依据；",
        "2. **对作品的解读**（主题、导演意图、口碑评价）—— 工具返回里本来就不存在这类东西，",
        "   只要模型开口谈作品就必然记为 unsupported；",
        "3. **真幻觉**。",
        "",
        "所以本轴量的是「**回复里有多少内容是从检索来的**」，不是「编造了多少」。"
        "要区分这三类得靠人工 —— 那正是步骤 4 的 κ 校准要抽样读的东西。",
        "",
        "另一侧的对照：`supported` 的断言里，出处会被拿去证据里核两遍 —— **逐字核**"
        "（允许用省略号拼两段）和**宽松核**（拆成片段，每片都得在证据里出现，不看顺序）。",
        f"实测：逐字核不到 {o['cite_unverified']} 条、宽松核不到 "
        f"{o['cite_unverified_loose']} 条。**逐字那一列高是正常的、不是错** —— 规则 1 "
        "放宽「找到依据」之后，判官引的是依据而不是原句（`rating_total: 36196` 这种"
        "字段名+值的改写、重排过的标签表）。要看的是宽松那一列：它才是「判官报了个"
        "证据里根本没有的数字/名字」。",
        "\n## 开销\n",
              f"- HTTP {rep['usage']['http_calls']} 次，prompt "
              f"{rep['usage']['prompt_tokens']:,} tok，completion "
              f"{rep['usage']['completion_tokens']:,} tok",
              f"- 判定调用 {o['judge_calls']} 次，缓存命中 {o['cache_hits']} 次，"
              f"耗时 {m['elapsed_s']}s"]

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


# ═══════════════════════════════════════════════════════════════════════
# 步骤 4 · 人工 κ 校准
# ═══════════════════════════════════════════════════════════════════════
#
# 判官判得对不对，只有人独立判一遍才知道。流程照搬 D 组
# （eval/generate_rag_gt.py 的 ANNOTATION_SHEET 一族），三处【故意不同】：
#
#   ① 每条断言**必须带着证据**。剥掉证据让人凭自己的知识判，标出来的就成了
#      「人 vs 判官谁更懂动画」—— 跟本轴要量的东西无关。判官被明令禁止用百科
#      知识，人也一样，否则 κ 比出来的是个假数，而且会假得很难看。
#   ② D 组一条候选就是一行卡片；这里一条断言要配一整块证据（最长 2000 token），
#      所以**按场景排版、证据只印一遍**，且同一场景至多抽 2 条 —— 同场景的断言
#      共用一份证据，抽多了是把同一次误读重复计数，κ 的置信区间会假性收窄。
#   ③ 判官判定**直接写死在 key 里**。D 组得拿缓存反推（卡片格式变过，只能重算
#      哈希对齐）；我们的 report JSON 本身就是冻结记录，抄下来即可，不碰缓存。

ANNO_DIR = Path(__file__).resolve().parent / "data"
ANNOTATION_SHEET = ANNO_DIR / "annotation_sheet_tier2.md"
ANNOTATION_KEY = ANNO_DIR / "annotation_key_tier2.json"

ANNOTATE_N = 40            # 主样本条数（判官判 unsupported 一半、supported 一半）
ANNOTATE_PER_SCENARIO = 2  # 同一场景至多抽几条
ANNOTATE_SEED = 20260914

TIER2_VERDICTS = ("supported", "unsupported", "unknown")

# ⚠ 必须带 re.M —— 少了它 `^` 只认整串开头，`_count_answered()` 恒返回 0，
# 防呆失效，重跑会**静默覆盖已填的标注表**（D 组踩过同一个坑，见
# generate_rag_gt.py:2155）。条目按 `**#N 断言**` 切块，块内找答案行。
#
# 答案行捕获**整行剩余**而不是 `(\S*)`：D 组那个写法在本表会出事 —— 判词后面
# 顺手补一句理由（「supported（"score": 8.23）」）会让 `\S*` 吞成
# `supported（"score":`，落不进词表 → **答案被静默丢掉**。改成整行找独立的判词。
_ANSWER_RE = re.compile(r"^\*\*你的判定\*\*[：:](.*)$", re.M)
_ITEM_RE = re.compile(r"^\*\*#(\d+)\s*断言\*\*[：:][ \t]*(.*)$", re.M)


def _read_verdict(line: str) -> str | None:
    """答案行 → 判定词。整行找**独立**出现的判词，不要求行首。

    人会在后面补理由、或在前面写「我判 …」，都认。`(?<![a-z])` 保证
    `unsupported` 里的 `supported` 不算命中。词表里没有互为前缀的词，
    但按长度倒序扫是防将来加词的便宜保险。
    """
    low = (line or "").lower()
    for v in sorted(TIER2_VERDICTS, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){v}(?![a-z])", low):
            return v
    return None

# 示范里用掉的真实条目 —— 必须从抽样里剔掉：看过答案的题不能再标。
# 用前缀匹配（断言文本在报告里就是这串）。
_DEMO_USED = (
    ("C3-人物查询", "花泽香菜配过千石抚子"),
    ("C2-查评分", "《进击的巨人》第一季评分是 8.23"),
    ("B1-bangumi人格", "《孤独摇滚！》有12话"),
    ("A5-deep深入", "EVA问的是"),
)


def _count_answered() -> int:
    """标注表里已经填了多少条 —— 重建前的防呆用。"""
    if not ANNOTATION_SHEET.exists():
        return 0
    return sum(1 for m in _ANSWER_RE.finditer(
        ANNOTATION_SHEET.read_text(encoding="utf-8")) if m.group(1))


def _collect_assertions(rep: dict) -> list[dict]:
    """报告里所有【有判定】的断言，带场景坐标。

    零证据场景的断言 `v` 是 None（切了句但没核对）—— 它们不进任何分母，也就没有
    判官判定可校准，不能进标注表。
    """
    out: list[dict] = []
    for s in rep.get("scenarios", []):
        for a in s.get("assertions", []):
            if a.get("v") not in TIER2_VERDICTS:
                continue
            if any(s["scenario_id"] == sid and a["text"].startswith(pfx)
                   for sid, pfx in _DEMO_USED):
                continue                     # 示范用掉了，不能再标
            out.append({"scenario_id": s["scenario_id"], "depth": s.get("depth"),
                        "i": a["i"], "text": a["text"], "v": a["v"],
                        "why": a.get("why", ""), "cite": a.get("cite", ""),
                        "cite_verified": a.get("cite_verified")})
    return out


def _sample_for_annotation(items: list[dict], n: int, per_scenario: int,
                           seed: int) -> list[tuple[str, list[dict]]]:
    """分层抽样 → [(场景, 该场景抽中的断言…)]，**场景顺序已洗牌**。

    按判官判定分层（unsupported / supported），两层各要一半。某一层不够时把余量
    让给另一层 —— 宁可样本偏，也不拿重复条目凑数（D 组同款取舍）。

    ⚠ 洗牌洗的是**场景顺序**：按层分组排版等于把判官的答案写在脸上，标出来的就是
    「你附和判官的比率」而不是 κ。场景内按断言原序排（那是回复的自然顺序，与判定
    无关）。
    """
    rng = random.Random(seed)
    strata = ("unsupported", "supported")
    pools = {v: [x for x in items if x["v"] == v] for v in strata}
    for v in strata:
        rng.shuffle(pools[v])

    # per_scenario 的额度是两层**共用**的（同一场景的断言共用一份证据，抽多了是把
    # 同一次误读重复计数）。共用就意味着两层要抢额度 —— 先抽满一层再抽另一层，
    # 先抽的那层会把共用场景吃光（实测 U 先抽 → 22:17 的偏样本）。所以**按场景轮转**：
    # 每个场景轮流由一层先挑，每轮换先后。顺序有偏时也只是样本略偏，不是某一层被饿死。
    quota = {v: n // 2 for v in strata}
    sids = sorted({x["scenario_id"] for x in items})
    rng.shuffle(sids)
    taken: set[tuple[str, int]] = set()
    picked: dict[str, list[dict]] = {v: [] for v in strata}

    for rnd, sid in enumerate(sids):
        for j in range(per_scenario):
            for v in (strata if (rnd + j) % 2 == 0 else strata[::-1]):
                if quota[v] <= 0:
                    continue
                cand = next((x for x in pools[v] if x["scenario_id"] == sid
                             and (sid, x["i"]) not in taken), None)
                if cand is None:
                    continue
                taken.add((sid, cand["i"]))
                picked[v].append(cand)
                quota[v] -= 1
                break

    used: dict[str, int] = {}
    for sid, _ in taken:
        used[sid] = used.get(sid, 0) + 1
    for v in strata:                          # 某层抽不满 → 余量让给另一层
        for x in pools[v]:
            if len(taken) >= n:
                break
            sid = x["scenario_id"]
            if (sid, x["i"]) in taken or used.get(sid, 0) >= per_scenario:
                continue
            taken.add((sid, x["i"]))
            used[sid] = used.get(sid, 0) + 1
            picked[v].append(x)

    groups: dict[str, list[dict]] = {}
    for v in strata:
        for x in picked[v]:
            groups.setdefault(x["scenario_id"], []).append(x)
    order = sorted(groups)                    # 先定序再洗牌，保证同 seed 可复现
    rng.shuffle(order)
    return [(sid, sorted(groups[sid], key=lambda x: x["i"])) for sid in order]


_SHEET_HEAD = """# 轴 3 Tier 2 忠实性判官 · 人工标注表

你在标的是**判官的校准集**。判官（`{MODEL}`，温度 0）对 Agent 回复里切出来的每条
可核查断言做了 supported / unsupported 判断，现在由你**独立**判一遍，两边一比算出
Cohen's κ。

> ⚠ **κ 出来之前，Tier 2 的数字（unsupported 38.4%）不许对外引用**
> —— `eval/README.md` 的硬规矩。这张表就是解锁它的钥匙。

## ⚠ 先看清楚：这不是知识测验

判官被问的是 **「这句话能不能在 Agent 当时的工具返回里找到」**，
不是 **「这句话对不对」**。

你比判官多知道的东西，在这张表里是**干扰项**。下面的证据是 Agent 当时手上唯一的
材料 —— 它没查到的，就不许算它的。**判 unsupported 也不是在骂它编造**：量的是
「回复里有多少内容是从检索来的」，不是「编了多少」。

## 四个示范（不用你标，看个手感）

**示范 1 · 真的，但仍然是 `unsupported`**

> **用户问**：花泽香菜配过哪些角色
> **Agent 答**：千石抚子（化物语）、立华奏（Angel Beats!）、神乐（银魂）……
>
> **证据**（此处只摘开头，表内是完整的）：
> ```
> ### 证据 1 · search_bangumi_subject · 完整
> {"results": [{"id": 4765, "name": "花澤香菜", "name_cn": "花泽香菜",
>   "info": "性别 女 / 生日 1989年2月25日 / 血型 AB型", "type": "个人",
>   "career": ["artist", "seiyu"]}, {"id": 48365, "name": "花澤友梨", "type": "个人",
>   "career": ["producer"]}, {"id": 61543, "name": "花澤菜摘", "type": "个人"} …]}
> ```
> **你的判定** → `unsupported`。工具返回的是**一堆姓花澤的人**（person 检索），
> 里头一个字都没提千石抚子。这句**很可能是真的**（花泽香菜确实配过），但 Agent
> 当时手上没有它 —— 那是模型自己带的。**这一条判 supported 就是错。**

**示范 2 · 有出处，`supported`**

> **断言**：`《进击的巨人》第一季评分是 8.23`
> 证据里明明白白有 `"score": 8.23`。
> **你的判定** → `supported`。判官判 supported 时**必须照抄**这样的片段，抄不出来
> 就不算 —— 你判的时候最好也顺手抄一下，抄不出来就别填 supported。

**示范 3 · 作品解读，也是 `unsupported`**

> **用户问**：深入分析 EVA 和巨人的主题差异
> **断言**：`EVA问的是"我为什么无法与人相连"`
> **你的判定** → `unsupported`。它听着像"观点"，但它是一个**关于这部作品的陈述**，
> 证据里没有任何依据。切句阶段已经把纯口味表态（「我个人更喜欢这部」）滤掉了，
> 能送到这里的都要核。**这是 2026-09-14 拍板的口径，不是 bug。**

**示范 4 · 跨字段的出处也算数**

> **断言**：`《孤独摇滚！》有12话`
> 判官引的是：`"name_cn": "孤独摇滚！" ... "eps": 12`
> **你的判定** → `supported`。名字在一处、集数在另一处，判官用 `...` 把两段拼起来
> 是**诚实的引用**（同一份工具返回的不同字段），不是编造。你能指出来就够了。

## 怎么填

每条断言下面有一行 `**你的判定**：`，在冒号后面填三个词之一：

| 填什么 | 什么意思 |
|---|---|
| `supported` | 你**能在下面的证据里指出来**（能抄出原句最好） |
| `unsupported` | 证据里找不到。**跟这句话本身对不对无关** |
| `unknown` | 你判不了 —— 门槛很高，见下 |

`unknown` 只有这两种情形才算：

- 这条断言依赖的证据**整块不在**（回复明显在引用某次检索，而证据里根本没有那次
  调用的返回）—— 那是录制侧的缺口，不能算在模型头上；
- 断言本身读不懂、指代不清、或自相矛盾，无从核对。

⚠ **两条最容易踩的**：

- 证据块标着「已截断」或「失败」**不构成 unknown**。截断砍掉的是尾部，**模型当时
  也没看到**；失败的那次调用模型也没拿到数据。「证据里没有」直接等于「模型当时
  没有」⇒ 照样 `unsupported`。
- **不要因为「我没看过这部番」就填 `unsupported`** —— 那不是判据。判据只有一条：
  **证据里有没有**。真判不了才填 `unknown`，但别拿它当"我不确定"的出口。

## 判定口径（判官当时收到的就是这段，原文照抄）

```
{RUBRIC}
```

↑ 上面是判官实际收到的原文，一个字没改。**最后那个 JSON 格式是给判官的**，你不用管
—— 你只要在每条的 `**你的判定**：` 后面写一个词（`supported` / `unsupported` /
`unknown`）。判词后面想补理由、想把出处抄上，随便补；读的时候只认判词，其余忽略。

## 关于这份样本

本次 {N} 条，从 {N_JUDGED} 个可判场景的 {N_POP} 条断言里抽，判官判 unsupported 的
抽 {N_UNS} 条、supported 的抽 {N_SUP} 条，**同一场景至多 {PER_SCENARIO} 条**，
场景顺序已洗牌。

> ⚠ 这是**分层样本，不是总体加权** —— 总体里 supported:unsupported ≈ 85:53，本表
> 是 {N_SUP}:{N_UNS}。κ 要连着这句一起报，别当成总体 κ。
> 分层是为了让「判官说 unsupported」的那一半有足够条目可校准 —— 那才是主指标。

> ⚠ 判官判定**故意不出现在本表里**，它只写在 `annotation_key_tier2.json`。
> 先看到判官的答案再标，标出来的是"你附和判官的比率"而不是 κ。**标完再看那份 key。**

---

"""


def _render_sheet(groups: list[tuple[str, list[dict]]], frozen: dict,
                  meta: dict, n_pop: int) -> str:
    """标注表正文。证据按场景印一次，断言全局编号 —— 编号只用于回读答案。"""
    by_id = {s["scenario_id"]: s for s in frozen.get("samples", [])}
    n_uns = sum(1 for _, g in groups for x in g if x["v"] == "unsupported")
    n_sup = sum(1 for _, g in groups for x in g if x["v"] == "supported")
    head = (_SHEET_HEAD
            .replace("{MODEL}", str(meta.get("model", "?")))
            .replace("{RUBRIC}", _CHECK_RUBRIC)
            .replace("{N_JUDGED}", str(meta.get("n_judged", "?")))
            .replace("{N_POP}", str(n_pop))
            .replace("{N_UNS}", str(n_uns)).replace("{N_SUP}", str(n_sup))
            .replace("{N}", str(n_uns + n_sup))
            .replace("{PER_SCENARIO}", str(ANNOTATE_PER_SCENARIO)))

    lines = [head]
    k = 0
    for gi, (sid, items) in enumerate(groups, 1):
        s = by_id[sid]
        ev_text, _ = build_evidence(s)
        lines += [
            f"## 场景 {gi}/{len(groups)} · `{sid}`（{s.get('depth', '?')}）", "",
            f"**用户问**：{s.get('message', '')}", "",
            "**Agent 回复**：", "", "~~~~", (s.get("reply") or "").strip(), "~~~~", "",
            "**证据**（Agent 当时读到的就是这份，含生产同款截断）：", "",
            "~~~~", ev_text, "~~~~", "",
        ]
        for x in items:
            k += 1
            lines += [f"**#{k} 断言**：{x['text']}", "", "**你的判定**：", ""]
        lines += ["---", ""]
    return "\n".join(lines)


def save_annotation_sheet(rep: dict, frozen: dict, report_name: str,
                          n: int = ANNOTATE_N, seed: int = ANNOTATE_SEED,
                          per_scenario: int = ANNOTATE_PER_SCENARIO,
                          ) -> tuple[Path, Path]:
    """从 Tier 2 报告生成标注表 + 答案纸。返回 (表, key)。

    已经填过就不覆盖 —— 重出会静默抹掉人工标注（一小时的活）。
    """
    done = _count_answered()
    if ANNOTATION_SHEET.exists() and done:
        raise SystemExit(
            f"标注表里已经填了 {done} 条，不覆盖：{ANNOTATION_SHEET}\n"
            f"要重出请先改名或删掉它。")

    items = _collect_assertions(rep)
    groups = _sample_for_annotation(items, n, per_scenario, seed)
    o = rep.get("overall") or {}
    meta = dict(rep.get("meta") or {})
    meta["n_judged"] = o.get("n_judged", "?")
    n_pop = sum(1 for s in rep.get("scenarios", [])
                for a in s.get("assertions", []) if a.get("v") in TIER2_VERDICTS)
    # 总体两层的真实条数 —— 报抽样比要用它，别拿样本层数去减总体总数。
    pop = {"n": n_pop}
    for v in ("supported", "unsupported"):
        pop[v] = sum(1 for s in rep.get("scenarios", [])
                     for a in s.get("assertions", []) if a.get("v") == v)

    sheet = _render_sheet(groups, frozen, meta, n_pop)
    ANNO_DIR.mkdir(parents=True, exist_ok=True)
    ANNOTATION_SHEET.write_text(sheet, encoding="utf-8")

    flat = [(sid, x) for sid, g in groups for x in g]
    strata: dict[str, int] = {}
    for _, x in flat:
        strata[x["v"]] = strata.get(x["v"], 0) + 1
    key = {
        "version": "tier2-anno-1",
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "purpose": ("轴 3 Tier 2 判官的人工校准集。判官判定【故意不写进标注表】，"
                    "避免锚定；这一份是事后算 κ 的答案纸。"),
        "source_report": report_name,
        "rubric_fingerprint": meta.get("rubric_fingerprint"),
        "model": meta.get("model"),
        "sample": {
            "n": len(flat), "seed": seed, "per_scenario": per_scenario,
            "strata": strata,
            "population": dict(pop, n_scenarios=meta.get("n_judged")),
            "warning": ("分层样本，不是总体加权 —— 总体 "
                        f"supported:unsupported = {pop['supported']}:"
                        f"{pop['unsupported']}，本表是 "
                        f"{strata.get('supported', 0)}:"
                        f"{strata.get('unsupported', 0)}。κ 要连着这句一起报。"),
        },
        "items": [{"i": i, "scenario": sid, "depth": x["depth"], "idx": x["i"],
                   "text": x["text"], "judge": x["v"],
                   "cite_verified": x.get("cite_verified")}
                  for i, (sid, x) in enumerate(flat, 1)],
    }
    ANNOTATION_KEY.write_text(json.dumps(key, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return ANNOTATION_SHEET, ANNOTATION_KEY


def refresh_annotation_key(rep: dict, report_name: str) -> tuple[int, int, int]:
    """重判之后，把 key 里的判官判定刷成新口径的 —— 人工判定一个字不动。

    改 rubric 会改指纹、让缓存全部失效、整批重判。但**人已经勾过的那些答案还有效**
    （断言没变、证据没变，变的只是判官怎么读它们）。所以 κ 不需要人重勾，只要把
    key 里的 `judge` 换掉，κ 直接重算。

    返回 (判官判定变了的条数, 对上的条数, 总条数)。

    ⚠️ `judge_prev` 写的是「**这一条上一次改判前**的值」，不是「改 rubric 之前的基线」——
    只在【这条只改判过一次】时两者才重合。重判一轮就会刷一次 key，多次刷新会把早先的
    prev 覆盖掉（2026-09-14 实测：拿 judge_prev 复原「旧口径」κ 得到 0.471，而真值
    （从旧报告本身算）是 0.683）。**要历史 κ 请回到那份旧报告本身算，别读 judge_prev。**
    """
    if not ANNOTATION_KEY.exists():
        return 0, 0, 0
    key = json.loads(ANNOTATION_KEY.read_text(encoding="utf-8"))
    det = {(s["scenario_id"], a["i"]): a
           for s in rep.get("scenarios", []) for a in s["assertions"]}
    changed = matched = 0
    for it in key.get("items", []):
        a = det.get((it["scenario"], it["idx"]))
        if a is None or a.get("v") not in TIER2_VERDICTS:
            continue
        matched += 1
        if a["v"] != it.get("judge"):
            it["judge_prev"] = it.get("judge")
            it["judge"] = a["v"]
            changed += 1
        it["cite_verified"] = a.get("cite_verified")
        it["tool_self"] = bool(a.get("tool_self"))
    key["rubric_fingerprint"] = rep["meta"].get("rubric_fingerprint")
    key["source_report"] = report_name
    key["refreshed_at"] = time.strftime("%Y-%m-%d %H:%M")
    ANNOTATION_KEY.write_text(json.dumps(key, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return changed, matched, len(key.get("items", []))


def _parse_annotation_answers() -> dict[int, str]:
    """读回标注表里的人工判定 → {条目号: 判定词}。没填的不进字典。

    行错位容错：答案写在 `**你的判定**：` 的【上一行】时也认。只在冒号后为空时才
    回看上一行，且要求整行就是一个合法判定词 —— 否则会把上一题的答案串过来
    （D 组 #14 踩过）。
    """
    if not ANNOTATION_SHEET.exists():
        return {}
    text = ANNOTATION_SHEET.read_text(encoding="utf-8")
    marks = list(_ITEM_RE.finditer(text))
    out: dict[int, str] = {}
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        body = text[m.end():end]
        am = _ANSWER_RE.search(body)
        if not am:
            continue
        v = _read_verdict(am.group(1))
        if v is None and not am.group(1).strip():
            before = [ln.strip() for ln in body[:am.start()].splitlines() if ln.strip()]
            if before and before[-1].lower() in TIER2_VERDICTS:
                v = before[-1].lower()
        if v is not None:
            out[int(m.group(1))] = v
    return out


def _unreadable_answers() -> list[int]:
    """答案行写了字、但读不出判词 —— 条目号列表。

    静默丢答案会让 κ 少算几条、而且是**非随机**地少算（写得啰嗦的那些）。宁可吵。
    """
    if not ANNOTATION_SHEET.exists():
        return []
    text = ANNOTATION_SHEET.read_text(encoding="utf-8")
    marks = list(_ITEM_RE.finditer(text))
    bad: list[int] = []
    for n, m in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        am = _ANSWER_RE.search(text[m.end():end])
        if am and am.group(1).strip() and _read_verdict(am.group(1)) is None:
            bad.append(int(m.group(1)))
    return bad


def _by(rows: list[dict], field: str) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r[field], []).append(r)
    return out


def judge_human_kappa() -> dict | None:
    """判官 vs 人工的 Cohen's κ —— Tier 2 的校准数字。

    κ 是纯函数：key 里存着判官判定，标注表里是人工判定，两边对齐即可。缓存、模型、
    报告都不参与 —— 换 rubric 之后重判，这张表仍在（只是 `matches_current_rubric`
    会变 false，提示它校准的是【上一版】判官）。
    """
    if not ANNOTATION_KEY.exists():
        return None
    try:
        key = json.loads(ANNOTATION_KEY.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    answers = _parse_annotation_answers()
    rows = [dict(it, human=answers[it["i"]])
            for it in key.get("items", []) if it["i"] in answers]
    if not rows:
        return None

    from eval.metrics import cohen_kappa

    out = dict(cohen_kappa([r["judge"] for r in rows], [r["human"] for r in rows]))
    # 分层是【按判官判定】抽的，所以按判官判定分组的一致率比总体 κ 更好读：
    # 「判官说 unsupported 的 N 条里，人同意几条」。单组只有一种判官值，
    # κ 无定义（pe=1），所以这里只报一致率。
    out["by_judge_verdict"] = {
        v: {
            "n": len(sub),
            "agree": sum(1 for r in sub if r["judge"] == r["human"]),
            "agree_rate": sum(1 for r in sub if r["judge"] == r["human"]) / len(sub),
            "human": {h: sum(1 for r in sub if r["human"] == h)
                      for h in TIER2_VERDICTS},
        }
        for v, sub in sorted(_by(rows, "judge").items())
    }
    out["by_depth"] = {
        d: dict(cohen_kappa([r["judge"] for r in sub], [r["human"] for r in sub]))
        for d, sub in sorted(_by(rows, "depth").items()) if len(sub) >= 2
    }
    cur = _rubric_fingerprint()
    out["annotation"] = {
        "created": key.get("created"),
        "source_report": key.get("source_report"),
        "version": key.get("version"),
        "rubric_fingerprint_key": key.get("rubric_fingerprint"),
        "rubric_fingerprint_current": cur,
        "matches_current_rubric": key.get("rubric_fingerprint") == cur,
        "n_items": len(key.get("items", [])),
        "n_answered": len(answers),
        "n_unreadable": _unreadable_answers(),
        "n_scored": len(rows),
        "strata": (key.get("sample") or {}).get("strata"),
        "population": (key.get("sample") or {}).get("population"),
        "warning": (key.get("sample") or {}).get("warning"),
    }
    return out


def print_kappa(k: dict) -> None:
    a = k["annotation"]
    print(f"\n轴 3 Tier 2 · 判官 vs 人工 Cohen's κ")
    print(f"  标注表 {a['created']} ｜ 共 {a['n_items']} 条，已填 {a['n_answered']}"
          f"，参与计分 {a['n_scored']} 条")
    if a["n_unreadable"]:
        print(f"  ⚠ 有 {len(a['n_unreadable'])} 条的答案读不出判词："
              f"{a['n_unreadable']} —— 这些不计分，去表里把判词写清楚"
              f"（supported / unsupported / unknown）")
    print(f"  判官口径 {a['rubric_fingerprint_key']}"
          + ("" if a["matches_current_rubric"]
             else f"  ⚠ 与当前 {a['rubric_fingerprint_current']} 不一致"
                  f" —— 校的是上一版判官"))
    if k.get("kappa") is None:
        print("  κ = 无定义（两人各自只用了一个标签）")
    else:
        print(f"  κ = {k['kappa']:.3f}  ({k['band']})   "
              f"观察一致 {k['po']:.3f} / 期望一致 {k['pe']:.3f}   n={k['n']}")
    print("\n  按判官判定分组（这才是主指标那一边的可读形式）")
    for v, s in k["by_judge_verdict"].items():
        print(f"    {v:<12} n={s['n']:>3}  人同意 {s['agree']}/{s['n']}"
              f" = {s['agree_rate']:.0%}   人工分布 {s['human']}")
    if k.get("by_depth"):
        print("\n  按 depth")
        for d, s in k["by_depth"].items():
            kk = "无定义" if s["kappa"] is None else f"{s['kappa']:.3f}"
            print(f"    {d:<6} n={s['n']:>3}  κ={kk}")
    print("\n  混淆矩阵（行=判官，列=人工）")
    labels = sorted({*k["matrix"], *(c for row in k["matrix"].values() for c in row)})
    print("           " + "".join(f"{c:>13}" for c in labels))
    for a_ in labels:
        print(f"    {a_:<8}" + "".join(f"{k['matrix'].get(a_, {}).get(b, 0):>13}"
                                       for b in labels))
    print(f"\n  ⚠ {a['warning']}")
    # 抽样时按【当时的】判官判定分层；改 rubric 重判之后判官那一列会变，两者不再相等。
    # 分开报：抽样的那个数是【怎么抽的】，判官那个数是【现在实际长什么样】。
    realized = {v: s["n"] for v, s in k["by_judge_verdict"].items()}
    strata = a.get("strata") or {}
    if strata and any(strata.get(v, 0) != realized.get(v, 0) for v in realized):
        print(f"     （抽样时按旧判官判定分层 = {strata.get('supported', 0)}:"
              f"{strata.get('unsupported', 0)}；重判后实际 = "
              f"{realized.get('supported', 0)}:{realized.get('unsupported', 0)} —— "
              f"抽样比例要连着报）")
    print(f"  判官判定另存 {ANNOTATION_KEY.name}；逐条明细见 source_report")
