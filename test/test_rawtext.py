"""
原始数据抓取脚本 (Raw Data Fetcher)

抓取 Bangumi 上不同风格、不同类型的条目原始文本数据，用于后续
RAG 模块的清洗（去 BBCode、归一化等）和向量化前的素材准备。

抓取策略：
  - 涵盖多种文本风格（古早 BBCode / 现代纯文本 / 轻小说风）
  - 涵盖多种条目类型（动画 / 书籍 / 音乐 / 游戏 / 三次元）
  - 异步并发抓取，通过 Semaphore 控制并发数防止触发 API 限流
  - 输出格式为 JSONL，每行一个完整条目，便于后续管道处理
"""

import asyncio
import json
import os
import sys
import time

# 将项目根目录加入 sys.path，确保从 test/ 下能正确导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.bgm_tools import get_bangumi_subject_detail

# ── 并发控制 ──────────────────────────────────────────────
MAX_CONCURRENT = 3  # Bangumi API 友好并发数
REQUEST_DELAY = 0.5  # 同一 session 内请求间隔（秒）

# ── 目标条目清单 ──────────────────────────────────────────
# 按文本风格 & 条目类型分组，确保 RAG 清洗管道覆盖多样化的 raw 输入
# 格式: (subject_id, 标签/说明)
TARGET_SUBJECTS: list[tuple[int, str]] = [
    # ── 动画 (type=2) ──────────────────────────────────
    (8, "动画-凉宫春日的忧郁(古早BBCode)"),
    (844, "动画-命运石之门(古早BBCode长简介)"),
    (169, "动画-CLANNAD ~AFTER STORY~(催泪/评分Top)"),
    (240038, "动画-终将成为你(新时代百合/情感细腻)"),
    (302186, "动画-我推的孩子(热门新番/偶像题材)"),
    (374739, "动画-葬送的芙莉莲(2023霸权/奇幻治愈)"),
    (428041, "动画-我心里危险的东西(恋爱喜剧/高分)"),
    # ── 书籍/漫画 (type=1) ────────────────────────────
    (104, "书籍-钢之炼金术师(经典漫画)"),
    (1597, "书籍-四畳半神话大系(轻小说/冷幽默)"),
    (2900, "书籍-三体(科幻小说/中文)"),
    # ── 音乐 (type=3) ────────────────────────────────
    (24668, "音乐-命运石之门OP(动画歌曲)"),
    (388559, "音乐-YOASOBI-アイドル(2023热门)"),
    # ── 游戏 (type=4) ────────────────────────────────
    (367751, "游戏-艾尔登法环(开放世界/高文本量)"),
    # ── 三次元 (type=6) ──────────────────────────────
    (303124, "三次元-孤独的美食家(日剧/生活)"),
]


async def fetch_one(
    sem: asyncio.Semaphore,
    subject_id: int,
    label: str,
) -> dict:
    """异步抓取单个条目，返回结构化数据。"""
    async with sem:
        print(f"⏳ [{(label):30s}] 正在抓取...", flush=True)
        raw_json = await get_bangumi_subject_detail(subject_id=subject_id)
        data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json

        # 记录元数据与原始字段
        record = {
            "subject_id": subject_id,
            "label": label,
            "name": data.get("name", ""),
            "name_cn": data.get("name_cn", ""),
            "type": data.get("type", 0),
            "score": data.get("score", 0.0),
            "rank": data.get("rank", 0),
            "date": data.get("date", ""),
            "platform": data.get("platform", ""),
            "total_episodes": data.get("total_episodes", 0),
            "short_summary": data.get("short_summary", ""),
            "tags": data.get("tags", []),
            "collection": data.get("collection", {}),
        }

        name_cn = record["name_cn"] or record["name"]
        print(
            f"  ✅ [{name_cn}] 抓取成功 (summary={len(record['short_summary'])} chars)"
        )
        return record


async def fetch_all() -> list[dict]:
    """并发抓取所有目标条目。"""
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = [fetch_one(sem, sid, label) for sid, label in TARGET_SUBJECTS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    records: list[dict] = []
    for i, result in enumerate(results):
        sid, label = TARGET_SUBJECTS[i]
        if isinstance(result, Exception):
            print(f"❌ [{(label):30s}] 抓取失败: {result}")
        else:
            records.append(result)
        await asyncio.sleep(REQUEST_DELAY)

    return records


def save_as_jsonl(records: list[dict], output_dir: str) -> str:
    """保存为 JSONL 格式（每行一个完整 JSON 对象）。"""
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "raw_subjects.jsonl")

    with open(output_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\n📄 JSONL 已保存: {output_path} ({len(records)} 条记录)")
    return output_path


def save_as_human_readable(records: list[dict], output_dir: str) -> str:
    """同时保存一份人类可读的纯文本版本，方便肉眼检查。"""
    output_path = os.path.join(output_dir, "raw_summary.txt")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# Bangumi Raw Data Dump\n")
        f.write(f"# 抓取时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# 条目数: {len(records)}\n\n")

        for rec in records:
            name_cn = rec["name_cn"] or rec["name"]
            f.write(f"{'=' * 40}\n")
            f.write(f"=== {name_cn} (ID: {rec['subject_id']}) ===\n")
            f.write(
                f"评分: {rec['score']}  |  排名: {rec['rank']}  |  类型: {rec['type']}  |  平台: {rec['platform']}\n"
            )
            f.write(f"标签: {', '.join(rec['tags'][:8])}\n\n")
            f.write("── 简介 ──\n")
            f.write(rec["short_summary"] or "(无简介)")
            f.write("\n\n" + f"{'=' * 40}\n\n")

    print(f"📄 TXT 已保存: {output_path}")
    return output_path


def main():
    print("🚀 开始抓取 Bangumi 原始数据...")
    print(f"   目标条目数: {len(TARGET_SUBJECTS)}")
    print(f"   最大并发: {MAX_CONCURRENT}\n")

    records = asyncio.run(fetch_all())

    output_dir = "test/mock_data"
    jsonl_path = save_as_jsonl(records, output_dir)
    txt_path = save_as_human_readable(records, output_dir)

    # 打印统计摘要
    success = len(records)
    failed = len(TARGET_SUBJECTS) - success
    total_chars = sum(len(r["short_summary"]) for r in records)
    avg_chars = total_chars / success if success else 0

    print(f"\n{'=' * 40}")
    print("📊 抓取统计:")
    print(f"   成功: {success} / {len(TARGET_SUBJECTS)}")
    print(f"   失败: {failed}")
    print(f"   简介总字符: {total_chars}")
    print(f"   平均简介长度: {avg_chars:.0f} chars")
    print(f"   输出: {jsonl_path}")
    print(f"         {txt_path}")


if __name__ == "__main__":
    main()
