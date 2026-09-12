"""
RAG 语料库批量灌入脚本 — Phase 2
══════════════════════════════════════════════════════════════════════
给后端初学者的说明 (2026-08-10)
══════════════════════════════════════════════════════════════════════

这个脚本做了什么？用大白话说：
  我们从 Phase 1（rag/cli/discover.py）拿到了一份"名单"——
  哪些动漫作品、角色、人物是需要存进数据库的（1500 个 ID）。
  这个脚本的任务就是：
    1. 读取名单
    2. 去 Bangumi 网站 API 拉取每个 ID 的详细信息（评分、标签、简介…）
    3. 把这些信息转成 AI 能理解的"向量"（1024 个数字）
    4. 全部写进 PostgreSQL 数据库的 rag_entities 表

整个流程用 4 个 Phase 串联，每个 Phase 依赖前一个的输出。


══════════════════════════════════════════════════════════════════════
前置概念：几个你会在代码里反复看到的东西
══════════════════════════════════════════════════════════════════════

【async / await — "不等了，先去干别的"】
  Python 默认是"一行一行等"的（同步）：发了 HTTP 请求就干等着，网络花 0.5 秒
  返回了才执行下一行。如果我们要取 900 部动漫的数据，900 × 0.5 = 450 秒，太慢了。

  async/await 让程序在等待网络的时候"先去做别的事"：
    await client.get(url)  →  发出请求，不等，切去处理下一个请求
    网络返回了          →  切回来继续执行

  这就是为什么 main() 函数的定义前面有 async 关键字。asyncio.run(main())
  是 Python 启动这个"可以同时做多件事"的环境。

  类比：就像你在等水烧开的同时去切菜，而不是站着干等水开再去切菜。

【httpx.AsyncClient — "和网站对话的接线员"】
  httpx 是一个 Python HTTP 库，负责向 Bangumi API 发送请求和接收响应。
  AsyncClient 是它的异步版本，可以同时管理多个请求。
  想象它是一个电话接线员，你告诉它"帮我问 Bangumi 服务器，id=328609 的作品是啥"，
  它打电话过去，拿到答案后告诉你。

【SQLAlchemy Session — "和数据库的一次完整对话"】
  你对数据库的所有操作（查、增、删、改）都包在一个 Session 里。
  Session 像一个"草稿本"——你在上面改数据，但数据库还没真改。
  只有调用 session.commit() 时，草稿本的内容才正式写入数据库。
  如果中途出错、调了 session.rollback()，草稿本就作废，数据库维持原样。

  类比：就像你编辑一个 Word 文档时，所有修改只在内存里，
  按 Ctrl+S（commit）才保存到硬盘。

【session.merge() — "有就更新，没有就新增"（UPSERT）】
  我们用的是 session.merge() 而不是 session.add()。
  merge 的意思是：
    - 如果这条数据已经存在（ID 相同）→ 用新数据覆盖旧数据
    - 如果不存在 → 插入新记录
  这保证我们多次跑脚本不会产生重复数据。

【向量化 (embedding) — "把文字变成 AI 能理解的数字串"】
  数据库里存的 embedding 列是 1024 个浮点数。这串数字是由智谱的 embedding-2
  模型生成的——输入一段文本（如"孤独摇滚 芳文社 音乐…"），输出 1024 个数字。
  两段语义相似的文字，数字串在数学上也"接近"。
  后续检索时，用户的问题也变成数字串，然后在数据库里找最接近的几条。


══════════════════════════════════════════════════════════════════════
端到端数据流
══════════════════════════════════════════════════════════════════════

  Phase 1（本脚本内）
  ┌─────────────────────────────────────────────┐
  │ scripts/data/subject_ids_type2.json (900个)  │
  │ scripts/data/subject_ids_type1.json (130个)  │
  │ scripts/data/character_ids.json     (350个)  │
  │ scripts/data/person_ids.json        (120个)  │
  │    ↓                                        │
  │ 头部 800+100+300+100 + 尾部随机采样            │
  │    ↓                                        │
  │ 最终: 1500 个 ID                            │
  └─────────────────────────────────────────────┘
         ↓
  Phase 3（本脚本内）: API 富化
  ┌─────────────────────────────────────────────┐
  │ 对每个 ID 调用 Bangumi p1 API 获取:           │
  │   作品名、评分、标签、播出日期、简介……         │
  │   角色介绍、出演作品、声优信息……              │
  │   人物职业、代表作……                          │
  │    ↓                                        │
  │ rag/enricher.py 的 SubjectCollector /        │
  │ CharacterEnricher / PersonEnricher           │
  │    ↓                                        │
  │ 输出: 结构化的 dict 列表 (anime_data 等)       │
  └─────────────────────────────────────────────┘
         ↓
  Phase 4（本脚本内 → rag/ingestion.py）: 向量化 + 灌入
  ┌─────────────────────────────────────────────┐
  │ 对每条富化后的数据:                           │
  │   1. 构造 embed_text（关键词密集文本）         │
  │   2. 调用 embedding-2 API → 1024 维向量       │
  │   3. 构造 rag_output（预构建返回 JSON）        │
  │   4. 构造 meta_info（结构化元数据）             │
  │   5. session.merge(entity) → 写入 PostgreSQL │
  └─────────────────────────────────────────────┘


用法::

    source .venv/bin/activate
    python -m rag.cli.ingest                # 全部流程（不进 --clear，不删旧数据）
    python -m rag.cli.ingest --clear        # 先清空 rag_entities，再灌入（推荐）
    python -m rag.cli.ingest --subjects-only # 只灌 Subject（动画+书籍）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from pathlib import Path

# ── 日志配置 ──────────────────────────────────────────────────────
# level=INFO 表示会打印所有 INFO / WARNING / ERROR 级别的消息
# 如果改成 DEBUG，会多打印大量细节（适合排查问题但会很吵）
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("ingest_corpus")

# ── 项目根目录 ────────────────────────────────────────────────────
# __file__  = rag/cli/ingest.py 的绝对路径
# .resolve()  = 把相对路径转成绝对路径
# .parent    = rag/cli/
# .parent    = rag/
# .parent    = 项目根目录 (bgm-agent-dev/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "scripts" / "data"


# ═══════════════════════════════════════════════════════════════════
# 头部/尾部采样配置
# ═══════════════════════════════════════════════════════════════════
#
# 为什么要分头部和尾部？
# ─────────────────
# 从 Bangumi 按收藏数排名发现 ID 时，默认都是热门作品（头部）。
# 但 RAG 检索评测如果只用热门作品，就无法衡量对冷门内容的检索能力——
# 冷门作品名字短、简介少、标签少，向量化后语义信号弱，检索更难。
#
# 所以：头部用确定性 Top-N（保证热门覆盖），
#       尾部从剩余 ID 中随机采样（保证冷门多样性）。
# 固定随机种子 TAIL_SEED=42，保证每次采样结果可复现。

# 头部：按收藏数排名确定性选取 Top-N
HEAD_SIZES: dict[str, int] = {
    "subject_type2": 800,   # 动画 Top-800 热门
    "subject_type1": 100,   # 书籍 Top-100 热门
    "character": 300,       # 角色 Top-300 热门
    "person": 100,          # 人物 Top-100 热门
}

# 短尾随机：从 Top-N 之后的剩余 ID 中随机采样
TAIL_DISTRIBUTION: dict[str, int] = {
    "subject_type2": 100,   # 动画尾部随机 100
    "subject_type1": 30,    # 书籍尾部随机 30
    "character": 50,        # 角色尾部随机 50
    "person": 20,           # 人物尾部随机 20
}

TAIL_SEED = 42  # 固定随机种子——保证每次跑脚本采样的尾巴是同一批 ID


# ═══════════════════════════════════════════════════════════════════
# Phase 1 产物 → ID 列表加载
# ═══════════════════════════════════════════════════════════════════
#
# 映射表：CLI 参数的 key（如 "subject_type2"）对应到:
#   - 哪个 JSON 文件（scripts/data/ 目录下）
#   - 显示标签（中文名，日志用）
#   - entity_type（存入数据库 entity_type 列的值）

_DISCOVERY_FILES: dict[str, dict] = {
    "subject_type2": {
        "file": "subject_ids_type2.json",
        "label": "Subject (anime)",
        "entity_type": "subject",
    },
    "subject_type1": {
        "file": "subject_ids_type1.json",
        "label": "Subject (book)",
        "entity_type": "subject",
    },
    "character": {
        "file": "character_ids.json",
        "label": "Character",
        "entity_type": "character",
    },
    "person": {
        "file": "person_ids.json",
        "label": "Person",
        "entity_type": "person",
    },
}


def _load_ids(key: str) -> list[int]:
    """从 Phase 1 JSON 产物中加载实体 ID 列表。

    参数 key 是上面的键名（如 "subject_type2"），函数找到对应的 JSON 文件，
    读取里面的所有 ID。
    """
    filepath = DATA_DIR / _DISCOVERY_FILES[key]["file"]
    if not filepath.exists():
        logger.error("%s 不存在，请先运行 python -m rag.cli.discover", filepath)
        return []

    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    ids = [item["id"] for item in data if isinstance(item.get("id"), int)]
    logger.info("加载 %s: %d 个 ID (%s)", _DISCOVERY_FILES[key]["label"], len(ids), filepath.name)
    return ids


# ═══════════════════════════════════════════════════════════════════
# 主流程 — async def main()
# ═══════════════════════════════════════════════════════════════════
#
# 整个脚本的入口函数。async 关键字表示这个函数内部可以 await 异步操作。
# 脚本最后一行 asyncio.run(main()) 负责启动异步环境并执行 main()。


async def main():
    # ── 命令行参数解析 ────────────────────────────────────────────
    # argparse 是 Python 标准库，把你在命令行打的 --clear 等参数转成
    # 程序能读到的变量（如 args.clear = True）
    parser = argparse.ArgumentParser(
        description="RAG 语料库批量灌入 — Phase 2"
    )
    parser.add_argument("--clear", action="store_true",
                        help="清空 rag_entities 后重新灌入")
    parser.add_argument("--subjects-only", action="store_true",
                        help="仅灌入 Subject（动画 + 书籍）")
    parser.add_argument("--characters-only", action="store_true",
                        help="仅灌入 Character（角色）")
    parser.add_argument("--persons-only", action="store_true",
                        help="仅灌入 Person（现实人物）")
    args = parser.parse_args()

    # ── 初始化客户端 ──────────────────────────────────────────────
    # BangumiClient 封装了和 bangumi.tv 网站通信的逻辑（发 HTTP 请求、处理限流、
    # 重试失败的请求等）。它需要 API Token 来认证身份（证明"我是开发者，有权调用 API"）。
    #
    # 这些配置值不是在代码里写死的，而是从 .env 文件或环境变量中读取的
    # （core/config.py 的 get_settings() 负责），这样敏感信息（token/key）不会
    # 出现在代码里。
    from clients.client import BangumiClient
    from core.config import get_settings

    settings = get_settings()
    client = BangumiClient(access_token=settings.BANGUMI_ACCESS_TOKEN or None)

    # 如果指定了 --subjects-only，run_all = False（只跑 subject 部分）
    #
    # ⚠️ 未启用的开关，【不是死代码，别删】：`run_all` 算出来之后全文件再没被读过，
    #    所以 `--subjects-only` / `--characters-only` / `--persons-only` 三个参数
    #    在 --help 里写着，实际【完全无效】—— 带上它们照样三类实体全灌。
    #    危险组合：`--subjects-only --clear` 会清库并重灌全部三类，与参数的承诺相反。
    #    修法不是删这一行（删了 bug 就再也看不见了），是让各 Phase 真的读它。
    run_all = not (args.subjects_only or args.characters_only or args.persons_only)


    # ═════════════════════════════════════════════════════════════════
    # Phase 1: 加载 ID 列表 + 头部/尾部拆分
    # ═════════════════════════════════════════════════════════════════
    #
    # 这一步做的事情：
    #   1. 从 scripts/data/*.json 读取 Phase 1 发现的全部 ID
    #   2. 对每种实体类型，取前 N 个（头部）+ 从剩余 ID 中随机抽 M 个（尾部）
    #   3. 头部保证热门覆盖，尾部保证冷门多样性
    #
    # 这些 JSON 文件是 Phase 1（python -m rag.cli.discover）的产出。
    # 例如 subject_ids_type2.json 是这样的：
    #   [{"id": 328609, "name": "ぼっち・ざ・ろっく！", "name_cn": "孤独摇滚！", ...}, ...]
    print("\n" + "=" * 60)
    print("  Phase 1: 加载 ID 列表 + 头部/尾部拆分")
    print("=" * 60)

    random.seed(TAIL_SEED)  # 固定随机种子，保证可复现

    # 加载每个来源的完整 ID 列表（保持排名顺序——越靠前越热门）
    all_ids: dict[str, list[int]] = {}
    for key in _DISCOVERY_FILES:
        all_ids[key] = _load_ids(key)

    # 拆分头部（确定性 Top-N）和尾部（随机采样）
    final_ids: dict[str, list[int]] = {}
    tail_summary: list[str] = []  # 收集汇总信息，稍后一次性打印

    for key, ids in all_ids.items():
        head_size = HEAD_SIZES.get(key, 0)
        tail_count = TAIL_DISTRIBUTION.get(key, 0)

        head = ids[:head_size]             # 列表切片：从第 0 个到第 head_size-1 个
        tail_pool = ids[head_size:]        # 剩下的全部是"尾巴候选池"

        if tail_pool and tail_count > 0:
            # random.sample(池子, 抽几个) 从候选池中随机选 tail_count 个，不重复
            actual_tail = min(tail_count, len(tail_pool))
            tail = random.sample(tail_pool, actual_tail)
        else:
            tail = []

        final_ids[key] = head + tail       # 头部 + 尾部拼成最终列表
        label = _DISCOVERY_FILES[key]["label"]
        tail_summary.append(
            f"  {label:20s}: head={len(head):>4d} + tail={len(tail):>3d} = {len(final_ids[key]):>4d}"
        )

    print()
    for line in tail_summary:
        print(line)

    # 按实体类型拆分 ID 列表，方便后续分别富化
    anime_ids = final_ids["subject_type2"]       # 动画 ID 列表
    book_ids = final_ids["subject_type1"]        # 书籍 ID 列表
    character_ids = final_ids["character"]       # 角色 ID 列表
    person_ids = final_ids["person"]             # 人物 ID 列表

    total_ids = len(anime_ids) + len(book_ids) + len(character_ids) + len(person_ids)
    print(f"  {'─' * 45}")
    print(f"  总计: {total_ids} (subject={len(anime_ids)+len(book_ids)}, "
          f"character={len(character_ids)}, person={len(person_ids)})")


    # ═════════════════════════════════════════════════════════════════
    # Phase 2: 清空旧数据（仅当你加了 --clear 参数时执行）
    # ═════════════════════════════════════════════════════════════════
    #
    # SQL 的 DELETE FROM 是物理删除，不是"标记删除"——数据真的从硬盘上消失了。
    # 所以 --clear 请谨慎使用，确保你知道自己在干什么。
    #
    # 为什么需要清空？
    #   因为用 session.merge() 灌入时，它是按 ID 匹配的。
    #   如果之前灌入了 900 部动画，这次只灌 800 部（因为头部数量改了），
    #   那多出来的 100 部旧数据会保留——你可能不想要它们。--clear 避免了这个问题。
    #
    # commit()：把 DELETE 指令真正执行到数据库。不调 commit，SQL 不会生效。
    if args.clear:
        print("\n" + "=" * 60)
        print("  Phase 2: 清空旧数据")
        print("=" * 60)
        from database.engine import engine
        from sqlmodel import Session, text
        # with Session(engine) as session:  ← 创建一次"数据库对话"
        #   进入 with 块 = 开始对话
        #   离开 with 块 = 自动关闭对话（释放数据库连接）
        with Session(engine) as session:
            result = session.exec(text("DELETE FROM rag_entities"))
            session.commit()  # Ctrl+S——正式写入
            count = getattr(result, 'rowcount', None)
            logger.info("已清空 rag_entities (删除 %s 行)", count if count is not None else "?")


    # ═════════════════════════════════════════════════════════════════
    # Phase 3: API 富化
    # ═════════════════════════════════════════════════════════════════
    #
    # 这是最耗时的阶段（3~5 分钟）。
    #
    # 对每个 ID，去 Bangumi 网站 API 拉取详细信息。
    # Subject（作品）通过 p1 API 单步获取（一条请求就够），
    # Character 和 Person 需要两步（multi-hop）：
    #   先请求角色/人物基本信息 → 再请求他们的出演作品/代表作列表。
    #
    # 为什么富化和灌入要分开（不用实时调用）？
    #   Bangumi API 有频率限制（0.2~0.3 秒/请求），如果每次用户搜索都临时去
    #   拉 API，响应时间会是 2~5 秒——太慢了。预先拉好存进数据库，检索时直接
    #   从本地数据库读，响应时间降到 0.05 秒。
    print("\n" + "=" * 60)
    print("  Phase 3: API 富化")
    print("=" * 60)

    # 初始化空列表 —— 各类型富化后的结果会填充到这里
    anime_data: list[dict] = []
    book_data: list[dict] = []
    characters_data: list[dict] = []
    persons_data: list[dict] = []

    # ── Subject（作品）富化 ─────────────────────────────────────────
    # SubjectCollector 封装了"去 Bangumi p1 API 拉取作品详情"的逻辑。
    # 它内部使用了 async + Semaphore（信号量）来控制并发——
    # 同时最多发 3 个请求，避免把 Bangumi 服务器打爆。
    if anime_ids or book_ids:
        from ..enricher import SubjectCollector
        collector = SubjectCollector(client)

    if anime_ids:
        logger.info("── Subject anime (%d IDs) ──", len(anime_ids))
        # await = "等这个异步操作完成，但不等的时候可以做别的事"
        # sorted() 把 ID 从小到大排序（为了稳定——每次跑脚本顺序一致）
        raw = await collector.collect_batch(sorted(anime_ids))
        # 过滤掉富化失败的条目（_error key 表示 API 出错了，跳过）
        valid = [r for r in raw if "_error" not in r]
        anime_data = valid
        logger.info("  富化: %d/%d 成功", len(valid), len(raw))

    if book_ids:
        logger.info("── Subject book (%d IDs) ──", len(book_ids))
        raw = await collector.collect_batch(sorted(book_ids))
        valid = [r for r in raw if "_error" not in r]
        book_data = valid
        logger.info("  富化: %d/%d 成功", len(valid), len(raw))

    # ── Character（角色）富化 ────────────────────────────────────────
    # CharacterEnricher 做两步：
    #   1. 调用 API 获取角色详情（名称、简介、出演角色类型）
    #   2. 调用 API 获取该角色出演了哪些作品（casts）
    if character_ids:
        from ..enricher import CharacterEnricher
        logger.info("── Character (%d IDs) ──", len(character_ids))
        enricher = CharacterEnricher(client)
        raw = await enricher.enrich_batch(sorted(character_ids))
        valid = [r for r in raw if "_error" not in r]
        characters_data = valid
        logger.info("  富化: %d/%d 成功", len(valid), len(raw))

    # ── Person（现实人物）富化 ──────────────────────────────────────
    # PersonEnricher 和 CharacterEnricher 类似：
    #   1. 获取人物详情（职业、简介）
    #   2. 获取代表作列表（works）
    if person_ids:
        from ..enricher import PersonEnricher
        logger.info("── Person (%d IDs) ──", len(person_ids))
        enricher = PersonEnricher(client)
        raw = await enricher.enrich_batch(sorted(person_ids))
        valid = [r for r in raw if "_error" not in r]
        persons_data = valid
        logger.info("  富化: %d/%d 成功", len(valid), len(raw))


    # ═════════════════════════════════════════════════════════════════
    # Phase 4: Embedding + 灌入
    # ═════════════════════════════════════════════════════════════════
    #
    # 这是"把数据写入数据库"的最后一步，也是最关键的一步。
    #
    # RagEntityIngestor（rag/ingestion.py）干了三件事：
    #   1. 对每条实体构造 embed_text（如"孤独摇滚 芳文社 音乐…"）
    #   2. 调用 智谱 embedding-2 API，把 embed_text → 1024 维向量
    #   3. 用 session.merge() 写入 rag_entities 表
    #
    # 为什么动画和书籍分开灌入？
    #   ingest_subjects() 的 subject_type 参数不一样：
    #     动画 → subject_type=2
    #     书籍 → subject_type=1
    #   这个值会写入数据库，后续检索时 WHERE subject_type=2 就只看动画。
    #
    # 每批次数据的处理是原子的——要么整批成功，要么整批失败回滚。
    print("\n" + "=" * 60)
    print("  Phase 4: Embedding + 灌入")
    print("=" * 60)

    from database.engine import engine
    from ..ingestion import RagEntityIngestor

    # 创建摄入器。engine 是数据库连接入口，
    # zhipu_api_key 用于调用 embedding 模型
    ingestor = RagEntityIngestor(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
        zhipu_base_url=settings.ZHIPU_BASE_URL,
    )

    n_anime = n_book = n_character = n_person = 0

    if anime_data:
        logger.info("灌入 %d 条 anime subject...", len(anime_data))
        n_anime = ingestor.ingest_subjects(anime_data, subject_type=2)
        logger.info("  ✓ %d anime", n_anime)

    if book_data:
        logger.info("灌入 %d 条 book subject...", len(book_data))
        n_book = ingestor.ingest_subjects(book_data, subject_type=1)
        logger.info("  ✓ %d book", n_book)

    if characters_data:
        logger.info("灌入 %d 条 character...", len(characters_data))
        n_character = ingestor.ingest_characters(characters_data)
        logger.info("  ✓ %d character", n_character)

    if persons_data:
        logger.info("灌入 %d 条 person...", len(persons_data))
        n_person = ingestor.ingest_persons(persons_data)
        logger.info("  ✓ %d person", n_person)


    # ═════════════════════════════════════════════════════════════════
    # 总结
    # ═════════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  灌入完成")
    print("=" * 60)
    n_subject = n_anime + n_book
    print(f"  Subject:   {n_subject} (anime={n_anime}, book={n_book})")
    print(f"  Character: {n_character}")
    print(f"  Person:    {n_person}")
    print(f"  ─────────────────")
    print(f"  总计:      {n_subject + n_character + n_person}")
    print()

    # 关闭 HTTP 客户端（释放连接——好习惯）
    await client.close()


# ── 程序入口 ──────────────────────────────────────────────────────
# __name__ 是 Python 的特殊变量：
#   当你直接跑这个文件（python -m rag.cli.ingest）时，__name__ == "__main__"
#   当你从别的地方 import 这个文件时，__name__ == "rag.cli.ingest"（不执行 main）
# 这保证了 "作为脚本跑"和"作为模块导入"互不干扰。
if __name__ == "__main__":
    asyncio.run(main())
