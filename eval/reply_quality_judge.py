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
                "cite": "证据里的原文片段；supported 必填，其余留空"}]}

必须覆盖给出的每一条断言，i 用断言编号。不要输出 JSON 以外的任何内容。"""


def _rubric_fingerprint() -> str:
    """判官口径指纹 —— 进缓存键。两段 rubric + 版本 + 单条上限，改一项旧判定全失效。

    证据口径（样本 meta 的 ``policy``）**不在这里**，它在 ``_cache_key`` 里从样本读 ——
    故意不 import ``reply_quality_eval``：那会形成环（对方在 ``main()`` 里 import 本模块），
    而且是把两层耦合起来。判官只需要知道"盘上这份样本是哪个 policy 录的"。
    """
    h = hashlib.sha1((_SPLIT_RUBRIC + _CHECK_RUBRIC).encode("utf-8")).hexdigest()[:8]
    return f"{JUDGE_VERSION}|cap={_MAX_SINGLE_MESSAGE_TOKENS}|{h}"


# ═══════════════════════════════════════════════════════════════════════
# 缓存（append-only，一行一条）
# ═══════════════════════════════════════════════════════════════════════


def _cache_key(stage: str, sample_meta: dict, payload: str) -> str:
    schema = _rubric_fingerprint() + "|" + str((sample_meta or {}).get("policy", "?"))
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
        out[i] = {"v": v, "why": why, "cite": cite}
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
                a["cite_verified"] = _cite_ok(a.get("cite", ""), evidence_text)
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
        "unknown_rate": (t["unknown"] / sum(t.values())) if sum(t.values()) else None,
        "all_assertions_incl_no_evidence": all_c,
        "scenarios_with_unsupported": len(with_unsup),
        "scenarios_with_unsupported_rate": (len(with_unsup) / len(judged)) if judged else None,
        "no_cite_downgrades": sum(s.get("no_cite", 0) for s in scenarios),
        "cite_unverified": sum(1 for s in scenarios for a in s["assertions"]
                               if a["v"] == "supported" and a.get("cite_verified") is False),
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
        print(f"  ⚠️ 判官报 supported 却没给出处被降级的：{o['no_cite_downgrades']} 条；"
              f"出处引了但核不到原文的：{o['cite_unverified']} 条 ——"
              f"两者正常都应恒为 0，不为 0 说明那些 ✓ 不可信。")
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
    print("    · 判 supported 必须报出处（照抄证据原文）；报不出的降级 unknown 并计数。")
    print("      出处还会拿去证据里逐字核（允许用省略号拼两段）；核不到的单独报数 ——")
    print("      那是判官自己的引用错误，不是断言判错，但说明 ✓ 要打折看。")
    print("    · ⚠ 本轴数字在 κ 校准（人工标注 30 条）完成前**不得对外引用** —— "
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
        "另一侧的对照：`supported` 的断言里，出处会被拿去证据里逐字核对（判官描述跨字段的"
        "证据时允许用省略号拼两段）。核不到的单独列出 —— 那是**判官自己的引用错误**，"
        "不是断言判错，但它说明那些 ✓ 要打折看。",
        "\n## 开销\n",
              f"- HTTP {rep['usage']['http_calls']} 次，prompt "
              f"{rep['usage']['prompt_tokens']:,} tok，completion "
              f"{rep['usage']['completion_tokens']:,} tok",
              f"- 判定调用 {o['judge_calls']} 次，缓存命中 {o['cache_hits']} 次，"
              f"耗时 {m['elapsed_s']}s"]

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
