"""
RAG 检索评测管线 v2

三步工作流::

    # 1. 我运行：生成自动 Ground Truth + 池化人工标注模板
    python -m eval.rag_eval --build

    # 2. 你标注：打开 eval/data/rag_gt/pooled_annotate.json，标记每个候选实体是否相关
    #    格式：将 "relevant": null 改为 true 或 false

    # 3. 我运行：计算全部指标 + 报告
    python -m eval.rag_eval --evaluate

    # 任何时候（改完代码想确认没弄坏东西）
    python -m eval.rag_eval --check      # 回归不变量，PASS/FAIL + 退出码

指标::

    Recall@K      K 个结果中找到了多少个 GT 实体 / GT 实体总数
    Precision@K   K 个结果中多少个在 GT 中 / K
    MRR           第一个 GT 实体排名的倒数（1/rank），越早越接近 1.0
    NDCG@K        带位置权重的相关度得分，排序越好越高
    Hit Rate@K    至少找到 1 个 GT 实体的 query 比例

评测维度::

    - 分组：A 自测组（期望恒 1.0，回归断言）/ B 质量组（有信息量）/
            C 人工组（池化偏差，偏高）
    - 按 category：tag / year / score / career / exact / semantic
    - 按检索通道：keyword / name / vector 各自【单独】能找到多少

⚠ 不要读「全局」那个数。它把三个可信度完全不同的组平均在一起，
  其中 A 组是构造出来的恒 1.0，会把整体拉高并稀释掉真正的差异。
  分组的定义见 SELF_TEST_CATEGORIES 的注释。
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eval")

EVAL_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = EVAL_DIR / "data" / "rag_gt"
AUTO_GT_FILE = ARTIFACTS_DIR / "ground_truth_auto.json"
POOLED_FILE = ARTIFACTS_DIR / "pooled_annotate.json"
MERGED_GT_FILE = ARTIFACTS_DIR / "ground_truth_merged.json"

# --check 的棘轮基线 —— 已确认的缺陷清单，【只允许收紧】。
# 为什么是数据文件而不是代码常量：它要带 why / at，且 --freeze 要能改写它。
CHECK_BASELINE_FILE = ARTIFACTS_DIR / "check_baseline.json"

# 棘轮"收紧"算不算失败。默认算——若只警告，基线文件会过期，之后一次把旧值
# 改回去的回归会【静默匹配】冻结集、门禁放行，洞就重新开了。想软化改这里，
# 不要在各个调用点临时决定。
RATCHET_SHRINK_IS_FAILURE = True

# I7：GT 为空（候选无一被判相关）的查询清单 —— 已知只有 p07。
# 写死而不是"读上一次运行"，因为这条断言的全部意义就是发现【标注被改动】。
EXPECTED_EMPTY_GT = ["p07"]

K_VALUES = [1, 3, 5, 10]

# 检索返回条数上限 —— 评测固定口径。
#
# ⚠ 这【不是】生产值：search_local_bangumi 的 limit 默认是 5（tools/bgm_tools.py:1131），
# 线上实际用几由 LLM 在工具参数里决定，是个变量（schema 限 1–20）。评测固定 10 是为了
# K=1/3/5/10 网格可比。改这里等于改评测口径，不改生产行为。
RETRIEVAL_LIMIT = 10

# 检索通道 —— 报告按通道分层。融合后的单一数字会掩盖「某个通道一条都没找到」，
# 而那恰恰是最需要看见的架构结论（见下面 SELF_TEST_CATEGORIES）。
CHANNELS = ("keyword", "name", "vector")

# ── 生产检索配置：唯一事实来源 ──
#
# 必须与 tools/bgm_tools.py:search_local_bangumi 一致 —— 生产【不传】任何消融开关，
# 用的是 rag/retriever.py:hybrid_search 的默认值。所以这三个值就是 hybrid_search
# 的签名默认值，改了生产参数要回来同步这里。
#
# 为什么要有这个常量：--evaluate 与 --ablate 都从它派生，两边口径不会再漂。
# 此前 --ablate 的基准写成「全开」（enable_bucketing=True），而生产默认是 False ——
# 等于拿一个线上不存在的配置当基准，所有 delta 都偏（实测 0.4103 vs 0.4172）。
PRODUCTION_RETRIEVAL: dict[str, bool] = {
    "enable_threshold": True,
    "enable_bucketing": False,   # ← 生产默认【关】。「全开」是虚构配置
    "enable_mmr": True,
}

# 生产距离阈值（hybrid_search 默认 0.65）。评测曾用 0.9，与生产不一致。
# 实测差异 ≈ 0（向量结果本就都落在 0.65 内），但口径必须一致才有意义。
PRODUCTION_DISTANCE_THRESHOLD = 0.65

# 池化模板【不】用生产阈值：池化要的是「尽量多捞候选」以减轻池化偏差（§3.2），
# 用更宽松的 0.9 是有意的；而测量必须复现生产，用 0.65。两者目的不同，别统一。
POOLING_DISTANCE_THRESHOLD = 0.9

# 自测组（self-test）：GT 的产生条件与检索器的 WHERE 条件【同源】。
#
#   a01「芳文社」  GT: tags @> '[{"name":"芳文社"}]'
#                  检索（生产解析出的）: tags @> '[{"name":"芳文社"}]'   ← 同一个条件
#
# 问的人和答的人是同一个 → 融合结果理论上恒为 1.0，方差为零。
# 它不「偏高」，它【零信息量】—— 测不出任何东西。
#
# 正确用途是【回归断言】：基线恒 1.0，任何下跌都说明 keyword 通道坏了。
# 错误的用途是当质量指标：它永远好看，且掩盖其他通道的失败。
#
# ⚠ 2026-09-13 收窄：原来还含 year / score，但"同源"对这两类【不成立】——
# 同源的前提是「查询文本逐字等于过滤值」。生产从查询文本里解析条件：
#   a13「2023年的动画」→ 解析出 {tags:[2023年,动画], year:2023}，GT 只是 year=2023
#                        → 检索严格【窄于】 GT，软兜底的填充项挤掉 GT
#   a15「高分神作」    → GT 是 score>=8.8，但 _MIN_SCORE_RE 要求查询里有"数字+分"，
#                        解析出的 tag=神作 与 GT【不相交】
# 这两类移入 A′ 组（PARSER_GAP_CATEGORIES），其数字是真实测量而非恒 1.0。
#
# exact 类别不在其中：GT 是人工指定的实体 id，检索器仍须靠名字找出来，
# 两者独立 → 是真实测量。
SELF_TEST_CATEGORIES = frozenset({"tag", "career"})

# A′ 解析缺口组：GT 按【理想查询】定义，但生产的规则解析器从查询文本里
# 产不出那个条件（或多产出了别的条件）。数字读作「理想条件 vs 实际解析」的差，
# 不是「检索器好不好」。a15 属此类：没有哪个检索系统能从"高分神作"推出 8.8。
PARSER_GAP_CATEGORIES = frozenset({"year", "score"})

# 分组标签 → 报告里的一句话说明
GROUP_NOTES = {
    "A": "自测组 — GT 与检索同源：返回的每一条必然在 GT 里 → P/NDCG/MRR 恒 1.0；"
         "R@5 偏低是 |GT|≫K 的天花板压的。四个数【都没有信息量】",
    "A'": "解析缺口组 — GT 按理想查询定义，生产规则解析器产不出该条件 "
          "→ 数字是【解析器的差距】，不是检索器的能力",
    "B": "质量组 — GT 是人工指定的实体 id，检索器须靠名字独立找到 → 有信息量 ★",
    "C": "人工组 — 池化偏差（候选池由被测系统产出，漏掉的不会被算作 miss）→ 偏高，仅定性",
}

# 报告里各分组的固定输出顺序（A′ 紧跟 A：两者都是"GT 与解析器"的关系）
GROUP_ORDER = ("A", "A'", "B", "C")

# ═══════════════════════════════════════════════════════════════════════
# 查询定义（从 queries.py 导入）
# ═══════════════════════════════════════════════════════════════════════

from eval.metrics import ndcg_at_k  # noqa: E402
from eval.rag_queries import AUTO_QUERY_DEFS, POOLED_QUERIES  # noqa: E402

# _InstrumentedRetriever 要在【类定义时】拿到基类，所以这个 import 不能像本模块
# 其他地方那样藏进函数里。实测代价 343ms（一次性，不影响 40–60s 的评测本身）。
from rag.retriever import RagEntityRetriever  # noqa: E402

# ═══════════════════════════════════════════════════════════════════════
# 基础设施
# ═══════════════════════════════════════════════════════════════════════

_retriever = None


def _silence_sqlalchemy() -> None:
    """关掉 SQLAlchemy 的 SQL echo —— 否则 --check / --ablate 的结论被刷屏淹没。

    ⚠ 任何 `setLevel(...)` 写法都【无效】，别再用（2026-09-13 实测定论，连
    main.py:62 那种设 `sqlalchemy.engine` 的写法也无效）。原因不是"父级被子的
    显式 level 压过"，而是 echo 的 level 判定【根本不看 logger 的 level】：

      create_engine(echo=True) 会给 engine 造一个 InstanceLogger(echo=True,
      name="sqlalchemy.engine.Engine")，它的 level 来自 `_echo_map[True] = INFO`
      （sqlalchemy/log.py:108 的 InstanceLogger 类文档 + :128 的 _echo_map），
      是【按实例】算的。
      实测：Engine logger 自身的 level 是 NOTSET、isEnabledFor(INFO) 返回 False，
      而 SQL 照刷 —— 因为发日志走的是 InstanceLogger，不是 logging.Logger。

    还必须【两件事一起做】，只做一件都不行（实测）：
      · 只 `propagate = False`（留着 handler）→ 照样刷：handler 是 InstanceLogger
        自己挂的（`_add_default_handler`，仅当该 logger 没有 handler 时挂），
        不经过 root。
      · 只 `handlers.clear()`（留着 propagate）→ 照样刷：记录传到 root 打出来。

    顺序也不能反：engine 是【模块级】创建的，InstanceLogger 和它挂的 handler 产生
    于 `import database.engine` 那一刻。先静音再导入 = 白静音（handler 又挂回来）。
    """
    import database.engine  # noqa: F401  —— 见 docstring，顺序不能反

    for name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.engine.Engine",
                 "sqlalchemy.pool"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.handlers.clear()
        lg.propagate = False


def _get_retriever():
    global _retriever
    if _retriever is None:
        from core.config import get_settings
        from database.engine import engine

        s = get_settings()
        _retriever = _InstrumentedRetriever(
            engine=engine,
            zhipu_api_key=s.ZHIPU_API_KEY,
            zhipu_base_url=s.ZHIPU_BASE_URL,
        )
    return _retriever


class _InstrumentedRetriever(RagEntityRetriever):
    """包住生产检索器，把 _unified_search 丢掉的信息捞回来。

    三个通道都是 retriever 【对象上的方法】，所以子类即插即用 —— 生产代码
    一行不动，评测却拿到了它内部才有的东西：

      · invoked —— 生产融合是【短路】的：keyword 凑满 limit 就不跑 name，
        name 凑满就不跑 vector（tools/bgm_tools.py:1091/1108）。不记这个，
        「短路契约」这条生产行为根本无法断言。
      · errors —— _unified_search 把通道异常降级成 logger.warning。不记这个，
        一次 embedding 故障在报告里完全隐形（和它改造前被 I6 兜住的理由一样）。
      · stage1/stage2 —— 生产 keyword 是两阶段的（严格 AND → 计数软兜底），
        final_score 已经编码了阶段（0.0=阶段1、1.0=阶段2，见
        rag/retriever.py:496-498），所以阶段分布零额外 SQL 就能读出来。

    ⚠ final_score 【不能】用来给融合结果归属通道 —— name_search 相似度为 1.0 时
    final_score 也是 0.0，会被误判成 keyword 命中。所以这里记的是每通道的 id 列表。

    ⚠ keyword_search 内部把异常吞成 `return []`（rag/retriever.py:583-585），
    所以关键词通道的失败在这里【看不见】，只能表现为 n=0。R2（阶段1 非空）
    是它的兜底断言。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.trace: dict[str, dict] = {}
        self.record = False

    def _note(self, channel: str, **kw) -> None:
        if self.record:
            self.trace.setdefault(channel, {}).update(kw)

    def keyword_search(self, **kwargs):
        results = super().keyword_search(**kwargs)
        if self.record:
            # final_score ∈ {0.0, 1.0} 是生产与评测的隐式契约。将来改了打分口径
            # 要【响亮地失败】，不能让阶段统计静默归零（那会让 I2/R2 假绿）。
            odd = [r.final_score for r in results if r.final_score not in (0.0, 1.0)]
            if odd:
                raise AssertionError(
                    f"keyword_search 的 final_score 出现意外取值 {odd[:3]} —— "
                    "阶段判定契约变了，_InstrumentedRetriever 的阶段统计不再可信"
                )
            stage1 = sum(1 for r in results if r.final_score == 0.0)
            self._note(
                "keyword", invoked=True, n=len(results),
                ids=[r.entity_id for r in results],
                stage1=stage1, stage2=len(results) - stage1,
            )
        return results

    def name_search(self, **kwargs):
        results = super().name_search(**kwargs)
        self._note("name", invoked=True, n=len(results),
                   ids=[r.entity_id for r in results])
        return results

    def hybrid_search(self, **kwargs):
        try:
            results = super().hybrid_search(**kwargs)
        except Exception as exc:
            self._note("vector", invoked=True, n=0,
                       error=f"{type(exc).__name__}: {exc}")
            raise
        self._note("vector", invoked=True, n=len(results),
                   ids=[r.entity_id for r in results])
        return results


def _embedding_canary() -> Optional[str]:
    """一次固定的 embedding 调用探活。返回错误串，None = 服务可用。

    必须在任何不变量之前跑：README 记过那种失败模式 —— embedding 服务挂了，
    而报告仍然给出绿色的形状类不变量和一份误导性数字。探活让"环境坏了"
    变成一个响亮的、可归因的、单一原因的失败，而不是随机某个不变量翻红。
    """
    try:
        from core.config import get_settings

        retriever = _get_retriever()
        retriever._check_client()
        retriever.client.embeddings.create(
            model=get_settings().EMBEDDING_MODEL,
            input=["探活"],
        )
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _query_db(sql: str) -> list[str]:
    """执行 SQL 返回 id 列表。"""
    from database.engine import engine
    from sqlmodel import Session, text

    with Session(engine) as s:
        rows = s.exec(text(sql)).all()
    return [row[0] for row in rows]


def _gt_sql_keyword_filters(sql: str) -> dict:
    """从 GT SQL 里反解出「这条查询的 GT 是按什么条件定义的」。

    ⚠ 这【不是】检索路径。检索一律走生产解析器 _extract_keyword_filters
    （从【查询文本】抽条件，tools/bgm_tools.py:969）。本函数的唯一用途是
    给 --check 的棘轮 R3 提供【期望值】：拿它和现场的生产解析结果逐条比，
    分歧集只许缩短不许增长。名字里带 gt_sql_ 就是为了防止后人再接回检索 ——
    它曾接过一次，那正是"评测跑的不是线上那条路"的一半原因。

    支持的 SQL 模式::
        meta_info->'tags' @> '[{"name": "XXX"}]'  →  required_tags: ["XXX"]
        meta_info->>'year' = 'YYYY'               →  year: YYYY
        (meta_info->>'score')::float >= X.X       →  min_score: X.X
        meta_info->'career' ? 'XXX'               →  career: "XXX"
    """
    filters: dict = {}
    tag_match = re.findall(r"""@>\s*'\[\{"name":\s*"([^"]+)"\}\]'""", sql)
    if tag_match:
        filters["required_tags"] = tag_match
    year_match = re.search(r"->>'year'\s*=\s*'(\d{4})'", sql)
    if year_match:
        filters["year"] = int(year_match.group(1))
    score_match = re.search(r"::float\s*>=\s*([\d.]+)", sql)
    if score_match:
        filters["min_score"] = float(score_match.group(1))
    career_match = re.search(r"\?\s*'(\w+)'", sql)
    if career_match:
        filters["career"] = career_match.group(1)
    return filters


# 这里原有一个 _run_keyword_search —— 生产 rag/retriever.py:keyword_search 阶段1
# 的一份【影子实现】。2026-09-13 删除：它只做严格 AND 匹配，没有阶段2 软兜底，
# 于是"keyword 通道找到几条"测的是影子而不是线上。两个实现并存必然继续漂。
# 关键词通道现在由生产 retriever.keyword_search 提供（见 _probe_search）。


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: --build — 生成 Ground Truth
# ═══════════════════════════════════════════════════════════════════════


def build_auto_gt(verbose: bool = True) -> list[dict]:
    """执行 SQL 生成自动 Ground Truth。

    verbose=False 供 --check 用：那条路径要的是 GT 数值，不是 25 行进度。
    """
    def _p(*a, **kw):
        if verbose:
            print(*a, **kw)

    queries = []
    for qid, qtext, etype, stype, cat, sql in AUTO_QUERY_DEFS:
        _p(f"  [{qid}] \"{qtext}\"", end=" ... ")

        if cat == "exact":
            # 精确名称：GT 就是这一个 id
            gt = [sql] if sql else []
        else:
            # 注入 subject_type + nsfw 过滤：SQL 里只写 entity_type 过滤，
            # 这两个由这里自动追加，保证 GT 与检索范围 [完全一致]。
            #
            # nsfw 不能漏：_run_keyword_search 有 `nsfw == False`，检索器被
            # 禁止返回 nsfw 条目。若 GT 不排除它们，分母里就含永远拿不到的
            # 条目，Recall 从理论上就打不满（实测 a18 有 10/58 条是 nsfw）。
            filtered_sql = sql
            if stype is not None:
                filtered_sql = f"{sql} AND subject_type={stype}"
            filtered_sql = f"{filtered_sql} AND nsfw = false"
            gt = _query_db(filtered_sql)

        queries.append({
            "id": qid,
            "query": qtext,
            "entity_type": etype,
            "subject_type": stype,
            "category": cat,
            "ground_truth": gt,
            "gt_size": len(gt),
            "keyword_filters": _gt_sql_keyword_filters(sql),
        })
        _p(f"GT={len(gt)}")

    return queries


def build_pooled_template(existing_auto_queries: list[dict]) -> list[dict]:
    """对每条语义查询池化 top-20 结果，生成人工标注模板。

    池化策略：两路检索取并集（entity_type=all + entity_type=specific），
    去重后取 top-20，附带关键信息方便人工判 relevance。
    """
    retriever = _get_retriever()
    pooled = []

    for pq in POOLED_QUERIES:
        qid = pq["id"]
        qtext = pq["query"]
        etype = pq["entity_type"]
        print(f"  [{qid}] \"{qtext}\"", end=" ... ")

        # 两路池化
        seen: set[str] = set()
        candidates: list[dict] = []

        stype = pq.get("subject_type")
        for search_type in ["all", etype]:
            try:
                results = retriever.hybrid_search(
                    query=qtext,
                    entity_type=search_type,
                    subject_type=stype if search_type != "all" else None,
                    limit=15,
                    distance_threshold=POOLING_DISTANCE_THRESHOLD,
                )
            except Exception:
                continue

            for r in results:
                if r.entity_id not in seen:
                    seen.add(r.entity_id)
                    # 从 rag_output 提取 summary snippet
                    snippet = ""
                    try:
                        ro = json.loads(r.rag_output)
                        snippet = (ro.get("summary") or "")[:120]
                    except (json.JSONDecodeError, TypeError):
                        pass

                    candidates.append({
                        "entity_id": r.entity_id,
                        "name": r.name,
                        "name_cn": r.name_cn or "",
                        "entity_type": r.entity_type,
                        "subject_type": r.subject_type,
                        "snippet": snippet,
                        "relevant": None,  # 待标注
                    })

        # 去重 + 截断
        candidates = candidates[:20]
        pooled.append({
            "id": qid,
            "query": qtext,
            "entity_type": etype,
            "category": pq["category"],
            "description": pq["desc"],
            "candidates": candidates,
        })
        print(f"{len(candidates)} candidates")

    return pooled


def _count_annotated_pooled() -> int:
    """池化文件里已有多少人工作答（relevant 非 null）。"""
    if not POOLED_FILE.exists():
        return 0
    try:
        data = json.loads(POOLED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    return sum(
        1 for q in data.get("queries", [])
        for c in q.get("candidates", [])
        if c.get("relevant") is not None
    )


def cmd_build():
    """--build: 生成 auto GT + 池化模板。"""
    print("=" * 60)
    print("  Phase 1: 自动 Ground Truth")
    print("=" * 60)
    auto_queries = build_auto_gt()

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUTO_GT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "description": "自动生成的 Ground Truth。GT 来自数据库元数据（tag/infobox/career/score），Recall 精确。",
            "queries": auto_queries,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  ✓ {len(auto_queries)} 条自动 GT → {AUTO_GT_FILE.name}")

    # 统计
    categories = {}
    for q in auto_queries:
        c = q["category"]
        categories[c] = categories.get(c, 0) + 1
    for c, n in sorted(categories.items()):
        print(f"    {c}: {n}")

    print()
    print("=" * 60)
    print("  Phase 2: 池化人工标注模板")
    print("=" * 60)

    # 防呆：重建会把全部 relevant 重置为 null，人工标注不可恢复。
    # 想重建就先删/改名该文件 —— 显式动作，不靠"跑个命令顺手"。
    n_annotated = _count_annotated_pooled()
    if n_annotated:
        print(f"  ⚠ 跳过重建 —— {POOLED_FILE.name} 已有 {n_annotated} 个人工作答。")
        print(f"    重建会把 relevant 全部重置为 null（标注不可恢复）。")
        print(f"    确实要重建：先删除或重命名 {POOLED_FILE.name} 再跑 --build。")
        total_candidates = sum(
            len(q.get("candidates", []))
            for q in json.loads(POOLED_FILE.read_text(encoding="utf-8")).get("queries", [])
        )
        print(f"    现有 {total_candidates} 个候选，保持原样。")
    else:
        pooled = build_pooled_template(auto_queries)

        with open(POOLED_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "description": (
                    "池化语义查询 — 需要人工标注 relevance。\n"
                    "每条 query 下有 ~20 个候选实体（带名称+简介片段）。\n"
                    "请将每个 candidate 的 relevant 从 null 改为 true（应该出现在搜索结果中）或 false（不应该）。\n"
                    "标注完成后运行: python -m eval.rag_eval --evaluate"
                ),
                "queries": pooled,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n  ✓ {len(pooled)} 条池化查询 → {POOLED_FILE.name}")
        total_candidates = sum(len(q["candidates"]) for q in pooled)
        print(f"    共 {total_candidates} 个候选实体待标注")

    print()
    print("=" * 60)
    print("  下一步")
    print("=" * 60)
    print(f"  1. 打开 {POOLED_FILE.relative_to(Path.cwd()) if POOLED_FILE.is_relative_to(Path.cwd()) else POOLED_FILE}")
    print(f"  2. 对每个 query 的 candidates，改 relevant: null → true/false")
    print(f"  3. 运行 python -m eval.rag_eval --evaluate")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Phase 2: --evaluate — 合并 GT + 检索 + 算指标
# ═══════════════════════════════════════════════════════════════════════


def _load_annotated_pooled() -> list[dict]:
    """加载人工标注的池化查询，提取 GT。"""
    if not POOLED_FILE.exists():
        print(f"  ⚠ {POOLED_FILE.name} 不存在，跳过池化查询")
        return []

    with open(POOLED_FILE, encoding="utf-8") as f:
        data = json.load(f)

    # subject_type 必须从查询定义回填 —— pooled_annotate.json 里【没有】这个键
    # （实际键只有 candidates/category/description/entity_type/id/query），
    # 所以 `pq.get("subject_type")` 恒为 None。但池化时是按 subject_type=2 圈的池子
    # （build_pooled_template 把它传给了 hybrid_search），评测却一直在宽 3–4 倍的
    # 域上跑 —— 召回域和池化域不一致，C 组数字偏乐观。走生产路径后更明显，
    # 因为更宽的域会喂大阶段2 的填充池。
    stype_by_id = {q["id"]: q.get("subject_type") for q in POOLED_QUERIES}

    queries = []
    unannotated = 0
    for pq in data.get("queries", []):
        gt = []
        for c in pq.get("candidates", []):
            if c.get("relevant") is True:
                gt.append(c["entity_id"])
            elif c.get("relevant") is None:
                unannotated += 1

        queries.append({
            "id": pq["id"],
            "query": pq["query"],
            "entity_type": pq["entity_type"],
            "subject_type": pq.get("subject_type", stype_by_id.get(pq["id"])),
            "category": pq.get("category", "semantic"),
            "source": "human",
            "ground_truth": gt,
            "gt_size": len(gt),
        })

    if unannotated > 0:
        print(f"  ⚠ {unannotated} 个候选实体未标注（relevant=null），已视为不相关")

    return queries


def _production_search(
    q: dict,
    retriever: "_InstrumentedRetriever",
    limit: int = RETRIEVAL_LIMIT,
) -> dict:
    """走【线上真正那条路】：生产过滤器解析 + 生产短路融合。

    这是 --evaluate / --check 的 merged 指标的唯一来源。上面 _probe_search 的
    数字不再充当主结果。

    为什么调 _extract_keyword_filters 和 _unified_search 而不是
    search_local_bangumi 工具本身：工具那层是 asyncio.to_thread + json.loads
    往返，会把 RagSearchResult 的 entity_id 和通道信息全丢掉，而指标是按
    entity_id 算的。这两个函数是工具内部真正干活的同步纯函数，
    _extract_keyword_filters 【不调 LLM】（只传 query 时是纯确定性的），
    所以走它不会给 --check 引入 LLM 抖动。

    返回::
        {
          "merged":   [...],   # 与线上完全一致（短路、两阶段、同一个解析器）
          "keyword_filters": {...},   # 生产从【查询文本】抽出来的条件
          "invoked":  {"keyword": bool, "name": bool, "vector": bool},
          "errors":   {"vector": "..."},          # 通道级失败
          "stages":   {"stage1": n, "stage2": n}, # keyword 两阶段分布
          "channel_ids": {"keyword": [...], "name": [...], "vector": [...]},
        }
    """
    from tools.bgm_tools import _extract_keyword_filters, _unified_search

    entity_type = q.get("entity_type", "all")
    subject_type = q.get("subject_type")

    filters = _extract_keyword_filters(q["query"], entity_type=entity_type)

    retriever.trace = {}
    retriever.record = True
    try:
        merged = _unified_search(
            retriever, q["query"], entity_type, subject_type,
            limit, exclude_nsfw=True, keyword_filters=filters,
        )
    finally:
        retriever.record = False
    trace = retriever.trace

    invoked = {ch: bool(trace.get(ch, {}).get("invoked")) for ch in CHANNELS}
    errors = {
        ch: trace[ch]["error"] for ch in CHANNELS
        if trace.get(ch, {}).get("error")
    }
    kw = trace.get("keyword", {})
    return {
        "merged": [r.entity_id for r in merged],
        "keyword_filters": filters,
        "invoked": invoked,
        "errors": errors,
        "stages": {"stage1": kw.get("stage1", 0), "stage2": kw.get("stage2", 0)},
        "channel_ids": {ch: trace.get(ch, {}).get("ids", []) for ch in CHANNELS},
    }


def _probe_search(
    query: str,
    entity_type: str,
    subject_type: Optional[int] = None,
    limit: int = RETRIEVAL_LIMIT,
    keyword_filters: Optional[dict] = None,
    **ablation_kwargs: bool,
) -> dict:
    """三通道【各自单跑】—— 通道探针，不是线上行为。

    ★ 本函数回答的是「这条通道自己找得到吗」，【不是】「线上表现如何」。
      线上是短路的：keyword 凑满 limit 就不跑 name，name 凑满就不跑 vector
      （tools/bgm_tools.py:1091/1108）。--evaluate 的主结果走 _production_search。

    保留它的唯一理由：这是唯一能产出「该修哪条通道」这类架构结论的仪器。
    融合数字回答不了「某通道贡献 0」是因为它找不到、还是因为被挤出去了 ——
    a01「芳文社」融合后 10/10 满分，但那 10 条全来自 keyword，name 和 vector
    一条没找到。只看融合数字，会以为三条通道都在工作。

    ablation_kwargs 透传给 hybrid_search 的消融开关 —— 消融【只能】在这里做：
    生产 pass 里 20/25 条查询被 keyword 短路，向量通道根本没跑，在那边做消融
    表会几乎全平。那不是"组件没用"，是"组件没跑"。

    返回::

        {
          "merged":  [...],   # 本地复刻的融合（keyword > name > vector 去重截断）
          "keyword": [...],   # 各通道【原始输出】，各自 limit 截断
          "name":    [...],
          "vector":  [...],
          "fusion":  {"keyword": n, "name": n, "vector": n},
          "errors":  {"vector": "RuntimeError: 查询 embedding 失败..."},
        }

    【为什么还要 errors】通道失败原来是 `except: pass` 静默的 —— 而"某通道返回 0 条"
    有两种完全不同的原因：①它真找不到（被测对象的问题）②它根本没跑起来
    （评测工具/网络的问题）。不区分这两者，报告会把一次 embedding 服务故障
    读成"向量通道能力差"。实测撞到过：智谱 API 连接失败时 vector 静默归零，
    融合数字一个字都不变。所以失败必须【显式记下来】。
    """
    errors: dict[str, str] = {}

    retriever = _get_retriever()

    # 通道 1: 关键词 —— 直接调生产方法（含阶段2 软兜底），不再用影子实现
    keyword_ids: list[str] = []
    if keyword_filters:
        retriever.record = False
        try:
            kw_results = retriever.keyword_search(
                entity_type=entity_type, subject_type=subject_type,
                limit=limit, exclude_nsfw=True, **keyword_filters,
            )
            keyword_ids = [r.entity_id for r in kw_results]
        except Exception as e:
            logger.error("keyword 通道失败 %s: %s", query, e)
            errors["keyword"] = f"{type(e).__name__}: {e}"
    keyword_seen = set(keyword_ids)

    # 通道 2: trigram 名称匹配
    name_ids: list[str] = []
    retriever.record = False
    try:
        name_results = retriever.name_search(
            query=query, entity_type=entity_type,
            subject_type=subject_type, limit=limit,
        )
        name_ids = [r.entity_id for r in name_results]
    except Exception as e:
        logger.error("name 通道失败 %s: %s", query, e)
        errors["name"] = f"{type(e).__name__}: {e}"
    name_seen = set(name_ids) | keyword_seen

    # 通道 3: 向量语义匹配 —— 用生产阈值，测的就是线上会返回什么
    try:
        results = retriever.hybrid_search(
            query=query, entity_type=entity_type,
            subject_type=subject_type, limit=limit,
            distance_threshold=PRODUCTION_DISTANCE_THRESHOLD, **ablation_kwargs,
        )
        vector_ids = [r.entity_id for r in results]
    except Exception as e:
        logger.error("vector 通道失败 %s: %s", query, e)
        vector_ids = []
        errors["vector"] = f"{type(e).__name__}: {e}"

    # 合并: 关键词 > 名称 > 向量 去重
    merged = list(keyword_ids)
    for nid in name_ids:
        if nid not in keyword_seen:
            merged.append(nid)
    for vid in vector_ids:
        if vid not in keyword_seen and vid not in name_seen:
            merged.append(vid)
    merged = merged[:limit]

    # 融合结果里各通道各占几条 —— 回答"这个分数是谁挣来的"
    fusion = {"keyword": 0, "name": 0, "vector": 0}
    for eid in merged:
        if eid in keyword_seen:
            fusion["keyword"] += 1
        elif eid in name_seen:      # name_seen 含 keyword_seen，故此处已是 name 独有
            fusion["name"] += 1
        else:
            fusion["vector"] += 1

    return {
        "merged": merged,
        "keyword": keyword_ids[:limit],
        "name": name_ids[:limit],
        "vector": vector_ids[:limit],
        "fusion": fusion,
        "errors": errors,
    }


def _compute_metrics(
    retrieved_ids: list[str],
    gt_ids: set[str],
) -> dict[str, float]:
    """对单条 query 的检索结果计算全部指标。"""
    scores: dict[str, float] = {}
    gt_set = set(gt_ids)

    if not gt_set:
        return scores

    for k in K_VALUES:
        top_k = retrieved_ids[:k]

        # Recall@K
        found = sum(1 for eid in top_k if eid in gt_set)
        scores[f"Recall@{k}"] = found / len(gt_set)

        # Precision@K
        scores[f"Precision@{k}"] = found / k if k > 0 else 0.0

        # Hit Rate@K
        scores[f"Hit@{k}"] = 1.0 if found > 0 else 0.0

    # MRR (使用全部检索结果，不限于 K)
    for i, eid in enumerate(retrieved_ids):
        if eid in gt_set:
            scores["MRR"] = 1.0 / (i + 1)
            break
    else:
        scores["MRR"] = 0.0

    # NDCG@K —— 二值相关度 {id: 1}，IDCG 由 eval.metrics 从【全集 GT】推导，
    # 而不是本次返回结果的降序重排；零命中返回 0.0 而非 1.0。
    #
    # 注意：二值相关度 + |GT| > k 时，NDCG@k 与 Precision@k 单调相关，
    # 信息量有限。它现在的作用是"不再算错"，不是"变敏锐了"。
    graded_gt = {eid: 1 for eid in gt_set}
    for k in K_VALUES:
        scores[f"NDCG@{k}"] = ndcg_at_k(retrieved_ids, graded_gt, k)

    return scores


def _eval_one_production(q: dict, retriever: "_InstrumentedRetriever") -> dict:
    """跑一条查询的【线上检索】+ 指标。--evaluate 与 --check 的主结果都出自这里。

    这是唯一产出 merged 指标的地方。探针 pass（_eval_one_probe）的数字只进
    通道分层表，不进主报告 —— 两者回答的是不同问题，见 _probe_search 的 docstring。

    与生产工具 search_local_bangumi 的【唯一】有意差异是 limit：线上由 LLM 在工具
    参数里定（默认 5），评测固定 RETRIEVAL_LIMIT=10 以支撑 K=1/3/5/10 网格。
    """
    retrieved = _production_search(q, retriever)
    gt = set(q.get("ground_truth", []))
    return {
        "query": q,
        "retrieved": retrieved,
        "metrics": _compute_metrics(retrieved["merged"], gt),
    }


def _eval_one_probe(q: dict, **ablation_kwargs: bool) -> dict:
    """跑一条查询的【三通道探针】+ 指标。通道分层表与 --ablate 用。

    ★ 这里的 merged 是本地复刻的融合，【不是】线上行为（线上是短路的）。
      写进报告时必须标"非生产行为"，否则读者会把它当成线上表现。
    """
    retrieved = _probe_search(
        q["query"], q.get("entity_type", "all"),
        q.get("subject_type"),
        keyword_filters=q.get("keyword_filters"),
        **ablation_kwargs,
    )
    gt = set(q.get("ground_truth", []))
    return {
        "query": q,
        "retrieved": retrieved,
        "metrics": _compute_metrics(retrieved["merged"], gt),
        # 各通道【单独】再算一遍 —— 纯函数，零额外成本。
        # 融合数字回答不了"这条通道自己找得到吗"：keyword 一满员，
        # name/vector 就被挤出 merged，看数字像是它们不存在。
        "channel_metrics": {ch: _compute_metrics(retrieved[ch], gt) for ch in CHANNELS},
    }


def _split_by_gt(queries: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 GT 是否为空分离查询：可参与指标计算的 vs 必须报告的。

    GT 为空的查询不能算指标（分母为 0），但 [不能静默丢弃] —— 它本身
    就是"检索完全跑偏"的证据。典型：p07 的 20 个候选全部被判为不相关，
    旧版 `continue` 让这条从报告里彻底消失，35 条查询只出了 34 条结果。
    """
    usable, dropped = [], []
    for q in queries:
        if q.get("ground_truth"):
            usable.append(q)
        else:
            dropped.append({
                "id": q.get("id"),
                "query": q.get("query"),
                "reason": "GT 为空（候选无一被判为相关）",
            })
    return usable, dropped


def _pad(s: str, width: int, align: str = "<") -> str:
    """按【显示宽度】补齐 —— CJK 占 2 列而 len() 只算 1，直接格式化必然错位。

    query 列混了「芳文社」和「CloverWorks」，不用这个函数表格会参差不齐。
    """
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    fill = " " * max(0, width - w)
    return fill + s if align == ">" else s + fill


def _fmt_filters(kf: dict) -> str:
    """把生产解析出的条件压成一行 —— 报告里要能一眼看出"它到底过滤了什么"。

    kf 为空是【合法】结果（查询里没有可解析的条件），不是解析失败。
    """
    parts = []
    if kf.get("required_tags"):
        parts.append("tags⊇[" + ",".join(kf["required_tags"]) + "]")
    for key, label in (("year", "year="), ("min_score", "score≥"), ("career", "career=")):
        if kf.get(key) is not None:
            parts.append(f"{label}{kf[key]}")
    return " ".join(parts) if parts else "（无过滤条件）"


def _group_of(q: dict) -> str:
    """把查询归入四个可信度不同的组 —— 报告的分层依据。

    A  自测组：GT 与检索同源（见 SELF_TEST_CATEGORIES 注释）→ 恒 1.0，回归断言
    A′ 解析缺口组：GT 是理想查询的结果，生产解析器够不到 → 真实测量，读"差多少"
    B  质量组：GT 由人工指定 id，检索器须独立找到 → 真数字
    C  人工组：人工判的 GT，但候选池是被测系统产出的 → 池化偏差，偏高
    """
    if q.get("source") == "human":
        return "C"
    cat = q.get("category")
    if cat in SELF_TEST_CATEGORIES:
        return "A"
    if cat in PARSER_GAP_CATEGORIES:
        # 组键用 ASCII 撇号：U+2032（′）和 U+0027（'）肉眼几乎分不出，
        # 混用必然在某处 KeyError（这个坑已经踩过一次）。显示时才用 ′。
        return "A'"
    return "B"


def _mean_metrics(rs: list[dict], keys: tuple[str, ...] = ("Recall@5", "Precision@5", "NDCG@5", "MRR")) -> dict:
    """一组结果的指标均值 —— 会自动跳过缺该指标的条目。

    GT 为空的查询 _compute_metrics 返回 {}，若不过滤会算进分母拉低均值。
    """
    out = {}
    for m in keys:
        arr = [r["metrics"][m] for r in rs if m in r["metrics"]]
        if arr:
            out[m] = statistics.mean(arr)
    return out


def cmd_evaluate():
    """--evaluate: 合并 GT + 【生产检索】+ 算指标。

    两趟：
      · 生产 pass（_production_search）—— 主结果。与线上同一解析器、同一融合。
      · 探针 pass（_probe_search）    —— 只喂「通道分层」表，标注为非生产行为。
    """
    # ── 加载 GT ──
    if not AUTO_GT_FILE.exists():
        print(f"错误: {AUTO_GT_FILE.name} 不存在，请先运行 --build")
        return

    with open(AUTO_GT_FILE, encoding="utf-8") as f:
        auto_data = json.load(f)
    auto_queries = auto_data.get("queries", [])
    print(f"自动 GT: {len(auto_queries)} 条")

    human_queries = _load_annotated_pooled()
    print(f"池化标注: {len(human_queries)} 条")

    all_queries = auto_queries + human_queries
    usable_queries, dropped = _split_by_gt(all_queries)
    print(f"合计: {len(all_queries)} 条（其中 {len(dropped)} 条 GT 为空，见报告末尾）\n")

    retriever = _get_retriever()

    # ── 生产 pass：检索 + 算指标 ──
    results: list[dict] = []
    for i, q in enumerate(usable_queries):
        qid = q["id"]
        qtext = q["query"]
        gt = set(q.get("ground_truth", []))

        print(f"  [{i+1:2d}/{len(usable_queries)}] {qid} \"{qtext}\" GT={len(gt)}", end=" ... ")
        r = _eval_one_production(q, retriever)
        print(f"{len(r['retrieved']['merged'])} hits, "
              f"Recall@5={r['metrics'].get('Recall@5', 0):.3f}")
        results.append(r)
        time.sleep(0.05)  # 轻微限流

    n_chan_fail = sum(len(r["retrieved"].get("errors", {})) for r in results)
    if n_chan_fail:
        print()
        print(f"  ⚠⚠ 本次有 {n_chan_fail} 次通道级失败 —— 下面的数字【不可引用】，"
              f"先排掉网络/服务问题再重跑。")
        print("     （--check 的 I8 会直接判 FAIL；明细见结果文件的 channel_errors）")

    # ── 探针 pass：三通道各自单跑，只喂通道分层表 ──
    print()
    print("  探针 pass（三通道各自单跑，只用于下面的「通道分层」表）...")
    for r in results:
        r["probe"] = _eval_one_probe(r["query"])
        time.sleep(0.05)

    # ── 聚合 ──
    all_metrics: dict[str, list[float]] = {}
    for r in results:
        for key, val in r["metrics"].items():
            all_metrics.setdefault(key, []).append(val)

    # ── 报告 ──
    print()
    print("=" * 78)
    print("  RAG 检索评测报告")
    print("=" * 78)
    print(f"  有效 query 数: {len(results)}")
    print(f"  GT 来源: auto={len(auto_queries)}, human={len(human_queries)}")
    print("  检索口径: 生产路径（_extract_keyword_filters → _unified_search，含短路）")
    print(f"  ⚠ 与线上唯一差异: limit={RETRIEVAL_LIMIT}（线上由 LLM 在工具参数里定，默认 5）")
    print()

    groups: dict[str, list[dict]] = {g: [] for g in GROUP_ORDER}
    for r in results:
        groups[_group_of(r["query"])].append(r)

    order = ("Recall@5", "Precision@5", "NDCG@5", "MRR")

    print("── 分组指标 ──")
    print("  各组可信度不同，【不要】平均成一个数。")
    print()
    print(f"  {'组':<5s}{'#Q':>4s}" + "".join(f"{m:>12s}" for m in order))
    print(f"  {'─' * 57}")
    for g in GROUP_ORDER:
        rs = groups[g]
        if not rs:
            continue
        vals = _mean_metrics(rs, order)
        print(f"  {g:<5s}{len(rs):>4d}", end="")
        for m in order:
            print(f"{vals[m]:>12.4f}" if m in vals else f"{'N/A':>12s}", end="")
        print()

    all_vals = _mean_metrics(results, order)
    print(f"  {'─' * 57}")
    print(f"  {'ALL':<5s}{len(results):>4d}", end="")
    for m in order:
        print(f"{all_vals[m]:>12.4f}" if m in all_vals else f"{'N/A':>12s}", end="")
    print()
    print()
    for g in GROUP_ORDER:
        if groups[g]:
            print(f"  {_pad(g, 4)}{GROUP_NOTES[g]}")
    print(f"  {'ALL':<4s}⚠ 含 A 组构造出来的数，拉高整体并稀释真实差异，仅供参考")
    print()

    # ── 生产检索的可观测面：阶段分布 + 短路 ──
    print("── 生产检索 · 阶段分布与短路 ──")
    print("  stage1 = 严格匹配条数（final_score=0.0）；stage2 = 软兜底填充条数。")
    print("  后续通道列为「—」= keyword 已凑满 limit，name/vector 【根本没被调用】。")
    print("  所以这一列是「—」的查询，其向量通道能力在本次评测中【未被测量】。")
    print()
    print(f"  {'id':<6s}" + _pad("query", 22) + f"{'GT':>5s}{'stage1':>8s}{'stage2':>8s}"
          f"{'返回':>5s}   后续通道")
    print(f"  {'─' * 74}")
    short_circuit = 0
    for r in results:
        q = r["query"]
        st = r["retrieved"]["stages"]
        inv = r["retrieved"]["invoked"]
        after = [ch for ch in ("name", "vector") if inv.get(ch)]
        if not after:
            short_circuit += 1
        print(f"  {q['id']:<6s}" + _pad(q["query"][:20], 22)
              + f"{q.get('gt_size', 0):>5d}{st['stage1']:>8d}{st['stage2']:>8d}"
              + f"{len(r['retrieved']['merged']):>5d}   {'+'.join(after) or '—'}")
    print(f"  {'─' * 74}")
    print(f"  {len(results)} 条里 {short_circuit} 条被 keyword 短路（name/vector 未调用）")
    print()

    # ── 通道分层：各通道【单独】检索 ──
    print("── 通道探针 · 各通道【单独】跑的 Recall@5 ──")
    print("  ⚠ 这不是线上行为：线上是短路的，被短路的通道根本没跑（见上表）。")
    print("    它回答的是「这条通道自己找得到吗」—— 唯一能回答「该修哪条通道」的仪器。")
    print("     C 组无 keyword 探针：池化查询没有从 GT SQL 反解出的条件可注入。")
    print()
    print(f"  {'组':<5s}{'#Q':>4s}" + "".join(f"{ch:>10s}" for ch in CHANNELS) + f"{'融合':>10s}")
    print(f"  {'─' * 53}")
    for g in GROUP_ORDER:
        rs = groups[g]
        if not rs:
            continue
        print(f"  {g:<5s}{len(rs):>4d}", end="")
        for ch in CHANNELS:
            arr = [r["probe"]["channel_metrics"][ch]["Recall@5"] for r in rs
                   if "Recall@5" in r["probe"]["channel_metrics"][ch]]
            print(f"{statistics.mean(arr):>10.4f}" if arr else f"{'N/A':>10s}", end="")
        arr = [r["probe"]["metrics"]["Recall@5"] for r in rs if "Recall@5" in r["probe"]["metrics"]]
        print(f"{statistics.mean(arr):>10.4f}" if arr else f"{'N/A':>10s}")
    print()
    # 融合可能【低于】单通道 —— 优先级高的通道用不相关结果挤掉了后面的相关结果。
    # 这是只看融合数字永远发现不了的，必须显式指出来。
    for g in GROUP_ORDER:
        rs = groups[g]
        if not rs:
            continue
        cand = [r["probe"]["metrics"]["Recall@5"] for r in rs if "Recall@5" in r["probe"]["metrics"]]
        if not cand:
            continue
        m = statistics.mean(cand)
        best_ch, best_v = None, -1.0
        for ch in CHANNELS:
            arr = [r["probe"]["channel_metrics"][ch]["Recall@5"] for r in rs
                   if "Recall@5" in r["probe"]["channel_metrics"][ch]]
            if arr and statistics.mean(arr) > best_v:
                best_ch, best_v = ch, statistics.mean(arr)
        if best_ch and m < best_v - 1e-9:
            print(f"  ⚠ {g} 组：探针融合({m:.4f}) < {best_ch} 单独({best_v:.4f}) —— "
                  f"融合把 {best_ch} 的相关结果挤出了前 10（优先级更高的通道占了名额）")
    print()

    # ── A 组逐条：GT 与解析器到底同源到哪一步 ──
    for g in ("A", "A'"):
        if not groups[g]:
            continue
        if g == "A":
            print("── A 自测组逐条 · 生产解析是否复现了 GT 的 WHERE ──")
            print("  stage1 == GT 条数 ⇒ 生产解析复现了 GT 的 WHERE（同源前提成立）。")
        else:
            print("── A′ 解析缺口组逐条 · 生产解析达不到 GT 的地方 ──")
            print("  stage1 < GT  ⇒ 解析出的条件比 GT 窄，stage2 填充项挤掉本该在前面的 GT。")
            print("  stage1 == 0 且 GT>0 ⇒ 解析出的条件与 GT 【不相交】，返回的全是别的实体。")
        print()
        print(f"  {'id':<6s}" + _pad("query", 22) + f"{'GT':>5s}{'stage1':>8s}{'stage2':>8s}"
              f"{'返回':>5s}   生产解析出的条件")
        print(f"  {'─' * 92}")
        for r in groups[g]:
            q = r["query"]
            st = r["retrieved"]["stages"]
            kf = r["retrieved"]["keyword_filters"]
            print(f"  {q['id']:<6s}" + _pad(q["query"][:20], 22)
                  + f"{q.get('gt_size', 0):>5d}{st['stage1']:>8d}{st['stage2']:>8d}"
                  + f"{len(r['retrieved']['merged']):>5d}   {_fmt_filters(kf)}")
        print()

    # ── 未参与统计的查询 ──
    if dropped:
        print()
        print("── 未参与统计的查询 ──")
        for d in dropped:
            print(f"  {d['id']} \"{d['query']}\" — {d['reason']}")
        print(f"  （共 {len(dropped)} 条；GT 为空无法算指标，但检索跑偏本身就是结果）")

    print()

    # ── 保存详细结果 ──
    merged = {
        "description": "合并后的 Ground Truth + 检索结果（生产路径）",
        "retrieval": "production (_extract_keyword_filters → _unified_search，含短路)",
        "retrieval_limit": RETRIEVAL_LIMIT,
        "group_legend": {g: GROUP_NOTES[g] for g in GROUP_ORDER},
        "dropped_queries": dropped,
        "queries": [
            {
                "id": r["query"]["id"],
                "query": r["query"]["query"],
                "entity_type": r["query"]["entity_type"],
                "category": r["query"]["category"],
                "source": r["query"].get("source", "auto"),
                "group": _group_of(r["query"]),
                "gt_size": len(r["query"].get("ground_truth", [])),
                "retrieved_count": len(r["retrieved"]["merged"]),
                "metrics": r["metrics"],
                # 生产解析器从【查询文本】抽出来的条件 —— 想知道"为什么返回这些"看这里
                "keyword_filters": r["retrieved"]["keyword_filters"],
                # 短路后还跑了哪些通道（False 的通道本次【未被测量】）
                "invoked": r["retrieved"]["invoked"],
                # stage1 严格匹配 / stage2 软兜底填充
                "stages": r["retrieved"]["stages"],
                # 通道级失败（空 dict = 已调用的通道都跑起来了）
                "channel_errors": r["retrieved"].get("errors", {}),
                # 探针 pass —— 非生产行为，仅用于通道分层
                "probe_metrics": r["probe"]["metrics"],
                "probe_channel_metrics": r["probe"]["channel_metrics"],
                "probe_fusion": r["probe"]["retrieved"]["fusion"],
            }
            for r in results
        ],
    }
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(MERGED_GT_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"  详细结果已保存到 {MERGED_GT_FILE}")


# ═══════════════════════════════════════════════════════════════════════
# Phase 3: --check — 回归不变量（轴 2 的"基线"）
# ═══════════════════════════════════════════════════════════════════════
#
# 轴 2 【没有质量基线】，而且不该有：
#
#   A 组 分数是构造的（GT 与检索共用同一个 WHERE 子句）→ 零信息量
#   B 组 分数到顶了（R@5 = MRR = 1.0）                  → 只能跌，测不出涨
#   C 组 池化偏差（候选池由被测系统自己产出）            → 偏高，不可引用
#
# 冻结这三个里的任何一个，冻的都是一个解释不了的东西。所以这条轴交付的不是
# 分数，而是【不变量】：一条可辩护的结构性质 —— 它成立时不说明系统好，
# 破掉时一定说明系统坏了。这恰好就是"这次改动弄坏了没有"。
#
# 三条设计约束（缺任何一条，这批断言就会退化成又一个 A 组）：
#   1. 【手写】不变量由人写下，不由当前运行结果自动生成。自动生成 = 又一次
#      考卷与考题同源 —— 正是 A 组的病根。
#   2. 【对库漂移免疫】A 组的 GT 与检索同源、两者一起动，所以这里【当场重算】
#      GT（build_auto_gt），不用冻结的 gt_size：灌了新数据，I1 照样成立。
#   3. 【可归因】每条不变量指向一个具体坏法，不是"分数跌了"。
#
# 退出码：0 = 全部成立，1 = 有不变量破掉（可直接挂 CI / pre-push）。
# 想看数字跑 --evaluate；这里只回答"坏没坏"。
# ═══════════════════════════════════════════════════════════════════════
# 不变量实现 —— 每条都是【一对一纯函数】，便于单测，也便于失败时直接指向它
# ═══════════════════════════════════════════════════════════════════════
#
# 硬不变量：必须零违规。成立时只说明"没坏"，破掉时指向一个具体坏法。
# 棘轮不变量：违规集必须【恰好等于】冻结集。今天就是红的，作用是把已知缺陷
#             有条件地放进门槛 —— 新增即 FAIL，修好也得 FAIL 到有人跑 --freeze
#             把改进锁进去（见 RATCHET_SHRINK_IS_FAILURE）。

_Violations = list[tuple[str, str, str]]   # (id, query, 为什么)


def _inv_keyword_shape(results: list[dict]) -> _Violations:
    """I1 关键词通道形状契约（全查询）。

    keyword_search 的契约：阶段1 严格匹配（final_score=0.0）优先，不足 limit 时
    阶段2 软兜底补齐（final_score=1.0）。所以返回条数必须满足
    `stage1 <= len <= limit`，且 stage1/stage2 恰好把返回的条数分完。

    破掉说明：阶段2 没补齐 / 补齐越界 / 结果被截断 —— 任一处都让 stage1 这个
    观测点失真。也顺带断言"无过滤条件 ⇒ 一条都不返回"：_unified_search 只在
    keyword_filters 非空时调这条通道，若它无条件下场，报的就不是线上行为。
    """
    bad: _Violations = []
    for r in results:
        q, ret = r["query"], r["retrieved"]
        kf = ret["keyword_filters"]
        kw = ret["channel_ids"]["keyword"]
        st = ret["stages"]
        if not kf:
            if kw or st["stage1"] or st["stage2"]:
                bad.append((q["id"], q["query"],
                            f"无过滤条件却返回 {len(kw)} 条"))
            continue
        if len(kw) > RETRIEVAL_LIMIT:
            bad.append((q["id"], q["query"], f"返回 {len(kw)} 条 > limit {RETRIEVAL_LIMIT}"))
        if st["stage1"] > len(kw):
            bad.append((q["id"], q["query"], f"stage1={st['stage1']} > 返回 {len(kw)} 条"))
        if st["stage2"] != len(kw) - st["stage1"]:
            bad.append((q["id"], q["query"],
                        f"stage1({st['stage1']})+stage2({st['stage2']}) != 返回 {len(kw)} 条"))
    return bad


def _inv_group_a_roundtrip(results: list[dict]) -> _Violations:
    """I2 A 组自测回环（tag + career，15 条）。

    这 15 条的前提是"查询文本逐字等于过滤值"，于是生产解析出的 WHERE 与 GT 的
    WHERE 是同一个（由 I5 断言）。前提成立时，检索要多少有多少 —— 所以
    阶段1 必须【恰好】填出 min(|GT|, limit) 条，且一条软兜底都不需要。

    破掉说明：生产解析复现不了 GT SQL 的 WHERE（前提失效），或检索层坏了
    （jsonb contains / entity_type / nsfw 过滤 / 融合截断任一处出错）。

    旧版这条写的是 len == min(|GT|, limit)，走生产路径后它仍然全过但【已经没有
    内容】：软兜底填充能把任何条数凑到 limit。把等号挪到 stage1 上才重新有意义。
    """
    bad: _Violations = []
    for r in results:
        q, ret = r["query"], r["retrieved"]
        if _group_of(q) != "A":
            continue
        want = min(len(q.get("ground_truth", [])), RETRIEVAL_LIMIT)
        kw = ret["channel_ids"]["keyword"]
        st = ret["stages"]
        if st["stage1"] != want or len(kw) != want or st["stage2"] != 0:
            bad.append((q["id"], q["query"],
                        f"stage1={st['stage1']} stage2={st['stage2']} "
                        f"返回={len(kw)}，应全部为 {want}"))
    return bad


def _inv_shortcircuit(results: list[dict]) -> _Violations:
    """I3 短路契约（全查询）—— 生产融合的【成本特征】，不是质量特征。

    _unified_search（tools/bgm_tools.py:1044）逐通道检查 running 长度：
    凑够 limit 就不再调下一条通道。这条契约决定了线上每次搜索的延迟和
    embedding 花销，拆掉它不会有任何指标变化 —— 只会让每次搜索都多付
    一次向量检索。所以它必须被单独断言。

    谓词用【等价】而不是蕴含：等价与蕴含在这套结构下是同一个陈述
    （见 _probe_search 的说明），但等价读起来是完整契约。
    """
    bad: _Violations = []
    for r in results:
        q, ret = r["query"], r["retrieved"]
        kw = ret["channel_ids"]["keyword"]
        nm = ret["channel_ids"]["name"]
        inv = ret["invoked"]
        limit = RETRIEVAL_LIMIT
        want_kw = bool(ret["keyword_filters"])
        want_name = len(kw) < limit
        want_vec = len(set(kw) | set(nm)) < limit
        for ch, want in (("keyword", want_kw), ("name", want_name), ("vector", want_vec)):
            if inv.get(ch) != want:
                bad.append((q["id"], q["query"],
                            f"{ch} 通道 invoked={inv.get(ch)}，短路契约要求 {want}"))
    return bad


def _inv_exact_name(results: list[dict]) -> _Violations:
    """I4 B 组精确名称排第一（exact，7 条）。

    GT 是人工指定的【单个】实体 id，与检索器完全独立 —— 轴 2 唯一的真测量。
    MRR < 1.0 = 名字找得到但没排第一（trigram 通道退化），或 keyword 用无关
    结果把它挤出了前 10。
    """
    bad: _Violations = []
    for r in results:
        q = r["query"]
        if _group_of(q) != "B":
            continue
        mrr = r["metrics"].get("MRR", 0.0)
        rec = r["metrics"].get("Recall@5", 0.0)
        if mrr < 1.0 or rec < 1.0:
            bad.append((q["id"], q["query"], f"MRR={mrr:.4f} Recall@5={rec:.4f}"))
    return bad


def _inv_group_premise(results: list[dict]) -> _Violations:
    """I5 分组前提成立：A 组的生产解析结果必须【逐字等于】GT SQL 反解出的条件。

    A 组"零信息量"这个读法完全建立在"两者同源"上。这条断言是把那个前提
    变成机械约束 —— 前提一旦悄悄失效，A 组的数字就不再是自测组，而报告
    里没有任何东西会提示这一点。

    只查 A 组：A′ 组的分歧是【预期】的（那正是它存在的理由），由 R3 登记。
    """
    bad: _Violations = []
    for r in results:
        q, ret = r["query"], r["retrieved"]
        if _group_of(q) != "A":
            continue
        want = _gt_sql_keyword_filters(_gt_sql_of(q["id"]))
        got = ret["keyword_filters"]
        if _canon_filters(want) != _canon_filters(got):
            bad.append((q["id"], q["query"],
                        f"GT SQL {_fmt_filters(want)} ≠ 生产解析 {_fmt_filters(got)}"))
    return bad


def _inv_no_silent_empty(results: list[dict]) -> _Violations:
    """I6 无查询静默返回空。

    "检索没崩、也没报错、就是什么都没给"是所有失败里最难发现的一种 ——
    它在下游表现为"AI 不知道"，而不是"AI 报错"。GT 非空的查询理应拿得到东西。
    """
    return [(r["query"]["id"], r["query"]["query"], "GT 非空但检索返回 0 条")
            for r in results if not r["retrieved"]["merged"]]


def _inv_dropped_unchanged(dropped: list[dict]) -> _Violations:
    """I7 GT 为空的清单不变（已知只有 p07）。

    GT 被标注改动而变空 → 那条查询会从统计里静默消失（旧版就是 `continue`）。
    """
    got = [d["id"] for d in dropped]
    if got == EXPECTED_EMPTY_GT:
        return []
    return [("I7", "", f"GT 为空清单 {got}，应为 {EXPECTED_EMPTY_GT}")]


def _inv_instrument_health(canary_err: Optional[str],
                           prod: list[dict], probe: list[dict]) -> _Violations:
    """I8 仪器健康：探活可用 ∧ 两趟 pass 均无通道级失败。

    "某通道返回 0 条"有两种完全不同的原因：①它真找不到（被测对象的问题）
    ②它根本没跑起来（测量仪器的问题）。不区分这两者，报告会把一次 embedding
    服务故障读成"向量通道能力差"。

    实测撞到过：智谱 API 连接失败 → vector 静默归零，而 A/B 组的融合数字
    【一个字都不变】（keyword/name 把名额填满了），从报告上完全看不出来。

    这是"本次任何数字能不能读"的总闸。canary 已经先跑过一次并提前返回，
    这里是兜底：pass 中途服务挂了同样要 FAIL。
    """
    bad: _Violations = []
    if canary_err:
        bad.append(("I8", "", f"探活失败：{canary_err}"))
    for tag, rs in (("生产", prod), ("探针", probe)):
        for r in rs:
            for ch, msg in r["retrieved"].get("errors", {}).items():
                bad.append((r["query"]["id"], r["query"]["query"],
                            f"{tag} pass {ch} 通道失败：{msg}"))
    return bad


# ── 棘轮 ──

def _rat_parse_domain_support(live_auto: list[dict]) -> dict[str, str]:
    """R1 解析域内支持：required_tags 里每个 tag 在查询域内 COUNT ≥ 1。

    域 = entity_type × subject_type × nsfw=false —— 与 keyword_search 的
    WHERE 逐项一致。tag 在域内一条都没有 = 这个条件永远 AND 死结果集。

    这是【有预言机】的一层：能指名到底哪个 tag 坏。
    """
    from database.engine import engine
    from sqlmodel import Session, text as sa_text

    bad: dict[str, str] = {}
    with Session(engine) as s:
        for q in live_auto:
            etype = q.get("entity_type", "subject")
            stype = q.get("subject_type")
            kf = _extract_filters(q)
            tags = kf.get("required_tags") or []
            if not tags:
                continue
            where = ["nsfw = false"]
            params: dict = {}
            if etype != "all":
                where.append("entity_type = :etype")
                params["etype"] = etype
            if stype is not None:
                where.append("subject_type = :stype")
                params["stype"] = stype
            missing = []
            for t in tags:
                sql = ("SELECT COUNT(*) FROM rag_entities WHERE "
                       + " AND ".join(where)
                       + " AND meta_info->'tags' @> :pat")
                # ⚠ 必须用 execute().scalar_one()，不能用 exec().one()：
                # SQLModel 的 one() 返回 Row 而不是 int，而 Row 不是 tuple 的
                # 子类（isinstance(row, tuple) 为 False），`if not n` 于是恒假 ——
                # 这条棘轮会【永远报 0 违规】。踩过一次。
                n = s.execute(sa_text(sql), {**params,
                                             "pat": json.dumps([{"name": t}])}
                              ).scalar_one()
                if not n:
                    missing.append(t)
            if missing:
                bad[q["id"]] = f"tag {missing} 在域内 0 条匹配（全部 {tags}）"
    return bad


def _rat_stage1_nonempty(results: list[dict]) -> dict[str, str]:
    """R2 阶段1 非空：有过滤条件 ⇒ 阶段1 至少命中 1 条。

    与 R1 是【不同层】：R1 逐 tag 查库（有预言机），R2 只读通道自己的输出
    （无预言机）。所以 R2 能抓到 R1 看不见的「AND 为空」类 —— p01/p05 每个
    tag 单独都有支持，四个标签同时 AND 却是空的，于是 10 条全是填充。
    """
    bad: dict[str, str] = {}
    for r in results:
        q, ret = r["query"], r["retrieved"]
        if ret["keyword_filters"] and ret["stages"]["stage1"] == 0:
            bad[q["id"]] = (f"解析出 {_fmt_filters(ret['keyword_filters'])} "
                            f"但阶段1 命中 0 条，返回的 {len(ret['channel_ids']['keyword'])} 条全是软兜底")
    return bad


def _rat_fingerprint(prod_kf: dict[str, dict]) -> dict[str, dict]:
    """R3 解析指纹：全量 {id: canonical(生产解析结果)}（自动 25 + 池化 9）。

    前两条棘轮都看不见 a13/a14/a15 那类漂移：a13 的 stage1=6（非零），每个 tag
    也都在域内，只有字典本身是错的。R3 抓的是【跨字段冗余】：a13 解析出的
    `2023年` 是个【合法】标签（域内 50 条匹配），R1 会放行，但它和 `year: 2023`
    重复 —— 而 _drop_substring_tags 看不见，它只在 tag 列表【内部】去重。

    这是【输入侧】棘轮：它不判断对错，只保证输入不变。任何变化都要人看一眼。
    """
    return {qid: _canon_filters(kf) for qid, kf in sorted(prod_kf.items())}


def _canon_filters(kf: dict) -> dict:
    """把过滤条件规范化成可逐字比较的形式（tag 列表排序，键排序）。"""
    out = {}
    for k in sorted(kf):
        v = kf[k]
        out[k] = sorted(v) if isinstance(v, list) else v
    return out


def _extract_filters(q: dict) -> dict:
    """现场调用【生产】解析器 —— 不是 eval 的影子实现，见 _gt_sql_keyword_filters。"""
    from tools.bgm_tools import _extract_keyword_filters

    return _extract_keyword_filters(
        query=q["query"], entity_type=q.get("entity_type", "subject")
    )


def _gt_sql_of(qid: str) -> str:
    """从查询定义表里取回该 id 的 GT SQL（live_auto 的条目不自带）。"""
    for qid_, _qtext, _etype, _stype, _cat, sql in AUTO_QUERY_DEFS:
        if qid_ == qid:
            return sql or ""
    return ""


# ═══════════════════════════════════════════════════════════════════════
# 基线文件与冻结
# ═══════════════════════════════════════════════════════════════════════

RATCHET_KEYS = ("parse_domain_support", "keyword_stage1_nonempty",
                "extraction_fingerprint")


def _load_baseline() -> dict:
    if not CHECK_BASELINE_FILE.exists():
        return {}
    return json.loads(CHECK_BASELINE_FILE.read_text(encoding="utf-8"))


def cmd_freeze(reason: str) -> int:
    """--check --freeze: 收紧棘轮基线。只能删条目，不能加。

    棘轮要能【在物理上】无法被放宽，靠的就是这条：新增缺陷永远被拒 ——
    想让它过，只能去修代码，不能来改文件。
    """
    if not reason:
        print("  错误: --freeze 必须带 --reason（写进基线的 why 字段）")
        return 1

    old = _load_baseline()
    live_auto = build_auto_gt(verbose=False)
    retriever = _get_retriever()
    usable, _ = _split_by_gt(live_auto + _load_annotated_pooled())
    prod = [_eval_one_production(q, retriever) for q in usable]

    r1 = _rat_parse_domain_support(live_auto)
    r2 = _rat_stage1_nonempty(prod)
    r3 = _rat_fingerprint({r["query"]["id"]: r["retrieved"]["keyword_filters"]
                           for r in prod})
    today = datetime.date.today().isoformat()

    new = {
        "schema": 2,
        "frozen_at": today,
        "note": "手写冻结。【只允许收紧】：--freeze 只能删除条目，不能新增。"
                "新增缺陷必须去修代码。",
        "parse_domain_support": {k: {"why": v, "at": today} for k, v in r1.items()},
        "keyword_stage1_nonempty": {k: {"why": v, "at": today} for k, v in r2.items()},
        "extraction_fingerprint": r3,
    }

    # 拒绝新增：棘轮不能被放宽。
    # 基线不存在时（首次建立）没有"新增"可言 —— 整份都是新建的，
    # 所以这个闸只在【已有基线】时生效。否则棘轮永远建不起来。
    added = []
    if old:
        for key in ("parse_domain_support", "keyword_stage1_nonempty"):
            fresh = set(new[key]) - set(old.get(key, {}))
            for k in sorted(fresh):
                added.append(f"{key}/{k}：{new[key][k]['why']}")
        if old.get("extraction_fingerprint"):
            for qid, kf in r3.items():
                if qid not in old["extraction_fingerprint"]:
                    added.append(f"extraction_fingerprint/{qid}：{kf}")
    if added:
        print("  ✗ --freeze 拒绝执行：本次出现了【新增】条目。棘轮只许收紧。")
        for a in added:
            print(f"      {a}")
        print("    新增缺陷必须修代码，不能冻进基线。")
        return 1

    print(f"  收紧前: R1={len(old.get('parse_domain_support', {}))} 条  "
          f"R2={len(old.get('keyword_stage1_nonempty', {}))} 条")
    print(f"  收紧后: R1={len(r1)} 条  R2={len(r2)} 条  R3={len(r3)} 条")
    for key, cur in (("parse_domain_support", r1), ("keyword_stage1_nonempty", r2)):
        shrunk = sorted(set(old.get(key, {})) - set(cur))
        if shrunk:
            print(f"  {key} 移出: {', '.join(shrunk)}（已修好）")
    print(f"  why: {reason}")

    CHECK_BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECK_BASELINE_FILE.write_text(
        json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  已写入 {CHECK_BASELINE_FILE}")
    return 0


def _print_ratchet(name: str, cur: dict, frozen: dict,
                   is_fingerprint: bool = False) -> tuple[bool, list[str], int, int]:
    """判定一条棘轮。返回 (是否通过, 归因提示行, 新增数, 可收紧数)。

    两个方向都报，不在第一个非空时提前返回 —— 一次篡改同时制造"新增"和
    "可收紧"时，只报前者会让人以为后者不存在（自检时踩到过）。
    """
    cur_ids, frozen_ids = set(cur), set(frozen)
    added = sorted(cur_ids - frozen_ids)
    removed = sorted(frozen_ids - cur_ids)
    if is_fingerprint:
        # 指纹没有"改善"方向：任何变化都是违规，都要人看一眼
        detail = [f"{name} 与冻结指纹不符: {', '.join(added)}"] if added else []
        return (not added), detail, len(added), 0
    msgs = []
    if added:
        msgs.append(f"{name} 新增缺陷（须修代码）: {', '.join(added)}")
    if removed and RATCHET_SHRINK_IS_FAILURE:
        msgs.append(f"{name} 基线可收紧（跑 --freeze --reason '...'）: {', '.join(removed)}")
    return (not msgs), msgs, len(added), len(removed)


def cmd_check(freeze: bool = False, probe: bool = True, reason: str = "") -> int:
    """--check: 跑检索 + 断言回归不变量。返回退出码（0 = PASS，1 = FAIL）。"""
    _silence_sqlalchemy()

    # ── 第 0 步：仪器探活 ──
    #
    # 必须在任何断言之前。README 记过那种失败模式：embedding 服务挂了，报告
    # 仍然给出绿色的形状类不变量和一份误导性数字。探活把"环境坏了"变成一个
    # 响亮的、单一原因的失败，还省掉 40 秒注定失败的调用。
    #
    # 退出码维持 0/1（不引入第三态）：归因由这段文字承担，不由退出码承担 ——
    # CI 只关心"能不能过"，人关心"为什么没过"。
    canary_err = _embedding_canary()
    if canary_err:
        print()
        print("=" * 74)
        print("  环境不可用（embedding 服务）：" + canary_err)
        print("  本次【未测任何不变量】—— 不要把这次当成 PASS 或 FAIL 读。")
        print("=" * 74)
        return 1

    if freeze:
        return cmd_freeze(reason)

    # ── GT 当场重算（约束 2：对库漂移免疫）──
    print("  现场重算自动 GT …")
    live_auto = build_auto_gt(verbose=False)
    print(f"  自动 GT {len(live_auto)} 条（现场，不用冻结文件）")

    # 冻结文件只用来报「过期」，不参与任何断言
    stale: list[str] = []
    if AUTO_GT_FILE.exists():
        frozen_gt = {
            q["id"]: q.get("gt_size", 0)
            for q in json.loads(AUTO_GT_FILE.read_text(encoding="utf-8")).get("queries", [])
        }
        stale = [q["id"] for q in live_auto
                 if q["id"] in frozen_gt and frozen_gt[q["id"]] != q["gt_size"]]
    else:
        print(f"  ⚠ {AUTO_GT_FILE.name} 不存在 —— 不影响本次检查，但 --evaluate 需要它")

    human_queries = _load_annotated_pooled()
    usable_queries, dropped = _split_by_gt(live_auto + human_queries)
    if stale:
        print(f"  ⚠ GT 文件已过期：{', '.join(stale)} 的 |GT| 与库不一致（跑 --build 刷新）")
    print()

    retriever = _get_retriever()

    # ── 生产 pass ──
    print("  生产 pass（线上路径）…")
    prod: list[dict] = []
    for i, q in enumerate(usable_queries):
        print(f"  [{i+1:2d}/{len(usable_queries)}] {q['id']:<5s}", end=" ... ", flush=True)
        r = _eval_one_production(q, retriever)
        print(f"{len(r['retrieved']['merged']):>2d} hits")
        prod.append(r)
        time.sleep(0.05)

    # ── 探针 pass ──
    probe_r: list[dict] = []
    if probe:
        print("  探针 pass（三通道各自单跑，仅用于 I3 的形状与报告）…")
        for q in usable_queries:
            probe_r.append(_eval_one_probe(q))
            time.sleep(0.05)

    baseline = _load_baseline()
    has_baseline = bool(baseline)

    # ── 硬不变量 ──
    i1 = _inv_keyword_shape(prod)
    i2 = _inv_group_a_roundtrip(prod)
    i3 = _inv_shortcircuit(prod)
    i4 = _inv_exact_name(prod)
    i5 = _inv_group_premise(prod)
    i6 = _inv_no_silent_empty(prod)
    i7 = _inv_dropped_unchanged(dropped)
    i8 = _inv_instrument_health(canary_err, prod, probe_r)

    by_group: dict[str, list[dict]] = {g: [] for g in GROUP_ORDER}
    for r in prod:
        by_group[_group_of(r["query"])].append(r)

    # ── 棘轮 ──
    r1_live = _rat_parse_domain_support(live_auto)
    r2_live = _rat_stage1_nonempty(prod)
    r3_live_c = _rat_fingerprint(
        {r["query"]["id"]: r["retrieved"]["keyword_filters"] for r in prod})
    r3_frozen_c = _rat_fingerprint(baseline.get("extraction_fingerprint", {}))

    if has_baseline:
        r1_ok, r1_msg, r1_add, r1_rem = _print_ratchet(
            "R1", r1_live, baseline.get("parse_domain_support", {}))
        r2_ok, r2_msg, r2_add, r2_rem = _print_ratchet(
            "R2", r2_live, baseline.get("keyword_stage1_nonempty", {}))
        r3_ok, r3_msg, r3_add, r3_rem = _print_ratchet(
            "R3", r3_live_c, r3_frozen_c, is_fingerprint=True)
    else:
        r1_ok = r2_ok = r3_ok = False
        r1_add = r2_add = r3_add = 0
        r1_rem = r2_rem = r3_rem = 0
        r1_msg = ["基线文件不存在 —— 跑 --check --freeze --reason '建立基线'"]
        r2_msg = r1_msg
        r3_msg = r1_msg

    checks = [
        ("I1", "关键词通道形状契约", not i1,
         f"{len(prod) - len(i1)}/{len(prod)}"),
        ("I2", "A 组自测回环 (stage1 精确)", not i2,
         f"{len(by_group['A']) - len(i2)}/{len(by_group['A'])}"),
        ("I3", "短路契约 (生产融合成本特征)", not i3,
         f"{len(prod) - len(i3)}/{len(prod)}"),
        ("I4", "B 组精确名称排第一 (MRR=1.0)", not i4,
         f"{len(by_group['B']) - len(i4)}/{len(by_group['B'])}"),
        ("I5", "A 组前提成立 (解析==GT SQL)", not i5,
         f"{len(by_group['A']) - len(i5)}/{len(by_group['A'])}"),
        ("I6", "无查询静默返回空", not i6,
         f"{len(prod) - len(i6)}/{len(prod)}"),
        ("I7", "GT 为空清单不变", not i7,
         f"{len(dropped)} 条"),
        ("I8", "仪器健康 (探活+通道失败)", not i8,
         f"{len(i8)} 次"),
    ]
    ratchets = [
        ("R1", "解析域内支持 (逐 tag 有预言机)", r1_ok, r1_live, r1_msg, r1_add, r1_rem),
        ("R2", "阶段1 非空 (无预言机, 抓 AND 为空)", r2_ok, r2_live, r2_msg, r2_add, r2_rem),
        ("R3", "解析指纹 (输入侧, 抓跨字段冗余)", r3_ok, r3_live_c, r3_msg, r3_add, r3_rem),
    ]

    print()
    print("=" * 74)
    print("  轴 2 回归不变量")
    print("=" * 74)
    print("  断的是【结构性质】，不是分数。成立不说明系统好，破掉一定说明系统坏了。")
    print("  检索口径: 生产路径（_extract_keyword_filters → _unified_search，含短路）")
    print()
    for code, desc, ok, detail in checks:
        print(f"  {code}  {_pad(desc, 34)}{detail:>9s}   {'PASS' if ok else 'FAIL'}")
    print()
    print("  ── 棘轮（违规集必须恰好等于冻结集）──")
    for code, desc, ok, cur, _msg, n_add, n_rem in ratchets:
        frozen_n = len(baseline.get(
            {"R1": "parse_domain_support", "R2": "keyword_stage1_nonempty",
             "R3": "extraction_fingerprint"}[code], {}))
        mark = "PASS" if ok else f"FAIL 新增{n_add}/可收紧{n_rem}"
        print(f"  {code}  {_pad(desc, 34)}{len(cur):>4d}/{frozen_n:<4d}   {mark}")

    # ── 失败细节 ──
    details = [
        ("I1", "keyword 通道形状契约破了（阶段2 补齐越界/截断）：", i1),
        ("I2", "A 组自测回环破了 —— 先看 I5 是否同时红（前提失效），"
               "否则是检索层坏了：", i2),
        ("I3", "短路契约破了（成本/延迟特征变了）：", i3),
        ("I4", "B 组精确名称未排第一（trigram 退化成被 keyword 挤掉）：", i4),
        ("I5", "A 组前提失效 —— 这几条已不是自测组，报告里 A 组的读法要改：", i5),
        ("I6", "检索返回空：", i6),
        ("I7", "GT 为空清单变了（标注被改动？）：", i7),
        ("I8", "坏的是【测量仪器】，不是被测对象 —— 先排网络/服务：", i8),
    ]
    for code, head, viol in details:
        if not viol:
            continue
        print()
        print(f"  ✗ {code} 明细 · {head}")
        for qid, qtext, why in viol[:12]:
            print(f"      {qid}  {_pad(qtext[:18], 20)} {why}")
        if len(viol) > 12:
            print(f"      …另有 {len(viol) - 12} 条")

    for code, desc, ok, cur, msgs, _na, _nr in ratchets:
        if ok:
            continue
        print()
        print(f"  ✗ {code} 明细 · {desc}")
        for qid in sorted(cur)[:12]:
            print(f"      {qid}  {cur[qid]}")
        if len(cur) > 12:
            print(f"      …另有 {len(cur) - 12} 条")
        for m in msgs:
            print(f"      → {m}")

    # ── 通道探针（非生产行为，见 _probe_search）──
    #
    # 放在这里是因为探针 pass 已经跑过了（I8 要用它的通道失败信息），
    # 顺手打出这张表比让它白跑一趟有用。它回答【该修哪条通道】，
    # 是融合数字永远回答不了的问题。
    if probe_r:
        print()
        print("  ── 通道探针 · 各通道【单独】跑的 Recall@5（非生产行为）──")
        print("     线上是短路的，被短路的通道根本没跑；这里让它们各自单跑。")
        probe_by_group: dict[str, list[dict]] = {g: [] for g in GROUP_ORDER}
        for r in probe_r:
            probe_by_group[_group_of(r["query"])].append(r)
        print(f"      {'组':<5s}{'#Q':>4s}" + "".join(f"{ch:>10s}" for ch in CHANNELS)
              + f"{'融合':>10s}")
        for g in GROUP_ORDER:
            rs = probe_by_group[g]
            if not rs:
                continue
            print(f"      {g:<5s}{len(rs):>4d}", end="")
            for ch in CHANNELS:
                arr = [r["channel_metrics"][ch]["Recall@5"] for r in rs
                       if "Recall@5" in r["channel_metrics"][ch]]
                print(f"{statistics.mean(arr):>10.4f}" if arr else f"{'N/A':>10s}", end="")
            arr = [r["metrics"]["Recall@5"] for r in rs if "Recall@5" in r["metrics"]]
            print(f"{statistics.mean(arr):>10.4f}" if arr else f"{'N/A':>10s}")

    # ── 分组参考值（不是基线，只用来发现漂移）──
    print()
    print("  分组参考值（★ 不是基线 —— 解释见本文件 Phase 3 抬头）：")
    order = ("Recall@5", "Precision@5", "NDCG@5", "MRR")
    for g in GROUP_ORDER:
        if not by_group[g]:
            continue
        vals = _mean_metrics(by_group[g], order)
        line = "  ".join(f"{m}={vals[m]:.4f}" for m in order if m in vals)
        print(f"    {_pad(g, 3)}{len(by_group[g]):>2d} 条   {line}")

    failed = [c for c, _, ok, _ in checks if not ok]
    failed += [c for c, _, ok, _, _ in ratchets if not ok]
    print()
    if failed:
        print(f"  结论: FAIL（{'、'.join(failed)} 破掉）")
    else:
        print(f"  结论: PASS（{len(checks)} 条硬不变量 + {len(ratchets)} 条棘轮）")
    print()
    return 1 if failed else 0


# ═══════════════════════════════════════════════════════════════════════
# Phase 4: --ablate — 消融实验
# ═══════════════════════════════════════════════════════════════════════

# 消融以【生产的向量通道参数】为基准，每行只动一个开关 —— delta 才可解释为
# 「相对线上向量通道，动这一个开关会怎样」。
#
# 命名规则：`-xxx` = 把生产【开着】的关掉；`+xxx` = 把生产【关着】的打开。
# `+bucketing` 因此不是"消融"而是"候选改动" —— 它的 delta 直接回答
# 「要不要在生产开这个组件」。
ABLATION_CONFIGS = {
    "baseline (生产参数)":   dict(PRODUCTION_RETRIEVAL),
    "-threshold":           {**PRODUCTION_RETRIEVAL, "enable_threshold": False},
    "+bucketing":           {**PRODUCTION_RETRIEVAL, "enable_bucketing": True},
    "-mmr":                 {**PRODUCTION_RETRIEVAL, "enable_mmr": False},
    "vanilla (全关)":        {"enable_threshold": False, "enable_bucketing": False,
                             "enable_mmr": False},
}

BASELINE_NAME = "baseline (生产参数)"

# ⚠ 消融【必须】跑在探针 pass 上，不能跑在生产 pass 上。
#
# 生产融合是短路的（tools/bgm_tools.py:1091/1108）：keyword 凑满 limit 就不调
# vector。实测 25 条自动查询里 20 条被短路 —— 也就是说，在生产 pass 上做向量
# 消融，【20 条查询的向量通道根本没跑】，三个开关的效果恒为零，整张表几乎全平。
#
# 那不是"这些组件没用"，是"这些组件没跑"。探针 pass 是唯一保证三条通道都跑的
# pass，也就是唯一能让开关可测的 pass。
#
# 代价：这里的 baseline 行【不等于】线上表现 —— 它是"向量通道单独跑"的表现。
# 想知道线上表现跑 --evaluate，不要读这张表。


def cmd_ablate():
    """--ablate: 对每个消融配置跑【探针】eval，输出对比表。"""
    _silence_sqlalchemy()

    # ── 加载 GT ──
    if not AUTO_GT_FILE.exists():
        print(f"错误: {AUTO_GT_FILE.name} 不存在，请先运行 --build")
        return

    with open(AUTO_GT_FILE, encoding="utf-8") as f:
        auto_data = json.load(f)
    auto_queries = auto_data.get("queries", [])
    human_queries = _load_annotated_pooled()
    all_queries = auto_queries + human_queries
    usable_queries, dropped = _split_by_gt(all_queries)

    print(f"消融实验: {len(usable_queries)} 题 × {len(ABLATION_CONFIGS)} 配置"
          + (f"（另有 {len(dropped)} 条 GT 为空，未参与）" if dropped else ""))
    print("  ⚠ 跑在【探针】pass 上：三通道各自单跑。生产是短路的，被短路的通道")
    print("     在生产 pass 上根本没跑，在那里做消融表会几乎全平。")
    print("     所以本表的 baseline 行 =「向量通道单独跑」，【不是】线上表现。")
    print()

    # ── 对每个配置跑完整 eval ──
    config_results: dict[str, dict] = {}

    for config_name, kwargs in ABLATION_CONFIGS.items():
        print(f"  [{config_name}]", end=" ", flush=True)
        all_metrics: dict[str, list[float]] = {}
        # 分组聚合：主表把 A 组构造出来的 1.0 混进来，会稀释掉真实差异。
        group_metrics: dict[str, dict[str, list[float]]] = {g: {} for g in GROUP_ORDER}

        for i, q in enumerate(usable_queries):
            gt = set(q.get("ground_truth", []))
            grp = _group_of(q)

            retrieved = _probe_search(
                q["query"], q.get("entity_type", "all"),
                q.get("subject_type"),
                keyword_filters=q.get("keyword_filters"),
                **kwargs,
            )
            metrics = _compute_metrics(retrieved["merged"], gt)
            for key, val in metrics.items():
                all_metrics.setdefault(key, []).append(val)
                group_metrics[grp].setdefault(key, []).append(val)
            time.sleep(0.02)

        # 聚合
        agg = {}
        for key, vals in all_metrics.items():
            agg[key] = statistics.mean(vals) if vals else 0.0

        config_results[config_name] = {
            "metrics": agg,
            "group_metrics": {
                g: {k: statistics.mean(v) for k, v in gm.items() if v}
                for g, gm in group_metrics.items() if gm
            },
            "valid_queries": len(all_metrics.get("Recall@5", [])),
        }
        print(f"{config_results[config_name]['valid_queries']} 题, "
              f"Rec@5={agg.get('Recall@5', 0):.4f}, "
              f"Prec@5={agg.get('Precision@5', 0):.4f}, "
              f"MRR={agg.get('MRR', 0):.4f}")

    # ── 对比表 ──
    baseline = config_results[BASELINE_NAME]["metrics"]
    key_metrics = ["Recall@5", "Precision@5", "MRR", "NDCG@5", "Hit@5"]

    print()
    print("=" * 78)
    print(f"  消融对比 — 相对【{BASELINE_NAME}】的变化")
    print("=" * 78)
    print("  基准 = 线上的向量通道参数（tools/bgm_tools.py 不传消融开关）。")
    print("  ⚠ 口径是【探针】：向量通道单独跑。不是线上表现 —— 见本函数抬头。")
    # 单元格宽 13 = "0.4172 +0.0000"；baseline 行不显示 delta，用 >13 补齐以对齐
    header = "  " + _pad("配置", 26)
    for m in key_metrics:
        header += f"  {m:>13s}"
    print(header)
    print(f"  {'─' * 82}")

    for config_name in ABLATION_CONFIGS:
        agg = config_results[config_name]["metrics"]
        row = "  " + _pad(config_name, 26)
        for m in key_metrics:
            val = agg.get(m, 0)
            if config_name == BASELINE_NAME:
                row += f"  {val:>13.4f}"
            else:
                delta = val - baseline.get(m, 0)
                sign = "+" if delta >= 0 else ""
                row += f"  {val:.4f} {sign}{delta:.4f}"
        print(row)

    print()
    print("  解读: 正 delta = 这一改动【比线上好】，负 = 比线上差。")
    print("        `-xxx` 行看你关掉线上某组件会损失多少；")
    print("        `+bucketing` 行是【候选改动】—— 正数就直接说明该在生产开它。")
    print()

    # ── 分组对比 ──
    # 上面那张表把 A 组 18 条构造出来的 1.0 平均了进去，配置差异被稀释。
    # 分组看才知道某个组件到底帮的是【哪一类】查询。
    print("=" * 78)
    print("  分组 Recall@5（消除 A 组稀释后的真实差异）")
    print("=" * 78)
    labels = {"A": "A 自测", "A'": "A′ 缺口", "B": "B 质量", "C": "C 人工"}
    print("  " + _pad("配置", 26)
          + "".join(f"{labels[g]:>12s}" for g in GROUP_ORDER))
    print(f"  {'─' * 74}")

    for config_name in ABLATION_CONFIGS:
        gm = config_results[config_name]["group_metrics"]
        row = "  " + _pad(config_name, 26)
        for g in GROUP_ORDER:
            val = gm.get(g, {}).get("Recall@5")
            row += f"{val:>12.4f}" if val is not None else f"{'N/A':>12s}"
        print(row)

    print()
    print("  读法: A 组的数【不反映质量】—— 它就是配置的函数（GT 与检索同源）。")
    print("        若某配置把 A 组压低了 → keyword 通道被关掉或坏了，那是【回归】。")
    print("        B / A′ / C 组才是组件收益所在。")
    print()


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="RAG 检索评测管线 v2")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true",
                       help="生成自动 Ground Truth + 池化人工标注模板")
    group.add_argument("--evaluate", action="store_true",
                       help="检索 + 计算全部指标")
    group.add_argument("--check", action="store_true",
                       help="回归不变量断言（轴 2 的基线）：PASS/FAIL + 退出码")
    group.add_argument("--ablate", action="store_true",
                       help="消融实验：对比 threshold/bucketing/MMR 各组件的贡献")
    parser.add_argument("--freeze", action="store_true",
                        help="与 --check 同用：收紧棘轮基线（只能删条目，不能加）")
    parser.add_argument("--reason", default="",
                        help="--freeze 必填：为什么收紧，写进基线的 why 字段")
    parser.add_argument("--no-probe", action="store_true",
                        help="与 --check 同用：跳过探针 pass（省 ~35 次 embedding 调用）")
    args = parser.parse_args()

    if args.freeze and not args.check:
        parser.error("--freeze 只与 --check 同用")
    if args.no_probe and not args.check:
        parser.error("--no-probe 只与 --check 同用")
    if args.freeze and not args.reason:
        parser.error("--freeze 必须带 --reason")

    if args.build:
        cmd_build()
    elif args.evaluate:
        cmd_evaluate()
    elif args.check:
        sys.exit(cmd_check(freeze=args.freeze, probe=not args.no_probe,
                           reason=args.reason))
    elif args.ablate:
        cmd_ablate()


if __name__ == "__main__":
    main()
