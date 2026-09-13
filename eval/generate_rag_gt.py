"""eval/generate_rag_gt.py — D 组：用「倒着出题 + 正着判」给 RAG 检索造可信 GT

为什么要有 D 组
    轴 2 至今没有任何质量数字，而且是有意的 —— 现有四组 GT 全都不可信：
      A  自测         GT 与检索【共用同一个 WHERE】，分数是构造出来的
      A′ 解析缺口      量的是解析器差距，不是检索质量
      B  精确名称      人工写死 entity id，已经顶在 1.0，没有信息量
      C  池化人工      候选池【由被测检索器自己产生】→ 池化偏差
    四组缺的都是同一件事：**答案不是从检索器里长出来的**。

倒着出题（层 A）
    从库里抽一个实体 → 让 LLM **看着它的资料**、倒着编出一条用户真会问的查询。
    答案因此是客观的（就是出题的那个实体），不可能错，也不需要人工标注。
    直接算生产检索的 Recall@K / MRR —— 回答"系统能不能用一句话找回那部作品"。

    三种风格刻意对着三条通道设计：
      title  口头指代（"排球少年 第二季"）      偏 name / keyword
      tag    标签式（"芳文社的百合动画"，仅 subject） 偏 keyword 的结构化过滤
      plot   剧情式（"四个女孩组乐队的故事"）    偏 vector
    非 subject 没有 tags（实测 character/person 覆盖率 0%），所以「标签式」
    换成「作品式」（"配过《轻音少女》的声优"）—— 按 entity_type 适配。

⚠ 层 A 的固有弱点（必须写进报告，不是 bug）
    单答案 GT：一条查询若有多个合法答案，只有出题实体算命中，其余合法结果被记成
    "没找到"。剧情式尤其容易这样。这一半由【层 B】的相关率补上 —— 层 B 的候选池
    含随机补池，能把"系统没返回"和"系统返回了但不相关"分开。

⚠ 报告头必须写：未经层 B 过滤的初稿数字，不可引用。

用法::

    python -m eval.generate_rag_gt --generate --sample 20   # 试点：出题（花钱）
    python -m eval.generate_rag_gt --generate               # 全量 150 实体 → 450 查询
    python -m eval.generate_rag_gt --evaluate               # 层 A 数字（只花 embedding）
    python -m eval.generate_rag_gt --judge --sample 20      # 层 B 试点：判定（花钱）
    python -m eval.generate_rag_gt --judge                  # 层 B 全量

层 B（正着判）
    层 A 的 GT 是【单答案】的：一条查询若有多个合法答案，只有出题实体算命中，
    其余合法结果被记成"没找到"。层 B 把 GT 从"单答案"扩成"相关集" —— 让 LLM
    判官盲判候选池里每一条是否也算合理答案。候选池三部分，互斥：
      top-20     生产检索返回的（主指标：系统排序质量）
      随机 15    域内随机抽，【排除 top-20 与出题实体】（基线：闭眼乱抓的期望相关率）
      探针        出题实体，若不在上两者中则补进来（只用于层 A×层 B 互验）
    「top-20 相关率 − 随机相关率」= 这个系统相对瞎猜到底强多少，这是轴 2 第一个
    真正意义上的质量数字。判定结果按 (查询, 候选卡片) 落盘缓存 —— 相关性是这两者
    的纯函数，与"候选从哪条通道来"无关，所以将来修完融合只需重判新增的候选。

⚠ 层 B 的数字同样【不可引用】，直到人工标注算出 κ（eval/README.md:483）。

思考：全项目强制关闭（2026-09-13 用户决策，注入点在 `agent/llm.py::_DISABLE_THINKING`）
    `LLM_MODEL` 是推理模型，回包里有独立的 `reasoning_content`，而 `max_tokens`
    **把思考一起算**。实测（d428，同一个 28 张卡的池子）：

      thinking=on   批次 20 → 烧穿 8192 token、content 空串、整批判定丢失
                    批次 10 → 烧穿 4096、同样空串
                    批次 5  → 成功（457 token / 1335 字符思考）
      thinking=off  批次 20 → 一次过，247 token、0 字符思考

    开思考时批次 ≥10 会**非线性爆掉**：候选越多它越要逐条推演，最后把预算烧光。
    关思考便宜约 8 倍且不截断。层 B 曾用它做过 A/B 对照（395 对配对判定，98.5%
    一致；6 处分歧全是 off=false→on=true 且全落在 top-20 桶 —— 即关思考会
    **低估** top-20 相关率，方向上保守而非虚高），随后用户拍板全项目常关、
    开关删除（`--judge-thinking` 已不存在）。

    ⚠ 关思考有**已知代价**，别当它无偏：d177（查「剧场版 魔法少女小圆 [后篇]」→
    候选「魔法少女小圆」）被判成 false，违反 rubric 里"同一系列算相关"这一条。
    这是判官自身的偏差，不靠改 rubric 修（对着单个样本调措辞＝过拟合，这个坑
    在 JUDGE_PACK_THINKING_ON 上已经踩过一次），交给阶段 5 的人工标注 κ 去量。

边界的教训（上一轮踩过）
    `_run_keyword_search` 曾是一份【影子实现】，它和生产漂了才被删（提交 490a3f5）。
    本模块【只 import，不复制】任何检索逻辑 —— 检索一律走 eval.rag_eval 的
    `_production_search`。自检方式：本文件里不应出现任何检索函数的 `def`
    （有一个就说明又造了一份影子实现，它迟早会和生产漂）。

⚠ 别和 `pytest test/` 同时跑
    `test/test_rag.py` 与 `test/test_integration.py` 会通过 `RagEntityIngestor`
    往【同一个库】插测试实体（fixture 里按 id 前缀清）。并发时本模块读到的是
    被污染的库容 —— 实测同一份代码两次运行报出 1490 与 1492 条，随机池因此
    抽到不存在的条目，池子不可复现。判定缓存进键的是卡片文本，污染条目只会
    在缓存里留几条用不上的死记录，不会冒充结论；但**报告里的库容和池子会假**。
    跑评测前先确认没有 pytest 在后台。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EVAL_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = EVAL_DIR / "data" / "rag_gt"
GT_FILE = ARTIFACTS_DIR / "ground_truth_v2_draft.json"
RESULTS_DIR = EVAL_DIR / "results"

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eval")

# ── 抽样默认值 ──
# subject 是主战场（1030/1500），且只有它有 tags（标签式风格的前提）。
# character/person 各 15 条覆盖"查角色/查声优"这条路 —— 样本小，只作方向性读数。
SAMPLE_BASE = {"subject": 120, "character": 15, "person": 15}
DEFAULT_SEED = 13

# subject 内部按 subject_type 比例分层（动画 900 / 书籍 130）
SUBJECT_TYPE_ANIME = 2
SUBJECT_TYPE_BOOK = 1

# ── 检索口径 ──
# ⚠ 与轴 2 其余组的 RETRIEVAL_LIMIT=10【不同】，这里用 20 是为了拿到 Recall@20。
# 这不破坏可比性：keyword 的排序（match_count desc, popularity desc, id）与 limit
# 无关，阶段 2 的补齐量是 `need = limit - stage1_count`，所以 merged 的【前 10 条
# 在 limit=10 与 limit=20 下完全一致】—— @5/@10 仍是同一口径。多出来的只是
# 尾部 10 条，用来回答"到底有没有出现过"。
RETRIEVAL_LIMIT_D = 20
K_VALUES_D = [5, 10, 20]

STYLES = ("title", "tag", "plot")          # subject
STYLES_NON_SUBJECT = ("title", "work", "plot")

# 出题素材的截断长度 —— 太长会被标题本身淹没，太短则剧情式写不出来
SUMMARY_CHARS = 220
MAX_TAGS_IN_PROMPT = 8
MAX_WORKS_IN_PROMPT = 6

# 抽样的最低素材门槛：没有简介就出不了剧情式查询，抽进来只会产出废题
MIN_SUMMARY_CHARS = 40


# ═══════════════════════════════════════════════════════════════════════
# 通用
# ═══════════════════════════════════════════════════════════════════════


def _git_hash() -> str:
    """本模块与 report 命名要用。

    eval/ 没有共享工具模块（metrics.py 只放纯指标函数），按仓库惯例再抄一份 ——
    reply_quality_eval.py:70 / graph_smoke.py 各有一份，三份并存是既定事实。
    """
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=5,
        ).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def _settings_snapshot() -> dict:
    """记 settings 快照 —— 这些值不在 git 里，只记 githash 复现不了一次实验。"""
    try:
        from core.config import get_settings

        s = get_settings()
        return {
            "LLM_MODEL": s.LLM_MODEL,
            "LLM_TEMPERATURE": s.LLM_TEMPERATURE,
            "EMBEDDING_MODEL": s.EMBEDDING_MODEL,
        }
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


# 本次进程的 token 累计 —— 成本外推要实测值，不能靠估。计的是【HTTP 调用】：
# 被截断成空串的那次也烧了 token，只是没产出，漏计它会把成本算低。
# （所以这个 calls 与报告里的 n_calls 不是一回事：后者数的是"批次尝试"。）
USAGE = {"http_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}


def call_llm(client: OpenAI, model: str, system: str, user: str,
             n_retry: int = 2, temperature: float = 0.8,
             max_tokens: int = 2048) -> dict:
    """调用 LLM 并解析 JSON 对象，失败重试。

    照抄 eval/generate_intent_data.py:91 的范式（JSON mode + n_retry + sleep(2)），
    它是全 eval 目录唯一的重试实现。**不抛异常**是仓库规则 1 —— 但这里是【生成侧】，
    返回空 dict 会让调用方把该实体记成"出题失败"并继续，不会污染已有产出。

    max_tokens 可调是层 B 加的：判一池候选的输出比出题长得多，2048 会被截断，
    而 JSON mode 下截断的返回是**空串**（实测 finish_reason=length，content 长度 0），
    不报错、只是静默丢光整批判定。

    **思考一律关闭**（项目决定，2026-09-13）。`LLM_MODEL` 是推理模型，返回里有个独立的
    `reasoning_content` 字段，且 `max_tokens` **把思考一起算** —— 判定任务实测：开思考
    时批次 ≥10 会陷入长推理，烧穿 max_tokens 后 content 是空串（详见 _judge_batch）。
    这里不走 `agent.llm.create_llm`（eval 用自己的裸 OpenAI client），所以工厂那层的
    默认值管不到它，得显式传。
    """
    last_err: Exception | None = None
    for _ in range(n_retry):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            if resp.usage:  # 先记账再解析：解析失败的那次也已经付过钱了
                USAGE["http_calls"] += 1
                USAGE["prompt_tokens"] += resp.usage.prompt_tokens or 0
                USAGE["completion_tokens"] += resp.usage.completion_tokens or 0
            content = resp.choices[0].message.content or ""
            return json.loads(content)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(2)
    logger.warning("LLM 调用失败（重试 %d 次后放弃）: %s", n_retry, last_err)
    return {}


# ═══════════════════════════════════════════════════════════════════════
# 抽样
# ═══════════════════════════════════════════════════════════════════════


def _entity_record(e) -> dict:
    """把 RagEntity 摊平成出题要用的素材（从 rag_output 读，它是最全的富化记录）。

    为什么不从 meta_info 读：`works` 只在 rag_output 里（character 的 meta_info
    键是 casts/collects/info/role/summary，没有 works），而作品式风格要靠它。
    """
    try:
        rich = json.loads(e.rag_output)
    except (json.JSONDecodeError, TypeError):
        rich = {}
    return {
        "id": e.id,
        "entity_type": e.entity_type,
        "subject_type": e.subject_type,
        "name": e.name or "",
        "name_cn": e.name_cn or "",
        "popularity": e.popularity,
        "tags": [t.get("name") for t in (rich.get("tags") or []) if t.get("name")],
        "works": [w.get("subject_name") for w in (rich.get("works") or [])
                  if w.get("subject_name")],
        "summary": (rich.get("summary") or "").strip(),
        "role": rich.get("role") or "",
        "career": rich.get("career") or [],
    }


def _scale_counts(total: int) -> dict[str, int]:
    """把 --sample N 按 SAMPLE_BASE 的比例摊到三种 entity_type 上。

    每种至少 1 条 —— 试点时把 character/person 缩成 0 就等于没验证那两条分支。
    """
    base_total = sum(SAMPLE_BASE.values())
    if total >= base_total:
        return dict(SAMPLE_BASE)
    out: dict[str, int] = {}
    for k, v in SAMPLE_BASE.items():
        out[k] = max(1, round(v * total / base_total))
    # 四舍五入的零头挂到 subject 上（它是主组，多一条少一条不影响结论）
    drift = total - sum(out.values())
    out["subject"] = max(1, out["subject"] + drift)
    return out


def sample_entities(total: Optional[int] = None, seed: int = DEFAULT_SEED) -> list[dict]:
    """分层抽实体。返回 [_entity_record, ...]（已过滤掉出不了题的）。

    两道硬过滤，都是实测出来的：
      · nsfw=False —— 生产检索 exclude_nsfw=True，抽到 nsfw 实体等于出一道
        【系统永远找不到】的题，那不是检索质量问题，是前提被污染。
        （实测 subject 全为 False；nsfw=true 只出现在 person，共 10 条）
      · 简介 >= MIN_SUMMARY_CHARS —— 剧情式查询是从简介反推的，没有简介就没题

    非 subject 额外要求 works 里有【本库内】的作品名：作品式查询靠它指代，
    引用一个库里没有的作品等于把题出到闭世界之外。代价是池子变小
    （实测 character 220/349、person 24/110），报告里要声明。
    """
    # 顺序不能反：_silence_sqlalchemy 内部先 import database.engine，
    # 否则 InstanceLogger 的 handler 会挂回来（见该函数 docstring）。
    from eval.rag_eval import _silence_sqlalchemy
    _silence_sqlalchemy()

    from sqlmodel import Session, select

    from database.engine import engine
    from database.rag_tables import RagEntity

    want = _scale_counts(total) if total else dict(SAMPLE_BASE)
    rng = random.Random(seed)

    with Session(engine) as session:
        rows = session.exec(
            select(RagEntity).where(RagEntity.nsfw == False)  # noqa: E712
        ).all()

    recs = [_entity_record(e) for e in rows]
    corpus_names = {r["name"] for r in recs if r["entity_type"] == "subject"}
    corpus_names |= {r["name_cn"] for r in recs if r["entity_type"] == "subject"}
    corpus_names.discard("")

    def _usable(r: dict) -> bool:
        if len(r["summary"]) < MIN_SUMMARY_CHARS:
            return False
        if r["entity_type"] == "subject":
            return bool(r["tags"])
        return any(w in corpus_names for w in r["works"])

    picked: list[dict] = []

    # subject：按 subject_type 比例分层（这是唯一的受控维度，popularity 留给报告读）
    subs = [r for r in recs if r["entity_type"] == "subject" and _usable(r)]
    anime = [r for r in subs if r["subject_type"] == SUBJECT_TYPE_ANIME]
    books = [r for r in subs if r["subject_type"] == SUBJECT_TYPE_BOOK]
    n_sub = want["subject"]
    n_anime = min(len(anime), round(n_sub * len(anime) / max(1, len(anime) + len(books))))
    n_book = min(len(books), n_sub - n_anime)
    picked += rng.sample(anime, n_anime)
    picked += rng.sample(books, n_book)

    for et in ("character", "person"):
        pool = [r for r in recs if r["entity_type"] == et and _usable(r)]
        picked += rng.sample(pool, min(want[et], len(pool)))

    return picked


# ═══════════════════════════════════════════════════════════════════════
# 出题（层 A）
# ═══════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = (
    "你是 Bangumi（动画评分社区）的用户查询生成器。你模拟的是【真实用户会怎么问】，"
    "不是出题人会怎么问 —— 查询要口语、自然、像随手发的一条消息。\n"
    "输出必须是可以解析为 {\"queries\": [...]} 的 JSON，不要输出任何其他文字。"
)


def build_prompt(rec: dict) -> str:
    """一个实体 → 三条不同风格的查询。

    三种风格刻意对着三条检索通道（见模块 docstring）。两条最要紧的约束：
      · 剧情式【不许出现作品名/角色名/人名】—— 否则它退化成标题式，
        三种风格会塌成同一条通道，那这套设计就没有意义了。
      · 每条都必须【能唯一指向这个条目】—— 太泛的查询会匹配几十个实体，
        单答案 GT 会把那些合法结果全记成"没找到"，那不叫难，叫出题坏了。
    """
    is_subject = rec["entity_type"] == "subject"
    lines = [
        "=== 条目资料 ===",
        f"名称（原名）: {rec['name']}",
        f"名称（中文）: {rec['name_cn'] or '（无）'}",
    ]
    if is_subject:
        lines.append(f"标签: {'、'.join(rec['tags'][:MAX_TAGS_IN_PROMPT]) or '（无）'}")
    else:
        kind = "角色" if rec["entity_type"] == "character" else "人物"
        lines.append(f"类型: {kind}")
        if rec["role"]:
            lines.append(f"定位: {rec['role']}")
        if rec["career"]:
            lines.append(f"职业: {'、'.join(rec['career'])}")
        lines.append(f"参与作品: {'、'.join(rec['works'][:MAX_WORKS_IN_PROMPT]) or '（无）'}")
    lines += [
        f"简介: {rec['summary'][:SUMMARY_CHARS]}",
        "",
        "=== 任务 ===",
        "生成 3 条查询，风格必须不同。每条 5–30 字，像真人在 Bangumi 上随手问的。",
        "",
    ]

    if is_subject:
        lines += [
            "【A · 口头指代】一个看过它的人，想把它指给朋友时会怎么称呼它？",
            "  写成**简短的指代**（5–15 字），像在搜索框里输入的，**不要加感想或评论**。",
            "  允许用简称、中文译名、日文原名、带季数（如「第二季」）、记错一个字。",
            "  不要加书名号，不要写成「我想找一部……」。",
            "  例：「排球少年 第二季」「芙莉莲」「EVA 新剧场版」",
            "",
            "【B · 标签式】用它的【标签组合】来描述它，让别人能猜到是哪一部（用 2–4 个标签）。"
            "**不要出现作品名**。",
            "  例：「芳文社的百合动画」「2011年的科幻神作」",
            "",
            "【C · 剧情式】只根据上面的简介，用一句大白话描述「我想找一部讲XX的作品」。",
            "  **不要出现作品名、角色名、人名、社团名**。",
            "  例：「四个女孩组乐队、主唱有社交障碍的日常动画」",
        ]
        styles = STYLES
    else:
        noun = "角色" if rec["entity_type"] == "character" else "人物"
        lines += [
            f"【A · 口头指代】一个认识这个{noun}的人，想把它指给朋友时会怎么称呼TA？",
            "  写成**简短的指代**（3–15 字），**不要加感想或评论**。",
            "  允许用简称、昵称、译名、记错一个字。**不要加书名号**。",
            "",
            "【B · 作品式】用TA【参与过的作品】来指代TA，让别人能猜到是谁。"
            "**不要出现TA的名字**。",
            "  例：「《化物语》里那个毒舌的女主角」「配过《轻音少女》的声优」",
            "",
            f"【C · 剧情式】只根据上面的简介，用一句大白话描述这个{noun}的特点。",
            "  **不要出现TA的名字，也不要出现作品名**。",
        ]
        styles = STYLES_NON_SUBJECT

    lines += [
        "",
        "输出 JSON（style 字段固定用下面的英文名，顺序不变）:",
        '{"queries": [{"style": "%s", "query": "..."}, '
        '{"style": "%s", "query": "..."}, {"style": "%s", "query": "..."}]}'
        % styles,
    ]
    return "\n".join(lines)


def _clean_queries(raw: dict, styles: tuple[str, ...]) -> dict[str, str]:
    """从 LLM 返回里取三条查询，按 style 对齐。缺哪条就少哪条（调用方记失败）。"""
    out: dict[str, str] = {}
    for item in (raw.get("queries") or []):
        style = str(item.get("style", "")).strip()
        text = str(item.get("query", "")).strip()
        if style in styles and text and style not in out:
            out[style] = text
    return out


def cmd_generate(args) -> int:
    settings = _settings_snapshot()
    from core.config import get_settings

    cfg = get_settings()

    done: dict[str, dict] = {}
    if GT_FILE.exists():
        try:
            prev = json.loads(GT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}
        if prev.get("status") == "draft_unfiltered" and not args.force:
            # 防呆，沿用 rag_eval.py:516 的规矩：绝不静默抹掉已有产出。
            print(f"  ⚠ {GT_FILE.name} 已是一份完整草稿（{len(prev.get('queries', []))} 条查询）。")
            print("    重跑会重新出题（花钱）且覆盖现有内容。确实要重跑：加 --force。")
            return 1
        if prev.get("status") == "generating" and not args.force:
            for q in prev.get("queries", []):
                done.setdefault(q["gt_entity_id"], {})[q["style"]] = q["query"]
            print(f"  ↻ 检测到未完成的草稿，断点续跑：已完成 {len(done)} 个实体")

    entities = sample_entities(total=args.sample or None, seed=args.seed)
    print(f"抽样 {len(entities)} 个实体（seed={args.seed}）")
    by_type: dict[str, int] = {}
    for e in entities:
        by_type[e["entity_type"]] = by_type.get(e["entity_type"], 0) + 1
    print(f"  构成: {by_type}")

    client = OpenAI(api_key=cfg.LLM_API_KEY, base_url=cfg.LLM_BASE_URL or None)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    queries: list[dict] = []
    failed: list[str] = []
    n_calls = 0

    def _flush(status: str) -> None:
        GT_FILE.write_text(json.dumps({
            "version": "v2-draft",
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "status": status,
            "description": (
                "D 组 GT（倒着出题）：从库里抽实体 → LLM 看着资料反编用户查询 → "
                "答案即出题实体。层 A 数字为未经层 B 过滤的初稿，不可引用。"
            ),
            "seed": args.seed,
            "retrieval_limit": RETRIEVAL_LIMIT_D,
            "k_values": K_VALUES_D,
            "settings": settings,
            "queries": queries,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    for i, rec in enumerate(entities, 1):
        styles = STYLES if rec["entity_type"] == "subject" else STYLES_NON_SUBJECT
        cached = done.get(rec["id"], {})
        got = {s: cached[s] for s in styles if s in cached}

        if len(got) < len(styles):
            n_calls += 1
            raw = call_llm(client, cfg.LLM_MODEL, SYSTEM_PROMPT, build_prompt(rec))
            got.update({s: t for s, t in _clean_queries(raw, styles).items()
                        if s not in got})

        if not got:
            failed.append(f"{rec['id']} ({rec['name_cn'] or rec['name']})")
            print(f"  [{i}/{len(entities)}] ✗ {rec['id']} 出题失败")
            continue

        for style in styles:
            if style not in got:
                continue
            queries.append({
                "id": f"d{len(queries) + 1:03d}",
                "entity_type": rec["entity_type"],
                "subject_type": rec["subject_type"],
                "style": style,
                "query": got[style],
                "gt_entity_id": rec["id"],
                "gt_name": rec["name"],
                "gt_name_cn": rec["name_cn"],
                "gt_popularity": rec["popularity"],
            })

        _flush("generating")
        if i % 10 == 0 or i == len(entities):
            print(f"  [{i}/{len(entities)}] 已出 {len(queries)} 条查询")

    _flush("draft_unfiltered")
    print()
    print("=" * 64)
    print(f"  出题完成：{len(queries)} 条查询 / {len(entities)} 个实体"
          f"（LLM 调用 {n_calls} 次，失败 {len(failed)} 个实体）")
    print(f"  → {GT_FILE}")
    if failed:
        print(f"  ⚠ 失败实体（下次 --generate 会自动补）: {', '.join(failed[:5])}"
              + (" …" if len(failed) > 5 else ""))
    print("=" * 64)
    return 0 if queries else 1


# ═══════════════════════════════════════════════════════════════════════
# 层 A 指标
# ═══════════════════════════════════════════════════════════════════════


def _load_gt() -> dict:
    if not GT_FILE.exists():
        raise SystemExit(f"{GT_FILE} 不存在 —— 先跑 --generate")
    return json.loads(GT_FILE.read_text(encoding="utf-8"))


def _title_is_tag(q: dict, tags_by_id: dict[str, set[str]]) -> bool:
    """「口头指代」那条查询，标题是否恰好是该条目自己的标签？

    分层报的理由（实测）：标题恰好是标签时 Hit@1 是 100%，不是时只有 46%。
    两类混在一起会把两层的信息互相稀释 —— 前者是 B 组那种"顶在 1.0 没信息量"，
    后者才是真信号。
    """
    tags = tags_by_id.get(q["gt_entity_id"], set())
    return (q["gt_name"] in tags) or bool(q["gt_name_cn"] and q["gt_name_cn"] in tags)


def cmd_evaluate(args) -> int:
    from eval.rag_eval import (
        _compute_metrics, _embedding_canary, _get_retriever, _production_search,
        _silence_sqlalchemy,
    )

    _silence_sqlalchemy()

    print("=" * 64)
    print("  D 组 · 层 A（倒着出题）—— 生产检索命中率")
    print("=" * 64)

    err = _embedding_canary()
    if err:
        print(f"  ✗ embedding 探活失败，数字不可信：{err}")
        return 1
    print("  ✓ embedding 探活通过")

    gt = _load_gt()
    queries = gt.get("queries", [])
    if not queries:
        print("  ✗ 草稿里没有查询")
        return 1
    if gt.get("status") != "draft_unfiltered" and not args.allow_partial:
        print(f"  ⚠ 草稿状态是 {gt.get('status')!r}（可能是断点续跑的中间态）。")
        print("    要按部分样本出数：加 --allow-partial。")
        return 1

    from sqlmodel import Session, select

    from database.engine import engine
    from database.rag_tables import RagEntity

    with Session(engine) as session:
        tags_by_id = {
            e.id: {t.get("name") for t in ((e.meta_info or {}).get("tags") or [])}
            for e in session.exec(
                select(RagEntity).where(RagEntity.entity_type == "subject")
            ).all()
        }

    retr = _get_retriever()
    results: list[dict] = []
    t0 = time.time()
    for i, q in enumerate(queries, 1):
        try:
            r = _production_search(q, retr, limit=RETRIEVAL_LIMIT_D)
        except Exception as exc:  # 规则 1：不抛异常，记成错误继续
            results.append({**q, "error": f"{type(exc).__name__}: {exc}", "metrics": {}})
            continue

        merged = r["merged"]
        m = _compute_metrics(merged, {q["gt_entity_id"]}, k_values=K_VALUES_D)
        rank = merged.index(q["gt_entity_id"]) + 1 if q["gt_entity_id"] in merged else 0

        # 谁把闸门关上的：按融合顺序累加各通道的去重产出，第一个推到 limit 的通道
        # 就是短路元凶。这决定了「该修哪条通道」，比单纯知道"vector 没跑"有用。
        blocked_by = None
        if not r["invoked"]["vector"]:
            seen: set[str] = set()
            for ch in ("keyword", "name"):
                if r["invoked"].get(ch):
                    seen.update(r["channel_ids"].get(ch) or [])
                    if len(seen) >= RETRIEVAL_LIMIT_D:
                        blocked_by = ch
                        break

        results.append({
            **q,
            "metrics": m,
            "rank": rank,
            "n_returned": len(merged),
            "invoked": r["invoked"],
            "shortcircuit_by": blocked_by,
            "rescued_rank": None,  # --rescue 填；0=试了没救回
            "stages": r["stages"],
            "errors": r["errors"],
            "keyword_filters": r["keyword_filters"],
            # 只对 subject 算 —— tags 只在 subject 上有（character/person 覆盖率 0%），
            # 对非 subject 算会恒为 False，把"标题不是标签"那一桶污染掉。
            "title_is_tag": (
                _title_is_tag(q, tags_by_id)
                if q["style"] == "title" and q["entity_type"] == "subject" else None
            ),
        })
        if i % 50 == 0 or i == len(queries):
            print(f"  [{i}/{len(queries)}] 已跑 {time.time() - t0:.0f}s")

    rescue = None
    if args.rescue:
        print("\n  ── 救援实验：把被短路的漏检单独喂给语义通道 ──")
        rescue = _rescue_pass(results, retr)
        if rescue:
            print(f"    被短路且漏检 {rescue['n_blocked']} 条 → 救回 "
                  f"{rescue['n_rescued']} 条 ({rescue['rate']:.0%})")

    report = _build_report(gt, results, time.time() - t0, rescue)
    _print_report(report)
    if not args.no_save:
        json_path, md_path = _save(report)
        print(f"\n  → {json_path}\n  → {md_path}")
    return 0


def _rescue_pass(results: list[dict], retr) -> Optional[dict]:
    """救援实验 —— 本报告唯一的【因果】证据，不是相关性证据。

    交叉制表只能证明「短路」和「找不到」同时发生：短路【是因为】keyword 凑满了
    limit，而"什么查询会让 keyword 凑满"本身就和查询类型相关，两者共享一个原因。
    只有把被短路的那批查询单独喂给语义通道，才能回答「若它跑了，找得到吗」。

    ★ 成立性前提（改这里之前先回去读 tools/bgm_tools.py:1108）：
      `_unified_search` 调 vector 通道时【不传 keyword_filters】，参数只有
      query / entity_type / subject_type / limit / exclude_nsfw。所以这里用同一组
      参数单跑，就是生产【本来会发出的】那次调用，而不是一个更宽松的替身。
      若哪天生产开始给 vector 传标签（让它也受那些坏标签约束），这个实验立刻
      失效 —— 那时必须把 keyword_filters 一起传进来，否则救援率会被高估。

    返回 None 表示没有被短路的漏检可救。副作用：给被救援的行写 rescued_rank。
    """
    blocked = [r for r in results
               if not r.get("error") and r.get("rank") == 0
               and (r.get("invoked") or {}).get("vector") is False]
    if not blocked:
        return None

    for r in blocked:
        try:
            hits = retr.hybrid_search(
                query=r["query"],
                entity_type=r["entity_type"],
                subject_type=r.get("subject_type"),
                limit=RETRIEVAL_LIMIT_D,
                exclude_nsfw=True,
            )
            ids = [h.entity_id for h in hits]
            r["rescued_rank"] = (
                ids.index(r["gt_entity_id"]) + 1 if r["gt_entity_id"] in ids else 0
            )
        except Exception as exc:  # 规则 1：不抛异常，记下来继续
            r["rescued_rank"] = None
            r["rescue_error"] = f"{type(exc).__name__}: {exc}"

    ok = [r for r in blocked if isinstance(r.get("rescued_rank"), int)]
    rescued = [r for r in ok if r["rescued_rank"] > 0]

    # 反事实投影：把这批救回的命中算进去，整体 Recall@K 会变成多少。
    # 单答案 GT ⇒ 每条查询的 Recall@k 是 0/1，所以增益 = 救回条数 / 总条数。
    clean = [r for r in results if not r.get("error")]
    n = max(len(clean), 1)
    proj: dict[str, dict] = {}
    for k in K_VALUES_D:
        now = statistics.fmean([r["metrics"].get(f"Recall@{k}", 0.0) for r in clean])
        gain = sum(1 for r in rescued if r["rescued_rank"] <= k)
        proj[f"Recall@{k}"] = {
            "now": round(now, 4),
            "if_rescued": round(now + gain / n, 4),
            "gain_pp": round(100 * gain / n, 1),
        }

    by_style: dict[str, dict] = {}
    for style in STYLES + ("work",):
        sel = [r for r in ok if r["style"] == style]
        if sel:
            rn = sum(1 for r in sel if r["rescued_rank"] > 0)
            by_style[style] = {
                "n_blocked": len(sel), "n_rescued": rn,
                "rate": round(rn / len(sel), 4),
            }

    by_blocker: dict[str, int] = {}
    for r in blocked:
        b = r.get("shortcircuit_by") or "unknown"
        by_blocker[b] = by_blocker.get(b, 0) + 1

    errs = [r for r in blocked if r.get("rescue_error")]
    return {
        "n_blocked": len(ok),
        "n_rescued": len(rescued),
        "rate": round(len(rescued) / max(len(ok), 1), 4),
        "by_style": by_style,
        "by_blocker": by_blocker,
        "projected_recall": proj,
        "n_error": len(errs),
        "method": ("对每条【漏检且 vector 未跑】的查询，用与 _unified_search 相同的参数"
                   "单独调用 hybrid_search（不传 keyword_filters），记录真身排名。"
                   "这不是生产行为，是反事实：回答「闸门没关的话会怎样」。"),
    }


def _failure_paths(rows: list[dict]) -> dict:
    """把「被短路的漏检」拆成两条路径 —— 它们的修法不同，混报会选错药。

    路径 A：阶段 1 自己就满页（标签太泛，如"动画"匹配上千条）→ 按热度取前 20
    路径 B：阶段 1 的 AND 落空 → 阶段 2 软兜底补满 → 沾边的 20 条

    判据用 stages.stage2：>0 说明靠填充补齐（B），==0 说明阶段 1 独自满页（A）。
    """
    blocked = [r for r in rows
               if not r.get("error") and r.get("rank") == 0
               and (r.get("invoked") or {}).get("vector") is False]
    total = max(len(blocked), 1)

    def summarize(sel: list[dict]) -> dict:
        tags = [len((r.get("keyword_filters") or {}).get("required_tags") or [])
                for r in sel]
        return {
            "n": len(sel),
            "share": round(len(sel) / total, 4),
            "mean_tags": round(statistics.fmean(tags), 2) if tags else 0.0,
        }

    return {
        "n_blocked": len(blocked),
        "A_stage1_filled": summarize(
            [r for r in blocked if (r.get("stages") or {}).get("stage2", 0) == 0]),
        "B_stage2_padded": summarize(
            [r for r in blocked if (r.get("stages") or {}).get("stage2", 0) > 0]),
    }


# 关于代码结构的静态事实 —— 不随数据变，所以是常量而不是算出来的。
# 只在真正重新验证过之后才改这里（每条都标了出处）。
ROOT_CAUSE_NOTES = [
    "## 根因：keyword 用「过滤」的排序冒充「排序」",
    "",
    "keyword 通道的排序键是 `popularity desc` —— 一个与查询无关的量。它提供的是"
    "**过滤**（哪些条目带这个标签），不是**排序**（哪条最贴合用户想找的）。"
    "而融合层把它当成排序通道用：通道优先级拼接 + 截断，keyword 排在最前面占满位置。",
    "",
    "### 为什么「只去掉闸门」不够（反事实证明）",
    "",
    "把 `len(merged) < limit` 这个闸门直接删掉是**负收益**：`_unified_search` 最后是 "
    "`return merged[:limit]`，keyword 已经占满 20 个位置，name/vector 的结果被整个截掉。"
    "实测（d018）：去掉闸门后真身**仍未进榜**，只是白花了一次 embedding。",
    "",
    "同理，「填充项垫底」对路径 A 也无效 —— 路径 A 那 20 条是阶段 1 的"
    "「真命中」（`final_score=0.0`），一个填充项都没有，垫底无从谈起。",
    "",
    "### 这个仓库本来就有正确的设计，2026-08-13 被换掉了",
    "",
    "`rag/retriever.py:693` 起注释掉的旧 `hybrid_search`，签名里有 `required_tags`，"
    "docstring 写得分明：**SQL 硬过滤（tags）→ query 向量化 → 硬过滤后的子集内按余弦"
    "距离召回 → 语义阶梯分桶排序**。那就是「keyword 选候选域，vector 排域内序」。",
    "",
    "2026-08-13 `f83f4ad`（三通道接入生产工具）之后，排序信号从「语义距离」退化成了"
    "「通道归属 + 热度」。**三通道本身不是错** —— `name_search` 处理跨语言标题"
    "（「進撃の巨人」↔「进击的巨人」）是向量未必替代得了的。错的是融合方式。",
    "",
    "### 缺的仪表",
    "",
    "判路径 A 需要一个现在还拿不到的数：**标签的选择性**。`rag/retriever.py` 里是 "
    "`stmt.limit(limit)` **之后**才 `stage1_count = len(results)`，所以"
    "「20 条真命中」既可能是「库里只有 25 条」，也可能是「库里有 1000 条」——"
    "两者含义完全相反，现有仪表区分不了。要先加一个不被截断的 `COUNT(*)`。",
    "",
    "### 修法方向（未实施 —— 需层 B 判官集才能验证）",
    "",
    "1. **让 keyword 只提供候选域，排序交给向量距离**（≈ 恢复旧设计）。覆盖两条路径："
    "泛标签时在上千候选里按语义排；AND 落空时退化为全域向量召回。成本 1 次 "
    "query embedding + 1 次 SQL。",
    "2. **保守版**：保留三通道，只把融合从「通道优先级拼接」改成「按名次融合 + 填充垫底」。"
    "改动集中在一处，但只覆盖路径 B（78%）。",
    "",
    "⚠ **层 A 验证不了修法好坏**：它只回答「答案在不在候选池里」，回答不了"
    "「池子里其余 19 条是否更相关」。把排序全换成「语义最近」可能让一批查询变差，"
    "而 R@10 反而上升 —— 这需要判官集来判相关性。",
]


def _agg(rows: list[dict]) -> dict:
    """一组的均值。Recall@K 是单答案 GT，所以它等价于 Hit@K —— 两者都留，
    因为 Hit@K 是更常用的读法，而 Recall 是本仓库其余组的既有口径。"""
    ok = [r for r in rows if not r.get("error")]
    if not ok:
        return {"n": 0}

    def mean(metric: str) -> float:
        vals = [r["metrics"].get(metric) for r in ok if r.get("metrics")]
        return round(statistics.fmean(vals), 4) if vals else 0.0

    out = {"n": len(ok)}
    for k in K_VALUES_D:
        out[f"Recall@{k}"] = mean(f"Recall@{k}")
        out[f"Hit@{k}"] = mean(f"Hit@{k}")
    out["MRR"] = mean("MRR")
    return out


def _channel_profile(rows: list[dict]) -> dict[str, int]:
    """通道实际被调用的分布。

    生产融合是【短路】的：keyword 凑满 limit 就不跑 name / vector
    （tools/bgm_tools.py:1091/1108），而 keyword 的阶段 2 软兜底按设计总把结果
    补到 limit。所以"vector 没跑"直接意味着"这条查询没经过语义通道"——
    那是架构结论，不该被融合后的单一数字盖住。
    """
    prof: dict[str, int] = {}
    for r in rows:
        inv = r.get("invoked") or {}
        if not inv:
            continue
        key = "+".join(c for c in ("keyword", "name", "vector") if inv.get(c)) or "none"
        prof[key] = prof.get(key, 0) + 1
    return dict(sorted(prof.items(), key=lambda x: -x[1]))


def _shortcircuit_split(rows: list[dict]) -> dict[str, dict]:
    """按「vector 有没有被短路」分组看命中率。零额外成本 —— invoked 已在返回值里。

    ⚠ 读法：这不是干净的因果实验。短路【是因为】keyword 凑满了 limit，而
    "什么查询会让 keyword 凑满"本身就和查询类型相关。它证明的是"两件事同时发生"，
    "短路害的"这一步由报告里的「救援实验」一节负责（需 --evaluate --rescue）。
    """
    out: dict[str, dict] = {}
    for label, want in (("vector 被短路（没跑）", False), ("vector 跑了", True)):
        sel = [r for r in rows
               if not r.get("error") and (r.get("invoked") or {}).get("vector") is want]
        out[label] = _agg(sel)
    return out


def _build_report(gt: dict, results: list[dict], elapsed: float,
                  rescue: Optional[dict] = None) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in results:
        groups.setdefault(f"{r['entity_type']}·{r['style']}", []).append(r)

    table = {name: _agg(rows) for name, rows in sorted(groups.items())}

    # 分层诊断：口头指代那条，标题恰好是标签 vs 不是
    title_rows = [r for r in results
                  if r["style"] == "title" and r["entity_type"] == "subject"]
    title_split = {
        "标题恰好是标签": _agg([r for r in title_rows if r["title_is_tag"]]),
        "标题不是标签": _agg([r for r in title_rows if r["title_is_tag"] is False]),
    }

    misses = [r for r in results if not r.get("error") and r.get("rank") == 0]

    return {
        "name": "rag_gt_layer_a",
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "git": _git_hash(),
        "gt_file": GT_FILE.name,
        "gt_status": gt.get("status"),
        "retrieval_limit": RETRIEVAL_LIMIT_D,
        "k_values": K_VALUES_D,
        "settings": gt.get("settings") or _settings_snapshot(),
        "elapsed_s": round(elapsed, 1),
        "n_queries": len(results),
        "table": table,
        "title_split": title_split,
        "shortcircuit_split": _shortcircuit_split(results),
        "channel_profile": _channel_profile(results),
        "failure_paths": _failure_paths(results),
        "root_cause_notes": ROOT_CAUSE_NOTES,
        "rescue": rescue,
        # 逐条明细 —— 报告自证用，阶段 2 建候选池也从这里读
        "rows": results,
        "n_error": sum(1 for r in results if r.get("error")),
        "n_miss": len(misses),
        "miss_examples": [
            {"id": r["id"], "style": r["style"], "query": r["query"],
             "gt": r["gt_name_cn"] or r["gt_name"],
             "keyword_filters": r.get("keyword_filters"),
             "invoked": r.get("invoked"), "stages": r.get("stages")}
            for r in misses[:15]
        ],
        "known_limits": [
            "⚠ 未经层 B 过滤的初稿数字，不可引用。",
            "单答案 GT：一条查询若有多个合法答案，只有出题实体算命中，其余合法结果被记成没找到 —— 由层 B 的相关率补上。",
            "查询是【倒着编的】（看着条目资料反推），可能不像真人会问。",
            "答案闭世界：只在本地 1500 条实体库内，库里没有的作品一律算没找到。",
            "非 subject 的抽样池受限（作品式要求 works 含本库内作品）：character 220/349、person 24/110，代表性有限。",
            "judge 与被评模型同源（单 provider，LLM_MODEL 单值），层 B 的自偏好偏差只能声明不能消除。",
        ],
    }


def _print_report(rep: dict) -> None:
    print()
    print(f"  查询 {rep['n_queries']} 条 | 耗时 {rep['elapsed_s']}s | "
          f"检索 limit={rep['retrieval_limit']} | 出错 {rep['n_error']}")
    print()
    print("  ⚠ 未经层 B 过滤的初稿数字，不可引用")
    print()
    head = f"  {'组':22} {'n':>4} {'R@5':>7} {'R@10':>7} {'R@20':>7} {'MRR':>7}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for name, v in rep["table"].items():
        if not v.get("n"):
            continue
        print(f"  {name:22} {v['n']:>4} {v['Recall@5']:>7.3f} {v['Recall@10']:>7.3f} "
              f"{v['Recall@20']:>7.3f} {v['MRR']:>7.3f}")
    print()
    print("  分层诊断 · 口头指代（标题是否恰好是该条目的标签）:")
    for name, v in rep["title_split"].items():
        if v.get("n"):
            print(f"    {name:16} n={v['n']:>3}  R@10={v['Recall@10']:.3f}  MRR={v['MRR']:.3f}")
    print()
    print("  交叉制表 · vector 有没有被短路:")
    for name, v in rep["shortcircuit_split"].items():
        if v.get("n"):
            print(f"    {name:22} n={v['n']:>3}  R@10={v['Recall@10']:.3f}  MRR={v['MRR']:.3f}")
    print()
    print("  通道调用分布（生产融合是短路的：keyword 满 limit 就不跑 name/vector）:")
    for k, v in rep["channel_profile"].items():
        print(f"    {k:22} {v:>4}  ({v / max(1, rep['n_queries']):.0%})")
    fp = rep.get("failure_paths") or {}
    if fp.get("n_blocked"):
        print()
        print(f"  漏因分型（被短路的漏检 {fp['n_blocked']} 条，两条路修法不同）:")
        for key, label in (
            ("A_stage1_filled", "A 阶段1自己满页（标签太泛→热度榜）"),
            ("B_stage2_padded", "B 阶段2兜底补满（AND落空→沾边的）"),
        ):
            v = fp.get(key) or {}
            if v.get("n"):
                print(f"    {label:36} {v['n']:>3} ({v['share']:.0%})  "
                      f"平均标签 {v['mean_tags']}")
        print("    → 根因与修法方向见报告 md 的「根因」一节")

    if rep.get("rescue"):
        rs = rep["rescue"]
        print()
        print("  救援实验 · 把被短路的漏检单独喂给语义通道（因果证据）:")
        print(f"    被短路且漏检 {rs['n_blocked']} 条 → 救回 {rs['n_rescued']} 条 "
              f"({rs['rate']:.0%})")
        for style, v in rs["by_style"].items():
            print(f"      {style:6} {v['n_rescued']}/{v['n_blocked']} = {v['rate']:.0%}")
        print(f"    谁关的闸门: {rs['by_blocker']}")
        for k, v in rs["projected_recall"].items():
            print(f"    反事实 {k}: {v['now']:.3f} → {v['if_rescued']:.3f} "
                  f"(+{v['gain_pp']}pp)")
    if rep["miss_examples"]:
        print()
        print(f"  完全找不到的例子（共 {rep['n_miss']} 条，示前 5）:")
        for m in rep["miss_examples"][:5]:
            print(f"    [{m['style']:5}] {m['query'][:38]!r}  真身={m['gt']}")


def _save(rep: dict) -> tuple[Path, Path]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{rep['name']}-{stamp}-{rep['git']}"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{name}.json"
    md_path = RESULTS_DIR / f"{name}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 轴 2 · D 组 GT（倒着出题）— 层 A 初稿数字",
        "",
        f"**时间**: {rep['created']} | **git**: {rep['git']} | "
        f"**GT**: {rep['gt_file']} ({rep['gt_status']})",
        "",
        "> ## ⚠ 不可引用",
        "> 这是**未经层 B 过滤的初稿数字**。单答案 GT 会把「合法地匹配了别的作品」"
        "记成没找到，真实质量由层 B 的相关率（含随机基线对照）给出。",
        "",
        "## 总览",
        "",
        "| 组（entity_type·style） | n | Recall@5 | Recall@10 | Recall@20 | MRR |",
        "|---|---|---|---|---|---|",
    ]
    for gname, v in rep["table"].items():
        if not v.get("n"):
            continue
        lines.append(f"| {gname} | {v['n']} | {v['Recall@5']} | {v['Recall@10']} "
                     f"| {v['Recall@20']} | {v['MRR']} |")

    lines += ["", "## 分层诊断 · 口头指代", "",
              "标题恰好是该条目自己的标签时，命中率顶在 1.0（与 B 组同源，无信息量）；"
              "标题不是标签时才是真信号。两类混报会互相稀释。", "",
              "| 层 | n | Recall@10 | MRR |", "|---|---|---|---|"]
    for gname, v in rep["title_split"].items():
        if v.get("n"):
            lines.append(f"| {gname} | {v['n']} | {v['Recall@10']} | {v['MRR']} |")

    lines += ["", "## 交叉制表 · vector 有没有被短路", "",
              "⚠ 不是干净的因果实验：短路【是因为】keyword 凑满了 limit，而"
              "「什么查询会让 keyword 凑满」本身就和查询类型相关。"
              "它证明两件事同时发生，「短路害的」要靠救援实验证。", "",
              "| 组 | n | Recall@10 | MRR |", "|---|---|---|---|"]
    for gname, v in rep["shortcircuit_split"].items():
        if v.get("n"):
            lines.append(f"| {gname} | {v['n']} | {v['Recall@10']} | {v['MRR']} |")

    lines += ["", "## 通道调用分布", "",
              "生产融合是**短路**的（`tools/bgm_tools.py:1091/1108`）：keyword 凑满 limit "
              "就不跑 name / vector，而 keyword 的阶段 2 软兜底按设计总把结果补到 limit。"
              "「vector 没跑」= 这条查询根本没经过语义通道。", "",
              "| 实际调用的通道 | 条数 | 占比 |", "|---|---|---|"]
    for k, v in rep["channel_profile"].items():
        lines.append(f"| {k} | {v} | {v / max(1, rep['n_queries']):.0%} |")

    if rep.get("rescue"):
        rs = rep["rescue"]
        lines += ["", "## 救援实验 · 因果证据", "",
                  "交叉制表只能证明「短路」和「找不到」**同时发生**（两者共享同一个原因："
                  "查询类型决定了 keyword 会不会凑满）。要证明「是短路害的」，必须把被"
                  "短路的查询单独喂给语义通道，看它本来找不找得到。", "",
                  f"**{rs['n_blocked']} 条被短路且漏检 → 救回 {rs['n_rescued']} 条 "
                  f"({rs['rate']:.0%})**", "",
                  "| 风格 | 被短路且漏检 | 救回 | 救援率 |", "|---|---|---|---|"]
        for style, v in rs["by_style"].items():
            lines.append(f"| {style} | {v['n_blocked']} | {v['n_rescued']} | {v['rate']:.0%} |")
        lines += ["", f"闸门是谁关的：`{json.dumps(rs['by_blocker'], ensure_ascii=False)}`", "",
                  "### 反事实投影", "",
                  "| 指标 | 现在 | 若这批被救回 | 增益 |", "|---|---|---|---|"]
        for k, v in rs["projected_recall"].items():
            lines.append(f"| {k} | {v['now']:.4f} | {v['if_rescued']:.4f} | +{v['gain_pp']}pp |")
        lines += ["", f"> 方法：{rs['method']}", "",
                  "> ⚠ 这不是「修好之后会怎样」的预测。它只回答「闸门没关的话，语义通道"
                  "本来能不能找到」—— 真去修还可能动到别的东西。", ""]

    if rep["miss_examples"]:
        lines += ["", f"## 完全找不到的例子（共 {rep['n_miss']} 条，示前 10）", "",
                  "| id | 风格 | 查询 | 真身 | 解析出的过滤器 | 实际调用通道 |",
                  "|---|---|---|---|---|---|"]
        for m in rep["miss_examples"][:10]:
            kf = json.dumps(m["keyword_filters"] or {}, ensure_ascii=False)
            inv = "+".join(c for c, ok in (m["invoked"] or {}).items() if ok) or "-"
            lines.append(f"| {m['id']} | {m['style']} | {m['query']} | {m['gt']} "
                         f"| `{kf}` | {inv} |")

    fp = rep.get("failure_paths") or {}
    if fp.get("n_blocked"):
        a, b = fp["A_stage1_filled"], fp["B_stage2_padded"]
        lines += ["", f"## 漏因分型（被短路的漏检 {fp['n_blocked']} 条）", "",
                  "两条路径的**修法不同**，混在一起报会选错药。", "",
                  "| 路径 | n | 占被短路漏检 | 平均标签数 | 触发条件 |",
                  "|---|---|---|---|---|"]
        if a.get("n"):
            lines.append(f"| A · 阶段1 自己满页 | {a['n']} | {a['share']:.0%} "
                         f"| {a['mean_tags']} | 标签太泛（如「动画」匹配上千条）→ 按热度取前 20 |")
        if b.get("n"):
            lines.append(f"| B · 阶段2 兜底补满 | {b['n']} | {b['share']:.0%} "
                         f"| {b['mean_tags']} | 标签具体但 AND 落空 → 软兜底补到 20 |")
    if rep.get("root_cause_notes"):
        lines.append("")
    for note in rep.get("root_cause_notes") or []:
        lines.append(note)

    lines += ["", "## 已知限制", ""]
    lines += [f"- {x}" for x in rep["known_limits"]]

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


# ═══════════════════════════════════════════════════════════════════════
# 层 B：正着判（相关率 + 相对随机基线的增益）
# ═══════════════════════════════════════════════════════════════════════

# 判定口径的版本号。改动下列任一项都必须改它，否则旧判定会被【静默复用】：
#   rubric 文本 / 判定提示词文本（含 _type_label） / 简介截断长度 / 标签展示上限
#   / 判定值取值集合 / 卡片字段构成
# b2：_type_label 的 person 措辞修正（原措辞与"找公司"式查询冲突，致整批空判）
# b3：person 卡片补上「作品」列表（缺失致作品式查询把真身判成 false）
JUDGE_VERSION = "b3"
JUDGE_SUMMARY_CHARS = 300      # 判官看到的简介长度（比出题用的 220 长：判定要更多依据）
JUDGE_TAG_LIMIT = 8
# 卡片里列多少个"参与作品"。person 必须给：作品式查询问的就是它，而公司/声优的
# 简介（如 MADHOUSE 那段讲丸山正雄与名字由来的）根本不会提作品 —— 实测 d428
# 因此把真身判成 false。character 早就有「出自」，person 是漏的。
JUDGE_WORK_LIMIT = 5
JUDGE_TEMPERATURE = 0.0        # 判定是测量，不是创作
# 一次调用判多少条。实测（同一池 28 张卡）：
#   思考关：20 张一次过，247 token、0 字符思考
#   思考开：这条【查询】难时 20 张烧穿 8192 也返回空串、10 张烧穿 4096、5 张才成；
#           但换个查询，35 张一次过只用 782 token
# 所以"批次多大"不是决定性变量，**查询本身难不难才是**。固定小批次（曾设 5）
# 是在为少数难查询拖慢所有查询：实测 12 条查询要 101 次调用，而 3 条查询按 35 张
# 判只用了 5 次。改成从 20 起、遇到判不动的查询再往下切（见 _judge_members 的
# 自适应降档），既不赌也不会在同一个档位上反复烧 token。
JUDGE_PACK = 20
JUDGE_MIN_SPLIT = 2            # 判不出来的批次二分到这个条数还失败就认输（记 failed）
JUDGE_MAX_TOKENS = 4096        # 实测 2048 会把 28 条的批次截断成空串（见 call_llm docstring）
RANDOM_POOL_SIZE = 15          # 每查询抽多少条随机候选
BOOTSTRAP_N = 1000

# 标签式的"结构化全集"曾经打算当第二个基线用，被数据否决了：120 条标签式查询里
# 30 条的多标签 AND 集**为空**，非空的中位数只有 2 条（最大 26）——域小到 2 条时
# "随机抓一条的相关率"是纯噪声。现在只把它当**诊断**保留（AND 落空率本身就是
# 那条短路缺陷的机制证据），不再抽随机样本。
TAG_DOMAIN_DIAG_BINS = ((0, 0), (1, 5), (6, 15), (16, 10**9))

JUDGE_CACHE_FILE = ARTIFACTS_DIR / "judge_cache.jsonl"

SUBJECT_TYPE_NAMES = {1: "书籍（漫画/小说）", 2: "动画"}

JUDGE_RUBRIC = """你是 Bangumi（动画收藏与评分社区）的检索质量评审员。
你会看到一条用户查询，和若干条候选条目。逐条判断：

【这条候选算不算这条查询的合理答案之一？】
判据只有一条：用户看到这条结果，会不会觉得答非所问？

· 相关（true）
  - 这条就是查询所描述的那部作品 / 那个角色 / 那个人物
  - 或者是它的同一系列（不同季度、剧场版、OVA、原作与改编）——
    用户能顺着它找到自己要的东西
· 不相关（false）
  - 只是题材或标签沾边，但不是查询所描述的那一部
  - 只有名字字面重合，作品本身无关
  - 类型不符（查询要的是动画，候选是无关的漫画/小说）
· 信息不足（unknown）
  - 简介太短或与查询描述的特征对不上号，你判断不了它到底讲什么
  - 你对它没有可靠了解，且给定信息不足以判断
  ⚠ 不要因为"我不认识"就判 false —— 那是两回事。

拿不准时的自问：搜索结果里只有这一条，用户会觉得"这也太敷衍了"吗？

输出必须是 JSON 对象：{"judgments": [{"i": 0, "v": "true"}, {"i": 1, "v": "false"}]}
v 只能取 true / false / unknown，且必须覆盖给出的每一条候选。不要输出任何解释。"""


def _judge_cache_key(query: str, card: str) -> str:
    """缓存键 = (口径版本, 查询, 候选卡片) 的 sha1 —— 【不含】候选来自哪条通道。

    这样键才与融合方式无关：修完短路重跑，随机池的判定全部命中，只重判新进
    top-20 的候选。卡片文本进键是关键 —— 将来 DB 重灌、简介变了，旧判定自动
    失效，不会拿陈年判定冒充新结论。

    批次大小（JUDGE_PACK / 自适应降档）【不】进键：它是批处理参数，不是判定口径 ——
    rubric 要求逐条独立判。代价是同一条候选在不同批次大小下可能判得略有出入，
    这一项连同判官本身的偏差一起交给阶段 5 的 κ 校准兜底，而不是靠膨胀缓存键。

    schema 里那个 `think=0` 是**冻结的字面量**，别再动它：它曾经是 A/B 档位，
    开关已按用户决策删除（2026-09-13，思考全项目常关）。保留字面量纯粹是为了
    不改 schema 字符串 —— 一改 sha1 全变，现有 690+ 条"关思考臂"的判定会全部
    失效、全部重判，白花钱。开思考臂那约 100 条从此永远命不中，那正是我们要的。
    """
    schema = (f"{JUDGE_VERSION}|think=0"
              f"|{JUDGE_SUMMARY_CHARS}|{JUDGE_TAG_LIMIT}|{JUDGE_WORK_LIMIT}"
              f"|{hashlib.sha1(JUDGE_RUBRIC.encode('utf-8')).hexdigest()[:8]}")
    return hashlib.sha1(f"{schema}\n{query}\n{card}".encode("utf-8")).hexdigest()


def _load_judge_cache() -> dict[str, str]:
    """读回已判定的结果（键 → 判定值）。文件不存在就是空缓存，不是错误。"""
    cache: dict[str, str] = {}
    if not JUDGE_CACHE_FILE.exists():
        return cache
    for line in JUDGE_CACHE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("k") and rec.get("v"):
                cache[rec["k"]] = rec["v"]
        except (json.JSONDecodeError, AttributeError):
            continue  # 半行（断电）跳过，不让它毁掉整份缓存
    return cache


def _append_judge_cache(entries: list[dict]) -> None:
    """逐条追加。判完一条查询就落一次盘 —— 790 次调用的活不能因为中途挂掉重付。"""
    if not entries:
        return
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with JUDGE_CACHE_FILE.open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        fh.flush()


def _type_label(q: dict) -> str:
    """查询要找的是什么 —— 判官需要知道自己在判作品、角色还是人物。

    person 一行的措辞是修出来的，不是随手写的：Bangumi 的 person 桶里**装着
    制作公司/事务所**（本库的 MADHOUSE、京都动画都是 person 条目）。原措辞
    「一个现实人物（声优/创作者等）」与作品式查询「做过《欺诈游戏》和《钻石王牌》
    的公司」直接冲突，判官卡在"查询要公司、系统说找人物"这个矛盾里空转 ——
    d428 实测：思考 13281 字符、烧穿 4096 token、返回空串，整批判定丢失。
    改成描述【条目类型】而不是【人的身份】，并显式把解释权交给查询原文。
    """
    et = q["entity_type"]
    if et == "subject":
        return f"一部作品（{SUBJECT_TYPE_NAMES.get(q.get('subject_type'), '未知子类型')}）"
    if et == "character":
        return "一个角色（作品中的虚构人物）"
    return ("一个 Bangumi「人物」条目 —— 可能是声优、创作者，也可能是动画制作"
            "公司、事务所等；具体是哪种，以用户查询的措辞为准")


def _candidate_card(rec: dict) -> str:
    """一张候选卡片 —— 判官能看到的全部信息，也是缓存键的一部分。

    刻意【不给】热度：它会诱导"有名的就是相关的"，而随机池里的候选平均比
    top-20 冷门得多，这个偏差会直接压低随机基线、抬高增益。
    简介必须给：没有它，判官只能靠"我认不认识这部番"，冷门条目会被误判成
    不相关 —— 同一个偏差，换条路又回来了。
    """
    primary = rec["name_cn"] or rec["name"]
    alt = ""
    if rec["name_cn"] and rec["name"] and rec["name_cn"] != rec["name"]:
        alt = f" / {rec['name']}"

    bits: list[str] = []
    if rec["entity_type"] == "subject":
        bits.append(SUBJECT_TYPE_NAMES.get(rec["subject_type"], "作品"))
        if rec["tags"]:
            bits.append("标签：" + "、".join(rec["tags"][:JUDGE_TAG_LIMIT]))
    elif rec["entity_type"] == "character":
        if rec["works"]:
            bits.append("出自：" + "、".join(rec["works"][:JUDGE_WORK_LIMIT]))
        if rec["role"]:
            bits.append(f"定位：{rec['role']}")
    else:
        if rec["career"]:
            bits.append("职业：" + "、".join(rec["career"][:3]))
        if rec["works"]:
            bits.append("作品：" + "、".join(rec["works"][:JUDGE_WORK_LIMIT]))

    head = f"{primary}{alt}" + (f"（{'｜'.join(bits)}）" if bits else "")
    summary = " ".join((rec["summary"] or "").split())[:JUDGE_SUMMARY_CHARS]
    return f"{head}\n    简介：{summary or '（库内无简介）'}"


def _judge_prompt(q: dict, cards: list[str]) -> str:
    lines = [f"【用户查询】{q['query']}", f"【要找的东西】{_type_label(q)}", "", "【候选条目】"]
    for i, card in enumerate(cards):
        lines.append(f"[{i}] {card}")
    lines += ["", f"逐条判断上面 {len(cards)} 条候选，按系统提示里的判据输出 JSON。",
              f'{{"judgments": [{{"i": 0, "v": "true"}}, ...]}}（覆盖 0..{len(cards) - 1} 全部索引）']
    return "\n".join(lines)


def _parse_judgments(raw: dict, n: int) -> dict[int, str]:
    """把 LLM 的返回收成 {索引: 判定值}，只留合法项。

    容错两处：`v` 给成布尔值（JSON true/false）当字符串读；`judgments` 给成
    {"0": "true"} 这种字典也认。越界/重复/非法值一律丢弃 —— 丢弃会让索引缺口
    进入第二次补判，比强行当成 false 诚实。
    """
    items = raw.get("judgments") if isinstance(raw, dict) else None
    if isinstance(items, dict):
        items = [{"i": k, "v": v} for k, v in items.items()]
    if not isinstance(items, list):
        return {}

    out: dict[int, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        val = str(it.get("v", "")).strip().lower()
        if val in ("true", "false", "unknown") and 0 <= idx < n and idx not in out:
            out[idx] = val
    return out


def _judge_batch(client: OpenAI, model: str, q: dict,
                 items: list[tuple[int, str]]) -> tuple[dict[int, str], int]:
    """判一批 (全局索引, 卡片)，返回 ({索引: 判定值}, API 调用次数)。

    判不出来的索引**二分再判**，而不是原样重试 —— 原样重试只会原样失败。
    实测失败形态是：模型陷入长时间推理，把 max_tokens 烧穿，JSON mode 下返回空串
    （d428 拿 20 张卡思考 13281 字符仍无输出；同一批切到 5 张卡就出结果）。
    所以"减少同屏候选数"是唯一有效的降载手段，二分是它的兜底。

    `n_retry=1` 是刻意的：call_llm 内部默认还会重试 2 次，那样一次调用实际打两次
    API，报告里的调用次数就是假的（成本估算要拿它外推）。重试语义整体搬到这里的
    二分上 —— 网络抖动或模型抽风，都由"切小半批重判"接住。

    代价上界：一批 20 张若**每档都失败**，会裂成 39 次调用才认输（1+2+4+8+… 收敛
    到 2×2^k−1）。实测不会走到那儿 —— 关思考时 20 张一次过（247 token），
    失败只出现在"模型对这条查询陷入长推理"这种整批性故障上，二分两次就到底。
    """
    if not items:
        return {}, 0
    raw = call_llm(client, model, JUDGE_RUBRIC,
                   _judge_prompt(q, [c for _, c in items]),
                   temperature=JUDGE_TEMPERATURE, max_tokens=JUDGE_MAX_TOKENS,
                   n_retry=1)
    got = {items[j][0]: v for j, v in _parse_judgments(raw, len(items)).items()}
    missing = [it for it in items if it[0] not in got]
    if not missing or len(items) < JUDGE_MIN_SPLIT:
        return got, 1

    n_calls = 1
    mid = len(missing) // 2
    for half in (missing[:mid], missing[mid:]):
        sub, calls = _judge_batch(client, model, q, half)
        got.update(sub)
        n_calls += calls
    return got, n_calls


def _judge_cards(client: OpenAI, model: str, q: dict,
                 cards: list[str]) -> tuple[dict[int, str], int]:
    """判一批卡片（调用方按 JUDGE_PACK 控制条数并自适应降档），返回 ({索引: 判定值}, 次数)。"""
    return _judge_batch(client, model, q, list(enumerate(cards)))


# ── 候选池：三部分互斥 ──


def _full_domain_ids(q: dict, recs: dict[str, dict]) -> list[str]:
    """全域 = 同 entity_type + 同 subject_type 的所有实体（nsfw 已在上游滤掉）。

    非 subject 实体的 subject_type 是 None，查询也是 None，所以同一个条件式
    对三类实体都成立，不需要分支。
    """
    et, st = q["entity_type"], q.get("subject_type")
    return sorted(i for i, r in recs.items()
                  if r["entity_type"] == et and r["subject_type"] == st)


def _matches_filters(meta: dict, filters: dict) -> bool:
    """查询抽出来的结构化条件，候选是否满足。

    条件构造对齐 `rag/retriever.py:510-523` 的阶段 1 SQL（tags AND / year /
    score / career）。这里在 Python 里判而不是抄那段 SQL：全集已经在内存里
    （1500 条），而且【这不是检索路径】—— 它只用来划"随机基线从哪个域里抽"，
    不产出任何被测指标，所以不存在和生产漂移的风险。
    """
    tags = filters.get("required_tags") or []
    have = {t.get("name") for t in (meta.get("tags") or []) if isinstance(t, dict)}
    if any(t not in have for t in tags):
        return False

    year = filters.get("year")
    if year is not None and str(meta.get("year")) != str(year):
        return False
    if filters.get("min_score") is not None:
        try:
            if float(meta.get("score")) < float(filters["min_score"]):
                return False
        except (TypeError, ValueError):
            return False

    career = filters.get("career")
    if career:
        field = meta.get("career")
        if isinstance(field, dict):
            if career not in field:
                return False
        elif isinstance(field, list):
            if career not in field:
                return False
        else:
            return False
    return True


def _tagged_domain_ids(q: dict, filters: dict, recs: dict[str, dict],
                       meta_by_id: dict[str, dict]) -> list[str]:
    """标签式的"结构化全集" —— 生产 keyword 通道真正搜索的那个域。

    只在查询抽出了结构化条件时存在；没有条件时 keyword 通道根本不跑，
    这条查询的域就是全域。
    """
    if not filters:
        return []
    return sorted(i for i in _full_domain_ids(q, recs)
                  if _matches_filters(meta_by_id.get(i) or {}, filters))


def _build_pool(q: dict, merged_ids: list[str], recs: dict[str, dict],
                meta_by_id: dict[str, dict], filters: dict,
                rng: random.Random) -> dict:
    """组池子。三部分互斥，随机 15 条要排除 top-20 和探针。

    为什么要排除：探针是【已知答案】，混进随机池会污染基线；top-20 是系统
    已经返回的，混进来会低估"系统漏了什么"。
    """
    gt_id = q["gt_entity_id"]
    top20 = [i for i in merged_ids if i in recs][:RETRIEVAL_LIMIT_D]

    buckets: dict[str, list[str]] = {}

    def _add(ids: list[str], name: str) -> None:
        for i in ids:
            buckets.setdefault(i, []).append(name)

    _add(top20, "top20")
    taken = set(buckets) | {gt_id}

    domain = _full_domain_ids(q, recs)
    pool_ids = [i for i in domain if i not in taken]
    _add(rng.sample(pool_ids, min(RANDOM_POOL_SIZE, len(pool_ids))), "random_full")

    # 标签集域只当诊断用（AND 落空率是短路缺陷的机制证据），不抽随机样本 —— 见常量区注释
    tagged = _tagged_domain_ids(q, filters, recs, meta_by_id)

    probe_in_pool = gt_id in recs
    if probe_in_pool and gt_id not in buckets:
        _add([gt_id], "probe")

    # 打乱顺序：位置不能泄漏来源（盲判的硬要求）。先排序再打乱，保证可复现。
    order = sorted(buckets)
    rng.shuffle(order)
    return {
        "members": [{"id": i, "buckets": buckets[i]} for i in order],
        "domain_size": len(domain),
        "tagged_domain_size": len(tagged) if filters else None,
        "probe_in_pool": probe_in_pool,
    }


def _judge_members(client: OpenAI, model: str, q: dict, members: list[dict],
                   cache: dict[str, str],
                   recs: dict[str, dict]) -> tuple[list[Optional[str]], int, list[dict]]:
    """判一个池子。已进缓存的直接取回，只对没判过的付钱；没判过的分批。

    **自适应降档**：某一档没能一次过，说明难的是【这条查询】而不是批次大小，
    于是把剩下的批次砍半 —— 否则后续每一批都要在同一档位上先烧穿一次
    max_tokens 再二分回来，纯浪费。用 while 而不是 for：`range` 在进入循环时
    就定死了，循环里改 pack 不会生效（写错过一次）。
    """
    cards = [_candidate_card(recs[m["id"]]) for m in members]
    keys = [_judge_cache_key(q["query"], c) for c in cards]
    verdicts: list[Optional[str]] = [cache.get(k) for k in keys]

    todo = [i for i, v in enumerate(verdicts) if v is None]
    pack = JUDGE_PACK
    n_calls = 0
    new_entries: list[dict] = []
    start = 0
    while start < len(todo):
        chunk = todo[start:start + pack]
        start += len(chunk)
        got, calls = _judge_cards(client, model, q, [cards[i] for i in chunk])
        n_calls += calls
        for j, val in got.items():
            idx = chunk[j]
            verdicts[idx] = val
            new_entries.append({"k": keys[idx], "qid": q["id"],
                                "eid": members[idx]["id"], "v": val})
        if len(got) < len(chunk) and pack > JUDGE_MIN_SPLIT:
            pack = max(JUDGE_MIN_SPLIT, pack // 2)
    return verdicts, n_calls, new_entries


# ── 统计 ──


def _stat(members: list[dict], verdicts: list[Optional[str]], bucket: str) -> dict:
    vals = [verdicts[i] for i, m in enumerate(members) if bucket in m["buckets"]]
    judged = [v for v in vals if v in ("true", "false")]
    return {
        "n": len(vals),
        "judged": len(judged),
        "relevant": sum(1 for v in judged if v == "true"),
        "unknown": sum(1 for v in vals if v == "unknown"),
        "failed": sum(1 for v in vals if v is None),
    }


def _rate(stat: dict) -> Optional[float]:
    return stat["relevant"] / stat["judged"] if stat["judged"] else None


def _paired_bootstrap_ci(pairs: list[tuple[float, float]],
                         n: int = BOOTSTRAP_N, seed: int = DEFAULT_SEED) -> Optional[list]:
    """增益的 95% 区间 —— 配对重抽查询。

    不给区间的增益没法读：character/person 每组只有 15 条查询，"涨了 12pp"
    完全可能是噪声。区间跨 0 就是"看不出差别"，报告要照实说。
    """
    if len(pairs) < 2:
        return None
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        drawn = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        diffs.append(statistics.fmean(a for a, _ in drawn)
                     - statistics.fmean(b for _, b in drawn))
    diffs.sort()
    return [round(diffs[int(0.025 * n)], 4), round(diffs[int(0.975 * n)], 4)]


def _tag_domain_diag(rows: list[dict]) -> dict:
    """标签集域的规模分布（诊断，不是基线）。

    它记录的是那条短路缺陷的**机制**：查询抽出的多个标签是逐条 AND 的，AND 一落空，
    阶段 1 就是空的，阶段 2 的软兜底于是补满 limit —— 融合层读成"找够了"，语义通道
    被跳过。所以"AND 集为空/极小"的占比越高，短路就越普遍。
    """
    sizes = [r["tagged_domain_size"] for r in rows if r["tagged_domain_size"] is not None]
    bins = [{"range": (f"{lo}–{hi}" if hi < 10 ** 8 else f"≥{lo}"),
             "n": sum(1 for s in sizes if lo <= s <= hi)}
            for lo, hi in TAG_DOMAIN_DIAG_BINS]
    return {
        "n_with_filters": len(sizes),
        "n_no_filters": len(rows) - len(sizes),
        "n_empty": sum(1 for s in sizes if s == 0),
        "median": statistics.median(sizes) if sizes else None,
        "bins": bins,
    }


def _estimated_recall(rows: list[dict]) -> dict:
    """粗估召回率 = top-20 里相关的条数 ÷ 估计的相关条目总数。

    相关总数靠随机池给密度、乘域大小。**密度必须在同域内合并后再用**：每查询只有
    15 个随机样本，而相关条目在域里往往只占百分之几 —— 单查询的密度估计大多数时候
    就是 0，拿它做分母会把召回率放大好几倍（比率估计的小分母偏倚）。所以按域大小
    （= entity_type + subject_type）分层合并密度，再算组级比率。
    """
    strata: dict[int, dict] = {}
    for r in rows:
        b = r["buckets"]["random_full"]
        if not b["judged"] or not r["domain_size"]:
            continue
        s = strata.setdefault(r["domain_size"], {"judged": 0, "relevant": 0, "top20": 0, "n": 0})
        s["judged"] += b["judged"]
        s["relevant"] += b["relevant"]
        s["top20"] += r["buckets"]["top20"]["relevant"]
        s["n"] += 1

    num = den = 0.0
    densities = []
    for size, s in sorted(strata.items()):
        if not s["judged"]:
            continue
        density = s["relevant"] / s["judged"]
        densities.append({"domain_size": size, "density": round(density, 4),
                          "n_queries": s["n"]})
        num += s["top20"]
        den += density * size * s["n"]
    return {
        "value": round(num / den, 3) if den > 0 else None,
        "n_used": sum(s["n"] for s in strata.values()),
        "densities": densities,
    }


def _group_layer_b(rows: list[dict]) -> dict:
    out: dict = {"n_queries": len(rows)}
    for bucket in ("top20", "random_full"):
        agg = {k: sum(r["buckets"][bucket][k] for r in rows)
               for k in ("n", "judged", "relevant", "unknown", "failed")}
        seen = agg["judged"] + agg["unknown"]
        agg["rate"] = round(agg["relevant"] / agg["judged"], 4) if agg["judged"] else None
        agg["unknown_rate"] = round(agg["unknown"] / seen, 4) if seen else None
        out[bucket] = agg

    pairs = [(r["buckets"]["top20"]["_rate"], r["buckets"]["random_full"]["_rate"])
             for r in rows
             if r["buckets"]["top20"]["_rate"] is not None
             and r["buckets"]["random_full"]["_rate"] is not None]
    if pairs:
        top = statistics.fmean(a for a, _ in pairs)
        base = statistics.fmean(b for _, b in pairs)
        ci = _paired_bootstrap_ci(pairs)
        out["gain"] = {
            "n_pairs": len(pairs),
            "top20_rate": round(top, 4),
            "random_rate": round(base, 4),
            "gain_pp": round((top - base) * 100, 1),
            "ci95_pp": [round(x * 100, 1) for x in ci] if ci else None,
        }

    # 探针：出题实体被判成不相关 = 这条查询写坏了。
    # 「池外」按 probe_in_pool 数，不按"判定值是否为 None"—— 后者把判定失败混进来了。
    probes = [r["probe_verdict"] for r in rows if r["probe_in_pool"]]
    out["probe"] = {
        "n": len(probes),
        "true": sum(1 for v in probes if v == "true"),
        "false": sum(1 for v in probes if v == "false"),
        "unknown": sum(1 for v in probes if v == "unknown"),
        "failed": sum(1 for v in probes if v is None),
        "missing": sum(1 for r in rows if not r["probe_in_pool"]),
    }

    out["top20_self"] = sum(r["top20_relevant_is_self"] for r in rows)
    out["top20_sibling"] = sum(r["top20_relevant_sibling"] for r in rows)
    out["random_full_relevant"] = out["random_full"]["relevant"]
    out["tag_domain"] = _tag_domain_diag(rows)
    out["estimated_recall"] = _estimated_recall(rows)
    return out


def _select_queries(queries: list[dict], n: int, seed: int) -> list[dict]:
    """试点抽样：按 (entity_type·style) 分组配额，每组至少 1 条。

    "至少 1 条"是硬的 —— 试点把 character/person 缩成 0 就等于没验证那两条分支。
    实际条数可能略多于 n（四舍五入的零头），打印里会写清抽了几条。
    """
    if not n:
        return queries
    groups: dict[str, list[dict]] = {}
    for q in queries:
        groups.setdefault(f"{q['entity_type']}·{q['style']}", []).append(q)

    total = len(queries)
    picked: list[dict] = []
    for name, rows in sorted(groups.items()):
        k = max(1, round(n * len(rows) / total))
        picked += random.Random(f"{seed}:{name}").sample(rows, min(k, len(rows)))
    return sorted(picked, key=lambda q: q["id"])


def _build_layer_b_report(gt: dict, rows: list[dict], elapsed: float,
                          n_calls: int, n_cache_hit: int) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(f"{r['entity_type']}·{r['style']}", []).append(r)

    return {
        "name": "rag_gt_layer_b",
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "git": _git_hash(),
        "gt_file": GT_FILE.name,
        "gt_status": gt.get("status"),
        "settings": gt.get("settings") or _settings_snapshot(),
        "judge": {
            "version": JUDGE_VERSION,
            "rubric_sha1_8": hashlib.sha1(
                JUDGE_RUBRIC.encode("utf-8")).hexdigest()[:8],
            "model": (gt.get("settings") or _settings_snapshot()).get("LLM_MODEL"),
            "temperature": JUDGE_TEMPERATURE,
            "summary_chars": JUDGE_SUMMARY_CHARS,
            "tag_limit": JUDGE_TAG_LIMIT,
            "verdicts": ["true", "false", "unknown"],
            # 冻结事实而非可调口径：思考已全项目强制关闭（2026-09-13 用户决策）。
            # 留着它是为了让本报告与 A/B 时期的报告结构可比。
            "thinking": False,
            "pack": JUDGE_PACK,
        },
        "random_pool_size": RANDOM_POOL_SIZE,
        "seed": gt.get("seed"),
        "elapsed_s": round(elapsed, 1),
        "usage": dict(USAGE),  # 实测 token（含被判废的调用），成本外推用
        "n_queries": len(rows),
        "n_calls": n_calls,
        "n_cache_hit": n_cache_hit,
        "table": {name: _group_layer_b(rs) for name, rs in sorted(groups.items())},
        "rows": rows,
        "n_error": sum(1 for r in rows if r.get("error")),
        "n_judge_failed": sum(
            b["failed"] for r in rows for b in r["buckets"].values()),
        "known_limits": [
            "⚠ 判官尚无 κ（人工校准在阶段 4/5），本报告的数字按 eval/README.md:483 不可引用。",
            "三值 rubric：unknown 从相关率的分母里剔除，并单独报比率（>20% 说明判官看到的简介不够判）。",
            "探针（出题实体）的判定与查询词面高度重合，几乎必然判 true —— 互验只能发现【极端坏题】，没发现不等于题目都合格。",
            "随机基线是 15 条/查询的抽样估计，组内合并后仍带抽样噪声（配对 CI 见主表）。",
            "答案密度按域（entity_type+subject_type）分层合并后使用；单查询密度不作数 —— 15 个样本估不出百分之几的密度。",
            "粗估召回率 = top-20 相关数 / (合并密度 × 域大小)，依赖随机样本代表性，只作量级参考。",
            "标签集域（多标签 AND 的结果集）只作诊断，不充当基线：120 条标签式查询里 30 条为空、非空中位数 2 条。",
            "全域随机基线对标签式查询偏严：它把「标签过滤」的功劳也算进了系统的增益里。",
            "judge 与被评模型同源（单 provider，LLM_MODEL 单值），自偏好偏差只能声明不能消除。",
            "答案闭世界：只在本地 1500 条实体库内。",
            "非 subject 组各只有 15 条查询，只作方向性读数。",
        ],
    }


def _print_layer_b_report(rep: dict) -> None:
    print()
    u = rep.get("usage") or {}
    print(f"  查询 {rep['n_queries']} 条 | 批次调用 {rep['n_calls']} 次"
          f"（缓存命中 {rep['n_cache_hit']} 条判定）| 耗时 {rep['elapsed_s']}s")
    if u.get("http_calls"):
        print(f"  token 实测：HTTP {u['http_calls']} 次 | 输入 {u['prompt_tokens']:,}"
              f" | 输出 {u['completion_tokens']:,}")
    print()
    print("  ⚠ 判官尚无 κ，本报告数字不可引用（见 eval/README.md:483）")
    print()
    head = (f"  {'组':16} {'n':>3} {'top20':>7} {'随机':>7} {'增益':>8} "
            f"{'95%CI(pp)':>14} {'unknown':>8}")
    print(head)
    print("  " + "-" * (len(head) - 2))
    for name, v in rep["table"].items():
        g = v.get("gain") or {}
        ci = g.get("ci95_pp")
        ci_s = f"[{ci[0]}, {ci[1]}]" if ci else "-"
        print(f"  {name:16} {v['n_queries']:>3} "
              f"{(g.get('top20_rate') or 0):>7.3f} {(g.get('random_rate') or 0):>7.3f} "
              f"{(g.get('gain_pp') or 0):>+7.1f}pp {ci_s:>14} "
              f"{(v['top20'].get('unknown_rate') or 0):>8.1%}")
    print()
    print("  互验 · 探针（出题实体被判成什么；false/unknown 说明这条查询写坏了）:")
    for name, v in rep["table"].items():
        p = v["probe"]
        if p["n"] or p["missing"]:
            print(f"    {name:16} true={p['true']:>3} false={p['false']:>3} "
                  f"unknown={p['unknown']:>3} 判定失败={p['failed']:>2} 不在池内={p['missing']:>3}")
    print()
    print("  top-20 里被判相关的条目：本人 vs 同系列（系统找回的是那一部，还是它的兄弟）:")
    for name, v in rep["table"].items():
        if v["top20_self"] or v["top20_sibling"]:
            print(f"    {name:16} 本人={v['top20_self']:>4} 同系列/其它={v['top20_sibling']:>4}")
    print()
    print("  标签集域诊断（多标签 AND 落空 = 短路缺陷的机制）:")
    for name, v in rep["table"].items():
        td = v["tag_domain"]
        if td["n_with_filters"]:
            print(f"    {name:16} 抽出条件的 {td['n_with_filters']:>3} 条里 "
                  f"{td['n_empty']:>3} 条 AND 集为空，中位域大小 {td['median']}")
    print()
    print("  粗估召回率（top-20 相关数 ÷ 估计的相关条目总数）:")
    for name, v in rep["table"].items():
        er = v["estimated_recall"]
        er_s = f"{er['value']:.2f}（n={er['n_used']}）" if er["value"] is not None else "-"
        dens = " ".join(f"{d['domain_size']}条域:{d['density']:.3f}" for d in er["densities"])
        print(f"    {name:16} 召回≈{er_s}  答案密度 {dens}")
    if rep["n_judge_failed"]:
        print(f"\n  ⚠ 有 {rep['n_judge_failed']} 条判定失败（已从分母剔除，不是当成不相关）")


def _save_layer_b(rep: dict) -> tuple[Path, Path]:
    """与层 A 的 _save 分开写：层的报告结构不同，混用一个函数会让两边都变脆。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # 文件名不再带思考档：它曾经是 A/B 两臂的区分位，现在全项目常关、没有第二臂了
    name = f"{rep['name']}-{stamp}-{rep['git']}"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{name}.json"
    md_path = RESULTS_DIR / f"{name}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    jd = rep["judge"]
    lines = [
        "# 轴 2 · D 组 GT — 层 B：相关率 + 相对随机基线的增益",
        "",
        f"**时间**: {rep['created']} | **git**: {rep['git']} | "
        f"**GT**: {rep['gt_file']} ({rep['gt_status']})",
        f"**判定**: {jd['model']} temp={jd['temperature']} | rubric `{jd['version']}`"
        f"/`{jd['rubric_sha1_8']}` | 简介截断 {jd['summary_chars']} 字 | 三值 | "
        f"思考强制关闭（项目决定） | 批次 {jd['pack']}",
        "",
        f"**成本**: HTTP {rep['usage']['http_calls']} 次 | 输入 "
        f"{rep['usage']['prompt_tokens']:,} tok | 输出 "
        f"{rep['usage']['completion_tokens']:,} tok | 耗时 {rep['elapsed_s']}s"
        f" | 查询 {rep['n_queries']} 条",
        "",
        "> ## ⚠ 不可引用（判官尚无 κ）",
        "> `eval/README.md:483`：无校准数字的 judge 数字不可解释。人工标注（阶段 4）"
        "算出 κ 之前，本报告只能当**结构完整的初稿**读。",
        "",
        "## 主表（分风格）",
        "",
        "增益 = 生产 top-20 相关率 − 全域随机相关率。**只有同一行内两个数可以相减** —— "
        "标签式的随机域是标签集合、标题/剧情式是全库，跨风格不可比。",
        "",
        "| 组 | n | top-20 相关率 | 全域随机 | 增益 | 95% CI (pp) | top-20 unknown | 随机 unknown |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for gname, v in rep["table"].items():
        g = v.get("gain") or {}
        ci = g.get("ci95_pp")
        ci_s = f"[{ci[0]}, {ci[1]}]" if ci else "—"
        lines.append(
            f"| {gname} | {v['n_queries']} | {_fmt(g.get('top20_rate'))} "
            f"| {_fmt(g.get('random_rate'))} | {_fmt_pp(g.get('gain_pp'))} | {ci_s} "
            f"| {_fmt_pct(v['top20'].get('unknown_rate'))} "
            f"| {_fmt_pct(v['random_full'].get('unknown_rate'))} |")

    lines += ["", "## 标签集域诊断（不是基线）", "",
              "查询抽出的多个标签是**逐条 AND** 的：AND 一落空，keyword 阶段 1 就是空的，"
              "阶段 2 的软兜底随即补满 limit —— 融合层读成「找够了」，语义通道被跳过。"
              "所以「AND 集为空」的占比越高，短路越普遍。", "",
              "原计划拿这个集合当第二个随机基线，**被数据否决**：域小到中位数个位数时，"
              "「随机抓一条的相关率」是纯噪声。现在只留作诊断。", "",
              "| 组 | 抽出条件的查询 | 其中 AND 集为空 | 非空域大小（中位） | 没抽出条件 |",
              "|---|---|---|---|---|"]
    for gname, v in rep["table"].items():
        td = v["tag_domain"]
        if not (td["n_with_filters"] or td["n_no_filters"]):
            continue
        med = "—" if td["median"] is None else f"{td['median']:.0f}"
        lines.append(f"| {gname} | {td['n_with_filters']} | {td['n_empty']} | {med} "
                     f"| {td['n_no_filters']} |")

    lines += ["", "## 互验 · 探针（出题实体）", "",
              "层 A 说答案是它，层 B 说它不相关 → 这条查询写坏了。**注意**：查询是从它的"
              "简介反编的，词面高度重合，判 true 几乎是必然 —— 所以这里**只能发现极端坏题**，"
              "没发现不等于题目都合格。", "",
              "| 组 | 池内 | true | false | unknown | 判定失败 | 不在池内 |",
              "|---|---|---|---|---|---|---|"]
    for gname, v in rep["table"].items():
        p = v["probe"]
        lines.append(f"| {gname} | {p['n']} | {p['true']} | {p['false']} | {p['unknown']} "
                     f"| {p['failed']} | {p['missing']} |")

    lines += ["", "## 系统找回的是那一部，还是它的兄弟", "",
              "| 组 | 就是出题实体 | 同系列/其它也算相关 |", "|---|---|---|"]
    for gname, v in rep["table"].items():
        if v["top20_self"] or v["top20_sibling"]:
            lines.append(f"| {gname} | {v['top20_self']} | {v['top20_sibling']} |")

    lines += ["", "## 漏检证据与粗估召回率", "",
              "随机池按构造**排除了 top-20**，所以随机池里每一条被判相关的 = 系统没返回、"
              "但它确实算答案的条目。答案密度（随机池相关率）按域分层合并后乘域大小 ≈ "
              "相关条目总数，据此粗估召回率。", "",
              "| 组 | 随机池判相关（=漏检证据） | 粗估召回率 | 域大小 → 答案密度 |",
              "|---|---|---|---|"]
    for gname, v in rep["table"].items():
        er = v["estimated_recall"]
        er_s = "—" if er["value"] is None else f"{er['value']:.2f}"
        dens = "；".join(f"{d['domain_size']} 条域 → {d['density']:.3f}" for d in er["densities"])
        lines.append(f"| {gname} | {v['random_full_relevant']} | {er_s} | {dens or '—'} |")

    lines += ["", "## 判定健康度", "",
              f"- LLM 调用 **{rep['n_calls']}** 次，缓存命中 **{rep['n_cache_hit']}** 条判定",
              f"- 判定失败（两次都没吐出来）**{rep['n_judge_failed']}** 条 —— "
              "已从分母剔除，不是当成不相关",
              "- unknown 从相关率的分母剔除；某组 unknown 超过 20% 说明判官看到的简介不够判，"
              "该修输入而不是继续跑",
              "",
              "## 已知限制", ""]
    lines += [f"- {x}" for x in rep["known_limits"]]

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def _fmt(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.3f}"


def _fmt_pp(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:+.1f}pp"


def _fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.1%}"


def cmd_judge(args) -> int:
    from core.config import get_settings

    from eval.rag_eval import (
        _embedding_canary, _get_retriever, _production_search, _silence_sqlalchemy,
    )

    _silence_sqlalchemy()

    print("=" * 64)
    print("  D 组 · 层 B（正着判）—— 相关率 + 相对随机基线的增益")
    print(f"  判定口径：思考强制关闭（项目决定）"
          f"（批次起于 {JUDGE_PACK}、判不动自动砍半，JUDGE_VERSION={JUDGE_VERSION}）")
    print("=" * 64)

    gt = _load_gt()
    queries = gt.get("queries", [])
    if not queries:
        print("  ✗ 草稿里没有查询")
        return 1
    if gt.get("status") != "draft_unfiltered" and not args.allow_partial:
        print(f"  ⚠ 草稿状态是 {gt.get('status')!r}（可能是断点续跑的中间态）。")
        print("    要按部分样本出数：加 --allow-partial。")
        return 1

    queries = _select_queries(queries, args.sample, args.seed)
    if args.sample:
        print(f"  试点：抽 {len(queries)} 条查询（seed={args.seed}，分组配额）")

    err = _embedding_canary()
    if err:
        print(f"  ✗ embedding 探活失败，候选池不可信：{err}")
        return 1
    print("  ✓ embedding 探活通过")

    from sqlmodel import Session, select

    from database.engine import engine
    from database.rag_tables import RagEntity

    with Session(engine) as session:
        entities = session.exec(
            select(RagEntity).where(RagEntity.nsfw == False)  # noqa: E712
        ).all()
    recs = {e.id: _entity_record(e) for e in entities}
    meta_by_id = {e.id: (e.meta_info or {}) for e in entities}
    print(f"  库内实体 {len(recs)} 条（nsfw 已排除）")

    cfg = get_settings()
    client = OpenAI(api_key=cfg.LLM_API_KEY, base_url=cfg.LLM_BASE_URL or None)
    retr = _get_retriever()
    cache = _load_judge_cache()
    if cache:
        print(f"  判定缓存 {len(cache)} 条（命中即跳过，不重复付钱）")

    rows: list[dict] = []
    n_calls = 0
    n_cache_hit = 0
    t0 = time.time()

    for i, q in enumerate(queries, 1):
        rng = random.Random(f"{args.seed}:{q['id']}")  # 每查询独立种子 → 池子与批次无关
        try:
            r = _production_search(q, retr, limit=RETRIEVAL_LIMIT_D)
            pool = _build_pool(q, r["merged"], recs, meta_by_id,
                               r["keyword_filters"], rng)
            members = pool["members"]
            verdicts, calls, new_entries = _judge_members(
                client, cfg.LLM_MODEL, q, members, cache, recs)
            n_calls += calls
            for e in new_entries:
                cache[e["k"]] = e["v"]
            _append_judge_cache(new_entries)  # 判完一条查询就落盘
            n_cache_hit += len(members) - len(new_entries)
        except Exception as exc:  # 规则 1：不抛异常，记成错误继续
            rows.append({**q, "error": f"{type(exc).__name__}: {exc}", "buckets": {},
                         "probe_verdict": None, "top20_relevant_is_self": 0,
                         "top20_relevant_sibling": 0, "domain_size": 0,
                         "tagged_domain_size": None, "probe_in_pool": False,
                         "n_returned": 0, "judge_calls": 0})
            continue

        buckets = {name: _stat(members, verdicts, name)
                   for name in ("top20", "random_full", "probe")}
        for b in buckets.values():
            b["_rate"] = _rate(b) if "relevant" in b else None

        gt_idx = next((j for j, m in enumerate(members) if m["id"] == q["gt_entity_id"]), None)
        top_idx = [j for j, m in enumerate(members) if "top20" in m["buckets"]]

        rows.append({
            **q,
            "n_returned": len(r["merged"]),
            "domain_size": pool["domain_size"],
            "tagged_domain_size": pool["tagged_domain_size"],
            "probe_in_pool": pool["probe_in_pool"],
            "probe_verdict": verdicts[gt_idx] if gt_idx is not None else None,
            "buckets": buckets,
            "top20_relevant_is_self": sum(
                1 for j in top_idx if verdicts[j] == "true" and members[j]["id"] == q["gt_entity_id"]),
            "top20_relevant_sibling": sum(
                1 for j in top_idx if verdicts[j] == "true" and members[j]["id"] != q["gt_entity_id"]),
            "judge_calls": calls,
        })
        if i % 10 == 0 or i == len(queries):
            print(f"  [{i}/{len(queries)}] 调用 {n_calls} 次 | 已跑 {time.time() - t0:.0f}s")

    report = _build_layer_b_report(gt, rows, time.time() - t0, n_calls, n_cache_hit)
    report["judge"]["model"] = cfg.LLM_MODEL  # 判官【本次】用的模型，不是出题时的
    report["judge"]["pack"] = JUDGE_PACK
    _print_layer_b_report(report)
    if not args.no_save:
        json_path, md_path = _save_layer_b(report)
        print(f"\n  → {json_path}\n  → {md_path}")
    return 0


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="D 组 GT：倒着出题（层 A）+ 层 B 判定 —— 轴 2 的质量数字")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--generate", action="store_true",
                       help="抽实体 + LLM 出题 → ground_truth_v2_draft.json（要钱）")
    group.add_argument("--evaluate", action="store_true",
                       help="层 A：跑生产检索算 Recall@K / MRR（只花 embedding）")
    group.add_argument("--judge", action="store_true",
                       help="层 B：候选池正着判 → 相关率 + 随机基线（要钱；先 --sample 试点）")
    parser.add_argument("--sample", type=int, default=0,
                        help="--generate：抽多少个实体（0=默认 150）；"
                             "--judge：抽多少条查询（0=全量 450；按分组配额，每组至少 1）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="抽样随机种子（--judge 也用它定候选池，池子因此可复现）")
    parser.add_argument("--force", action="store_true",
                        help="--generate：覆盖已完成的草稿（默认拒绝，防抹掉产出）")
    parser.add_argument("--allow-partial", action="store_true",
                        help="--evaluate / --judge：允许对未完成的草稿出数")
    parser.add_argument("--no-save", action="store_true",
                        help="--evaluate / --judge：不落报告文件")
    parser.add_argument("--rescue", action="store_true",
                        help="--evaluate：救援实验 —— 把被短路的漏检单独喂给语义通道"
                             "（因果证据，约多花 76 次 embedding）")
    args = parser.parse_args()

    if args.sample and not (args.generate or args.judge):
        parser.error("--sample 只与 --generate / --judge 同用")
    if args.rescue and not args.evaluate:
        parser.error("--rescue 只与 --evaluate 同用")

    if args.generate:
        sys.exit(cmd_generate(args))
    if args.evaluate:
        sys.exit(cmd_evaluate(args))
    sys.exit(cmd_judge(args))


if __name__ == "__main__":
    main()
