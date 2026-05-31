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
MOCK_SUBJECT_IDS = [999001, 999002, 999003, 999004, 999005]

MOCK_CHUNKS_DATA: list[dict] = [
    {
        "chunk_id": 1,
        "subject_id": 999001,
        "name": "やがて君になる",
        "type": 2,  # 2 = 动画
        "score": 7.8,
        "rating_total": 3200,
        "nsfw": False,
        "core_staff": ["加藤誠 (监督)", "花田十輝 (系列构成)"],
        "main_cv": ["寿美菜子 (小糸侑)", "高田憂希 (七海灯子)"],
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
        "rating_total": 15800,
        "nsfw": False,
        "core_staff": ["新房昭之 (监督)", "虚渊玄 (剧本)", "蒼樹うめ (人设原案)"],
        "main_cv": ["悠木碧 (鹿目圆)", "斎藤千和 (暁美焰)", "水橋かおり (巴麻美)"],
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
        "rating_total": 8900,
        "nsfw": False,
        "core_staff": ["京極義昭 (监督)", "田中仁 (系列构成)"],
        "main_cv": ["花守ゆみり (志摩凛)", "東山奈央 (各务原抚子)"],
        "tags": ["日常", "治愈", "露营", "漫画改"],
        "text": (
            "志摩凛是一名喜爱独自露营的女高中生。某个冬日，她在"
            "本栖湖畔遇到了迷路的各务原抚子，两人一起吃了杯面，"
            "从此抚子对露营产生了浓厚的兴趣。故事围绕着野外活动"
            "同好会的成员们展开，描绘了她们在富士山脚下享受露营、"
            "欣赏美景的悠闲日常。"
        ),
    },
    # ── 新增 #4: 冷门 R18 动画（测试安全护栏） ───────────
    {
        "chunk_id": 4,
        "subject_id": 999004,
        "name": "淫らな魔法少女と変態の塔",
        "type": 2,
        "score": 6.1,
        "rating_total": 50,
        "nsfw": True,
        "core_staff": ["匿名 (监督)"],
        "main_cv": ["匿名声優A (主人公)", "匿名声優B (ヒロイン)"],
        "tags": ["R18", "魔法少女", "短篇"],
        "text": (
            "平凡な少年が異世界に召喚され、淫らな魔法少女たちと共に"
            "変態の塔を攻略する物語。過激な描写が多く含まれる成人向け"
            "アニメ作品であり、魔法少女というジャンルを大胆に再解釈している。"
        ),
    },
    # ── 新增 #5: 极度冷门但合法的动画（测试长尾召回） ────
    {
        "chunk_id": 5,
        "subject_id": 999005,
        "name": "蛍火の杜へ",
        "type": 2,
        "score": 8.0,
        "rating_total": 10,
        "nsfw": False,
        "core_staff": ["大森貴弘 (监督)"],
        "main_cv": ["佐倉綾音 (竹川蛍)", "内山昂輝 (ギン)"],
        "tags": ["治愈", "短篇", "妖怪", "恋爱", "催泪"],
        "text": (
            "竹川蛍在幼年时曾迷失在一片住着妖怪的森林中，"
            "一个戴着狐狸面具的神秘少年银救了她。银一旦被人类触碰就会消失，"
            "两人约定每年夏天在这片森林中相见。随着蛍渐渐长大，"
            "两人之间的感情也越发深厚，但命运的悲剧也在悄然逼近。"
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

    # ── 测试 A: 纯语义检索（无过滤，验证降级排序） ──────────
    query_a = "百合恋爱的校园动画"
    logger.info("  --- 测试 A: 纯语义检索 (降级排序) ---")
    logger.info("  Query: '%s'", query_a)

    t1 = time.perf_counter()
    results_a = retriever.hybrid_search(
        query=query_a,
        exclude_nsfw=True,
        top_k=3,
    )
    logger.info("  耗时: %.2f 秒, 命中 %d 条", time.perf_counter() - t1, len(results_a))
    _print_results("测试 A (降级排序)", results_a)

    # ── 测试 B: Tags JSONB 硬过滤 — 仅限带"百合"标签的条目 ────
    query_b = "少女之间的情感故事"
    required_tags_b = ["百合"]
    logger.info("  --- 测试 B: Tags JSONB 硬过滤 ---")
    logger.info("  Query: '%s', required_tags: %s", query_b, required_tags_b)

    t2 = time.perf_counter()
    results_b = retriever.hybrid_search(
        query=query_b,
        required_tags=required_tags_b,
        top_k=3,
    )
    logger.info("  耗时: %.2f 秒, 命中 %d 条", time.perf_counter() - t2, len(results_b))
    _print_results("测试 B (Tags 过滤: 百合)", results_b)

    # ── 测试 C: 日常治愈类检索 ──────────────────────────────────
    query_c = "治愈系日常动画 露营"
    logger.info("  --- 测试 C: 日常治愈语义检索 ---")
    logger.info("  Query: '%s'", query_c)

    t3 = time.perf_counter()
    results_c = retriever.hybrid_search(
        query=query_c,
        exclude_nsfw=True,
        top_k=3,
    )
    logger.info("  耗时: %.2f 秒, 命中 %d 条", time.perf_counter() - t3, len(results_c))
    _print_results("测试 C (日常治愈)", results_c)

    # ── 测试 D: 意图外闲聊阻断测试（距离阈值防爆） ────────────
    query_d = "100个五条悟vs100个哥斯拉，谁能赢？"
    logger.info("  --- 测试 D: 距离阈值防爆 (意图外闲聊) ---")
    logger.info("  Query: '%s', distance_threshold=0.65 (默认)", query_d)

    t4 = time.perf_counter()
    results_d = retriever.hybrid_search(
        query=query_d,
        top_k=3,
    )
    logger.info(
        "  耗时: %.2f 秒, 命中 %d 条 (预期 0 — 闲聊问题应被阈值拦截)",
        time.perf_counter() - t4,
        len(results_d),
    )
    _print_results("测试 D (闲聊阻断)", results_d)

    # ── 测试 E: 冷门番剧召回 — 验证不误杀长尾内容 ────────────
    query_e = "妖怪森林的夏日恋爱故事"
    logger.info("  --- 测试 E: 冷门番剧长尾召回 ---")
    logger.info(
        "  Query: '%s' — 预期召回《蛍火の杜へ》(rating_total=10, 极度冷门)",
        query_e,
    )

    t5 = time.perf_counter()
    results_e = retriever.hybrid_search(
        query=query_e,
        exclude_nsfw=True,
        top_k=3,
    )
    logger.info("  耗时: %.2f 秒, 命中 %d 条", time.perf_counter() - t5, len(results_e))
    _print_results("测试 E (冷门召回)", results_e)

    # ── 测试 F: 安全护栏 — exclude_nsfw=True 拦截 R18 ──────────
    query_f = "魔法少女"
    logger.info("  --- 测试 F: 安全护栏 (exclude_nsfw=True) ---")
    logger.info(
        "  Query: '%s', exclude_nsfw=True — 预期拦截 R18《淫らな魔法少女》",
        query_f,
    )

    t6 = time.perf_counter()
    results_f = retriever.hybrid_search(
        query=query_f,
        exclude_nsfw=True,
        top_k=5,
    )
    logger.info("  耗时: %.2f 秒, 命中 %d 条", time.perf_counter() - t6, len(results_f))
    _print_results("测试 F (安全护栏 ON)", results_f)

    # ═══════════════════════════════════════════════════════════
    # Step 5: 结果断言 (软断言，打印概要)
    # ═══════════════════════════════════════════════════════════
    logger.info("[Step 5] 结果概要:")
    logger.info("  测试 A (降级排序)    : %d 条结果", len(results_a))
    logger.info(
        "  测试 B (Tags 过滤)  : %d 条结果 — 应仅有 '百合' 标签条目", len(results_b)
    )
    logger.info("  测试 C (日常治愈)    : %d 条结果", len(results_c))
    logger.info("  测试 D (闲聊阻断)    : %d 条结果 — 预期为 0", len(results_d))
    logger.info(
        "  测试 E (冷门召回)    : %d 条结果 — 需含《蛍火の杜へ》", len(results_e)
    )
    logger.info("  测试 F (安全护栏 ON) : %d 条结果 — 不得含 R18 条目", len(results_f))

    # ── 软断言 ──────────────────────────────────────────────────
    assert len(results_a) > 0, "❌ 测试 A 失败：纯语义检索无结果"

    # 测试 B: Tags 硬过滤 — 所有结果必须包含"百合"标签
    assert len(results_b) > 0, "❌ 测试 B 失败：Tags 过滤无结果"
    assert all("百合" in r.tags for r in results_b), (
        "❌ 测试 B 失败：结果中存在不含'百合'标签的条目！"
    )

    assert len(results_d) == 0, (
        f"❌ 测试 D 失败：闲聊问题应被距离阈值拦截，实际返回 {len(results_d)} 条"
    )

    # 测试 E: 冷门番剧必须被召回
    cold_names = [r.name for r in results_e]
    assert any("蛍火" in name for name in cold_names), (
        f"❌ 测试 E 失败：冷门番剧《蛍火の杜へ》(rating_total=10) 未被召回！"
        f" 实际结果: {cold_names}"
    )

    # 测试 F: 安全护栏 — 不得返回任何 nsfw=True 的条目
    nsfw_in_results = [r for r in results_f if r.nsfw]
    assert len(nsfw_in_results) == 0, (
        f"❌ 测试 F 失败：安全护栏失效！"
        f" exclude_nsfw=True 时仍返回了 {len(nsfw_in_results)} 条 R18 条目: "
        f"{[r.name for r in nsfw_in_results]}"
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
            "    [%s #%d] %s | distance=%.4f | final=%.4f | "
            "score=%.1f | heat=%d | nsfw=%s | tags=%s",
            label,
            i,
            r.name,
            r.cosine_distance,
            r.final_score,
            r.score,
            r.rating_total,
            r.nsfw,
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
