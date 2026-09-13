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

边界的教训（上一轮踩过）
    `_run_keyword_search` 曾是一份【影子实现】，它和生产漂了才被删（提交 490a3f5）。
    本模块【只 import，不复制】任何检索逻辑 —— 检索一律走 eval.rag_eval 的
    `_production_search`。自检方式：本文件里不应出现任何检索函数的 `def`
    （有一个就说明又造了一份影子实现，它迟早会和生产漂）。
"""

from __future__ import annotations

import argparse
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


def call_llm(client: OpenAI, model: str, system: str, user: str,
             n_retry: int = 2, temperature: float = 0.8) -> dict:
    """调用 LLM 并解析 JSON 对象，失败重试。

    照抄 eval/generate_intent_data.py:91 的范式（JSON mode + n_retry + sleep(2)），
    它是全 eval 目录唯一的重试实现。**不抛异常**是仓库规则 1 —— 但这里是【生成侧】，
    返回空 dict 会让调用方把该实体记成"出题失败"并继续，不会污染已有产出。
    """
    last_err: Exception | None = None
    for _ in range(n_retry):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=2048,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
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
    parser.add_argument("--sample", type=int, default=0,
                        help="--generate：抽多少个实体（0=默认 150）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="抽样随机种子")
    parser.add_argument("--force", action="store_true",
                        help="--generate：覆盖已完成的草稿（默认拒绝，防抹掉产出）")
    parser.add_argument("--allow-partial", action="store_true",
                        help="--evaluate：允许对未完成的草稿出数")
    parser.add_argument("--no-save", action="store_true",
                        help="--evaluate：不落报告文件")
    parser.add_argument("--rescue", action="store_true",
                        help="--evaluate：救援实验 —— 把被短路的漏检单独喂给语义通道"
                             "（因果证据，约多花 76 次 embedding）")
    args = parser.parse_args()

    if args.sample and not args.generate:
        parser.error("--sample 只与 --generate 同用")
    if args.rescue and not args.evaluate:
        parser.error("--rescue 只与 --evaluate 同用")

    sys.exit(cmd_generate(args) if args.generate else cmd_evaluate(args))


if __name__ == "__main__":
    main()
