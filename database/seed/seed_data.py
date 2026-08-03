"""
种子数据注入脚本

通过 enricher → ingestion 管线，一键灌入 subject / character / person 初始数据。

用法::

    python database/cli.py seed          # CLI 入口
    python -m database.seed.seed_data    # 直接运行
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("database.seed")

# ═══════════════════════════════════════════════════════════════════
# 经典 Subject 种子数据（~100 条，覆盖不同年代/类型/热度）
# 来源: scripts/ingest_test_data.py
# ═══════════════════════════════════════════════════════════════════

_SUBJECT_SEEDS: list[dict] = [
    # ── 经典高分 ──
    {
        "subject_id": 1, "name": "新世紀エヴァンゲリオン", "name_cn": "新世纪福音战士",
        "score": 9.0, "rank": 20, "rating_total": 18500, "date": "1995-10-04",
        "year": 1995, "platform": "TV", "eps": 26,
        "tags": [{"name": "科幻", "count": 3200}, {"name": "心理", "count": 2800},
                 {"name": "机甲", "count": 2500}, {"name": "原创", "count": 2100}],
        "nsfw": False,
        "chunk_text": "14岁的少年碇真嗣被父亲召唤到NERV组织驾驶巨大的人形兵器EVA与使徒战斗。作品深入探讨了孤独、抑郁、人际关系等深刻主题，被誉为日本动画史上的里程碑之作。",
    },
    {
        "subject_id": 2, "name": "鋼の錬金術師 FULLMETAL ALCHEMIST", "name_cn": "钢之炼金术师FA",
        "score": 9.1, "rank": 2, "rating_total": 32000, "date": "2009-04-05",
        "year": 2009, "platform": "TV", "eps": 64,
        "tags": [{"name": "冒险", "count": 4500}, {"name": "奇幻", "count": 3800},
                 {"name": "少年漫改", "count": 3500}, {"name": "热血", "count": 3200}],
        "nsfw": False,
        "chunk_text": "爱德华和阿尔冯斯兄弟为了复活母亲进行禁忌的人体炼成，踏上寻找贤者之石的旅程，揭开了军部背后的巨大阴谋。",
    },
    {
        "subject_id": 265, "name": "新世紀エヴァンゲリオン", "name_cn": "新世纪福音战士",
        "score": 9.0, "rank": 20, "rating_total": 18500, "date": "1995-10-04",
        "year": 1995, "platform": "TV", "eps": 26,
        "tags": [{"name": "科幻", "count": 3200}, {"name": "心理", "count": 2800}],
        "nsfw": False,
        "chunk_text": "经典的EVA TV版。14岁的少年碇真嗣被父亲召唤到NERV组织。",
    },
    {
        "subject_id": 876, "name": "STEINS;GATE", "name_cn": "命运石之门",
        "score": 8.9, "rank": 25, "rating_total": 25000, "date": "2011-04-06",
        "year": 2011, "platform": "TV", "eps": 24,
        "tags": [{"name": "科幻", "count": 3800}, {"name": "时间旅行", "count": 3200},
                 {"name": "游戏改", "count": 2800}],
        "nsfw": False,
        "chunk_text": "自称疯狂科学家的凤凰院凶真在秋叶原创立了未来道具研究所，发明了能向过去发送邮件的时间机器D-Mail。前期慢热后期封神的典范。",
    },
    {
        "subject_id": 971, "name": "CLANNAD -クラナド-", "name_cn": "CLANNAD",
        "score": 8.7, "rank": 45, "rating_total": 22000, "date": "2007-10-04",
        "year": 2007, "platform": "TV", "eps": 23,
        "tags": [{"name": "催泪", "count": 5200}, {"name": "校园", "count": 3500},
                 {"name": "治愈", "count": 2500}],
        "nsfw": False,
        "chunk_text": "不良少年冈崎朋也在学校坡道上邂逅了体弱多病的古河渚，帮助她重建演剧部。After Story将视角延伸到毕业后的人生，被誉为催泪神作。",
    },
    {
        "subject_id": 1887, "name": "魔法少女まどか☆マギカ", "name_cn": "魔法少女小圆",
        "score": 8.4, "rank": 250, "rating_total": 20000, "date": "2011-01-06",
        "year": 2011, "platform": "TV", "eps": 12,
        "tags": [{"name": "魔法少女", "count": 3500}, {"name": "致郁", "count": 3200},
                 {"name": "原创", "count": 2800}],
        "nsfw": False,
        "chunk_text": "平凡的中学生鹿目圆遇到神秘生物丘比。魔法少女系统的残酷真相逐渐浮出水面，第3话开始的惊人转折颠覆了整个魔法少女题材。",
    },
    {
        "subject_id": 253, "name": "進撃の巨人", "name_cn": "进击的巨人",
        "score": 8.0, "rank": 500, "rating_total": 45000, "date": "2013-04-06",
        "year": 2013, "platform": "TV", "eps": 25,
        "tags": [{"name": "热血", "count": 5500}, {"name": "战斗", "count": 5000},
                 {"name": "末世", "count": 4200}],
        "nsfw": False,
        "chunk_text": "人类被巨人逼到建立了三道巨大的城墙来保护自己。艾伦·耶格尔目睹母亲被巨人吞食后，立志消灭所有巨人，逐渐展开为涉及历史真相和自由意志的宏大史诗。",
    },
    {
        "subject_id": 1424, "name": "攻殻機動隊 STAND ALONE COMPLEX", "name_cn": "攻壳机动队 SAC",
        "score": 8.9, "rank": 27, "rating_total": 12000, "date": "2002-10-01",
        "year": 2002, "platform": "TV", "eps": 26,
        "tags": [{"name": "科幻", "count": 3000}, {"name": "赛博朋克", "count": 2800}],
        "nsfw": False,
        "chunk_text": "在近未来的日本，人类身体可以完全义体化。公安九课的草薙素子少佐率领团队调查笑面男事件。赛博朋克题材的巅峰之作。",
    },
    {
        "subject_id": 258, "name": "カウボーイビバップ", "name_cn": "星际牛仔",
        "score": 9.0, "rank": 15, "rating_total": 15000, "date": "1998-04-03",
        "year": 1998, "platform": "TV", "eps": 26,
        "tags": [{"name": "科幻", "count": 2800}, {"name": "太空", "count": 2500},
                 {"name": "爵士", "count": 2000}],
        "nsfw": False,
        "chunk_text": "2071年，赏金猎人Spike和搭档Jet驾驶飞船Bebop号在星际间追捕逃犯，配合菅野洋子的爵士配乐，营造出独特的硬汉浪漫。See you space cowboy.",
    },
    {
        "subject_id": 10, "name": "蟲師", "name_cn": "虫师",
        "score": 8.9, "rank": 25, "rating_total": 10000, "date": "2005-10-22",
        "year": 2005, "platform": "TV", "eps": 26,
        "tags": [{"name": "治愈", "count": 3500}, {"name": "奇幻", "count": 3000},
                 {"name": "单元剧", "count": 2800}],
        "nsfw": False,
        "chunk_text": "虫师银古四处旅行，解决由虫引发的各种奇异现象。每个故事都如同一个寓言，在宁静悠远的山水间缓缓展开，配以增田俊郎的东方韵味音乐。",
    },
]

# ═══════════════════════════════════════════════════════════════════
# 经典 Character / Person ID 列表（通过 enricher 从 API 获取）
# ═══════════════════════════════════════════════════════════════════

_CHARACTER_SEED_IDS: list[int] = [
    # 经典角色
    1,     # 鲁路修
    4,     # 古河渚
    47,    # 阿虚
    48,    # 凉宫春日
    77,    # 史派克
    302,   # 碇真嗣
    303,   # 绫波零
    304,   # 明日香
    1063,  # 冈崎汐
    42379, # 碇唯
]

_PERSON_SEED_IDS: list[int] = [
    # 知名声优/创作者
    94,    # 庵野秀明
    100,   # 渡边信一郎
    692,   # 新房昭之
    1287,  # 押井守
    1313,  # 今敏
    2064,  # 新海诚
    2280,  # 汤浅政明
    4765,  # 花泽香菜
    4513,  # 杉田智和
    5076,  # 悠木碧
]


# ═══════════════════════════════════════════════════════════════════


async def run_seed() -> None:
    """执行种子数据注入。"""
    from clients.client import BangumiClient
    from core.config import get_settings
    from database.engine import engine
    from rag.ingestion import RagEntityIngestor
    from rag.enricher import CharacterEnricher, PersonEnricher

    settings = get_settings()
    client = BangumiClient(access_token=settings.BANGUMI_ACCESS_TOKEN or None)

    ingestor = RagEntityIngestor(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
        zhipu_base_url=settings.ZHIPU_BASE_URL,
    )

    # ── Subject ──────────────────────────────────────────────
    logger.info("── 注入 Subject 种子 (%d 条) ──", len(_SUBJECT_SEEDS))
    try:
        n = ingestor.ingest_subjects(_SUBJECT_SEEDS)
        logger.info("Subject: %d 条写入", n)
    except Exception as e:
        logger.warning("Subject 种子注入失败（可能主键冲突，跳过）: %s", e)

    # ── Character ────────────────────────────────────────────
    logger.info("── 注入 Character 种子 (%d 个 ID) ──", len(_CHARACTER_SEED_IDS))
    try:
        ce = CharacterEnricher(client)
        chars = await ce.enrich_batch(_CHARACTER_SEED_IDS)
        valid = [c for c in chars if "_error" not in c]
        if valid:
            n = ingestor.ingest_characters(valid)
            logger.info("Character: %d/%d 条写入", n, len(valid))
        failed = len(chars) - len(valid)
        if failed:
            logger.warning("  %d 条富化失败，跳过", failed)
    except Exception as e:
        logger.warning("Character 种子注入失败: %s", e)

    # ── Person ──────────────────────────────────────────────
    logger.info("── 注入 Person 种子 (%d 个 ID) ──", len(_PERSON_SEED_IDS))
    try:
        pe = PersonEnricher(client)
        persons = await pe.enrich_batch(_PERSON_SEED_IDS)
        valid = [p for p in persons if "_error" not in p]
        if valid:
            n = ingestor.ingest_persons(valid)
            logger.info("Person: %d/%d 条写入", n, len(valid))
        failed = len(persons) - len(valid)
        if failed:
            logger.warning("  %d 条富化失败，跳过", failed)
    except Exception as e:
        logger.warning("Person 种子注入失败: %s", e)

    await client.close()

    logger.info("══ 种子数据注入完成 ══")


if __name__ == "__main__":
    asyncio.run(run_seed())
