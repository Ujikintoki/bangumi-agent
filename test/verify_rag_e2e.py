"""
RAG 管道端到端 (E2E) 验证脚本

严格按以下流程串联 ingestion → retrieval：
  1. Mock Data 准备 — 2-3 条极简 Bangumi 条目数据（含 tags 等元数据）
  2. 连接池初始化 — 连接本地 Docker PostgreSQL + PGVector
  3. Ingestion — 调用智谱 embedding-3 将 Mock 数据向量化并入库
  4. Retrieval — 混合检索（向量召回 + JSONB 硬过滤）
  5. 结果断言与输出 — logger 打印 Top-K 结果
  6. Teardown — try...finally 清理 Mock 实体，保证数据库不被污染

用法:
    cd /Users/lichenhao/python/bgm-agent-dev
    python test/verify_rag_e2e.py
"""

from __future__ import annotations

import logging
import os
import sys
import time

# 确保能导入项目根目录的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, delete

from core.config import get_settings
from database.engine import engine, init_db
from database.models import BangumiChunk
from rag.ingestion import BangumiIngestor
from rag.retriever import BangumiRetriever

# ── Logger 配置 ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("verify_rag_e2e")

settings = get_settings()

# ═══════════════════════════════════════════════════════════════
# Step 1: Mock Data 准备
# ═══════════════════════════════════════════════════════════════

# 使用极高的 subject_id 避免与真实数据冲突
MOCK_SUBJECT_IDS = [999001, 999002, 999003]

MOCK_CHUNKS_DATA: list[dict] = [
    {
        "chunk_id": 1,
        "subject_id": 999001,
        "name": "やがて君になる",
        "type": 2,  # 2 = 动画
        "score": 7.8,
        "tags": ["百合", "校园", "恋爱", "漫画改"],
        "text": (
            "小糸侑是一名高中一年级学生，她一直无法理解'恋爱'这种感情。"
            "直到她遇到了学生会前辈七海灯子，灯子对任何人都温柔以待，"
            "却唯独对侑展现出了特别的感情。两人在学生会室中度过了"
            "许多时光，侑逐渐发现自己心中萌芽的感情。这是一个关于"
            "两位少女之间细腻情感的青春物语。"
        ),
    },
    {
        "chunk_id": 2,
        "subject_id": 999002,
        "name": "魔法少女まどか☆マギカ",
        "type": 2,
        "score": 8.5,
        "tags": ["原创", "魔法少女", "黑暗", "致郁", "虚渊玄"],
        "text": (
            "鹿目圆是一名普通的中学二年级学生，过着平凡的生活。"
            "一天，她遇到了一只名为丘比的神秘生物，它提出可以"
            "实现任何一个愿望，作为交换，圆将成为魔法少女与魔女战斗。"
            "然而，魔法少女的命运远比想象中残酷，隐藏在契约背后的"
            "真相将彻底颠覆圆和她朋友们的世界观。"
        ),
    },
    {
        "chunk_id": 3,
        "subject_id": 999003,
        "name": "ゆるキャン△",
        "type": 2,
        "score": 8.2,
        "tags": ["日常", "治愈", "露营", "漫画改"],
        "text": (
            "志摩凛是一名喜爱独自露营的女高中生。某个冬日，她在"
            "本栖湖畔遇到了迷路的各务原抚子，两人一起吃了杯面，"
            "从此抚子对露营产生了浓厚的兴趣。故事围绕着野外活动"
            "同好会的成员们展开，描绘了她们在富士山脚下享受露营、"
            "欣赏美景的悠闲日常。"
        ),
    },
]


def main() -> None:
    """RAG E2E 验证主函数。"""
    logger.info("=" * 60)
    logger.info("RAG 管道 E2E 验证开始")
    logger.info("=" * 60)

    # ═══════════════════════════════════════════════════════════
    # Step 2: 连接池初始化
    # ═══════════════════════════════════════════════════════════
    logger.info("[Step 2] 初始化数据库连接...")
    logger.info("  DATABASE_URL: %s", settings.DATABASE_URL)

    try:
        init_db()
        logger.info("  ✅ 数据库表结构确认完成 (pgvector 扩展已启用)")
    except Exception as exc:
        logger.error("  ❌ 数据库初始化失败: %s", exc)
        logger.error(
            "  请确保 Docker PostgreSQL 已启动: docker run -d --name bgm-pg "
            "-e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword "
            "-e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16"
        )
        sys.exit(1)

    # ═══════════════════════════════════════════════════════════
    # Step 3: Ingestion — 向量化并入库
    # ═══════════════════════════════════════════════════════════
    logger.info("[Step 3] 开始数据摄入 (Ingestion)...")
    logger.info("  Mock 数据条目数: %d", len(MOCK_CHUNKS_DATA))
    logger.info(
        "  ZHIPU_API_KEY: %s...",
        settings.ZHIPU_API_KEY[:8] if settings.ZHIPU_API_KEY else "(未配置)",
    )

    if not settings.ZHIPU_API_KEY:
        logger.error(
            "  ❌ ZHIPU_API_KEY 未配置！请在 .env 文件中设置 ZHIPU_API_KEY=你的密钥"
        )
        sys.exit(1)

    ingestor = BangumiIngestor(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
        zhipu_base_url=settings.ZHIPU_BASE_URL,
    )

    t0 = time.perf_counter()
    try:
        inserted = ingestor.ingest_chunks(MOCK_CHUNKS_DATA)
        elapsed = time.perf_counter() - t0
        logger.info(
            "  ✅ Ingestion 完成: 成功写入 %d 条 chunk, 耗时 %.2f 秒",
            inserted,
            elapsed,
        )
    except Exception as exc:
        logger.error("  ❌ Ingestion 失败: %s", exc)
        sys.exit(1)

    # ═══════════════════════════════════════════════════════════
    # Step 4: Retrieval — 混合检索
    # ═══════════════════════════════════════════════════════════
    logger.info("[Step 4] 开始混合检索 (Hybrid Search)...")

    retriever = BangumiRetriever(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
    )

    # ── 测试 A: 纯向量检索（无过滤） ──────────────────────────
    query_a = "百合恋爱的校园动画"
    logger.info("  --- 测试 A: 纯语义检索 ---")
    logger.info("  Query: '%s'", query_a)

    t1 = time.perf_counter()
    results_a = retriever.hybrid_search(
        query=query_a,
        top_k=3,
    )
    logger.info("  耗时: %.2f 秒, 命中 %d 条", time.perf_counter() - t1, len(results_a))
    _print_results("测试 A (纯语义)", results_a)

    # ── 测试 B: JSONB 硬过滤 — 仅限带有"百合"标签的条目 ──────
    query_b = "少女之间的情感故事"
    required_tags_b = ["百合"]
    logger.info("  --- 测试 B: Tags 硬过滤 ---")
    logger.info("  Query: '%s', required_tags: %s", query_b, required_tags_b)

    t2 = time.perf_counter()
    results_b = retriever.hybrid_search(
        query=query_b,
        required_tags=required_tags_b,
        top_k=3,
    )
    logger.info("  耗时: %.2f 秒, 命中 %d 条", time.perf_counter() - t2, len(results_b))
    _print_results("测试 B (Tags 过滤: 百合)", results_b)

    # ── 测试 C: Tags + Score 双重硬过滤 ───────────────────────
    query_c = "治愈系日常动画"
    required_tags_c = ["日常"]
    min_score_c = 8.0
    logger.info("  --- 测试 C: Tags + Score 双重硬过滤 ---")
    logger.info(
        "  Query: '%s', required_tags: %s, min_score: %s",
        query_c,
        required_tags_c,
        min_score_c,
    )

    t3 = time.perf_counter()
    results_c = retriever.hybrid_search(
        query=query_c,
        required_tags=required_tags_c,
        min_score=min_score_c,
        top_k=3,
    )
    logger.info("  耗时: %.2f 秒, 命中 %d 条", time.perf_counter() - t3, len(results_c))
    _print_results("测试 C (Tags + Score 双重过滤)", results_c)

    # ── 测试 D: 不存在的标签组合（预期空结果） ─────────────────
    query_d = "任何动画"
    required_tags_d = ["机战", "百合"]
    logger.info("  --- 测试 D: 不可能匹配的标签组合 ---")
    logger.info("  Query: '%s', required_tags: %s", query_d, required_tags_d)

    t4 = time.perf_counter()
    results_d = retriever.hybrid_search(
        query=query_d,
        required_tags=required_tags_d,
        top_k=3,
    )
    logger.info(
        "  耗时: %.2f 秒, 命中 %d 条 (预期 0)", time.perf_counter() - t4, len(results_d)
    )

    # ═══════════════════════════════════════════════════════════
    # Step 5: 结果断言 (软断言，打印概要)
    # ═══════════════════════════════════════════════════════════
    logger.info("[Step 5] 结果概要:")
    logger.info("  测试 A (纯语义)    : %d 条结果", len(results_a))
    logger.info(
        "  测试 B (Tags 过滤) : %d 条结果 — 应仅有 '百合' 标签条目", len(results_b)
    )
    logger.info(
        "  测试 C (双重过滤)  : %d 条结果 — 应有 '日常' 标签且 score ≥ 8.0",
        len(results_c),
    )
    logger.info("  测试 D (不可能组合) : %d 条结果 — 预期为 0", len(results_d))

    # 软断言
    all_tags_b = {tag for r in results_b for tag in r.tags}
    assert all("百合" in r.tags for r in results_b), (
        "❌ 测试 B 失败：结果中存在不含'百合'标签的条目！"
    )
    assert all(r.score >= 8.0 for r in results_c), (
        "❌ 测试 C 失败：结果中存在评分 < 8.0 的条目！"
    )
    assert all("日常" in r.tags for r in results_c), (
        "❌ 测试 C 失败：结果中存在不含'日常'标签的条目！"
    )
    assert len(results_d) == 0, (
        f"❌ 测试 D 失败：预期 0 条结果，实际 {len(results_d)} 条"
    )

    logger.info("  ✅ 所有软断言通过！")


def _print_results(label: str, results: list) -> None:
    """格式化打印检索结果。

    Args:
        label: 测试标签，如 "测试 A (纯语义)"。
        results: SearchResult 列表。
    """
    if not results:
        logger.info("    (%s) 无结果", label)
        return

    for i, r in enumerate(results, 1):
        logger.info(
            "    [%s #%d] %s | distance=%.4f | score=%.1f | tags=%s",
            label,
            i,
            r.name,
            r.cosine_distance,
            r.score,
            r.tags,
        )


# ═══════════════════════════════════════════════════════════════
# 入口 + Step 6: Teardown（try...finally 清理）
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except Exception as exc:
        logger.error("E2E 测试异常退出: %s", exc, exc_info=True)
        exit_code = 1
    finally:
        # ── Teardown: 清理 Mock 实体 ───────────────────────────
        logger.info("-" * 60)
        logger.info("[Teardown] 清理 Mock 数据 (entity_ids=%s)...", MOCK_SUBJECT_IDS)
        try:
            with Session(engine) as session:
                stmt = delete(BangumiChunk).where(
                    BangumiChunk.entity_id.in_(MOCK_SUBJECT_IDS)
                )
                result = session.exec(stmt)  # type: ignore[arg-type]
                session.commit()
                logger.info(
                    "  ✅ 已清理 %d 条 Mock 数据，数据库保持干净",
                    result.rowcount,  # type: ignore[union-attr]
                )
        except Exception as clean_exc:
            logger.error("  ⚠️ 清理失败（数据库可能残留 Mock 数据）: %s", clean_exc)

        logger.info("=" * 60)
        logger.info("RAG E2E 验证结束 (exit_code=%d)", exit_code)
        logger.info("=" * 60)

    sys.exit(exit_code)
