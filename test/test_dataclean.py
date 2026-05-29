"""
数据清洗与 Chunk 切分测试脚本 (Data Cleaning & Chunking Test)

从 test_rawtext.py 抓取的 JSONL 中加载原始数据，依次执行：
  1. clean_text()  — 清洗噪音（全角空格、连续换行、BBCode 等）
  2. split_text()  — tiktoken 滑动窗口切分
  3. 保存为 cleaned_chunks.jsonl，每行一个 chunk + 元数据
  4. 打印详细统计和 RAG 就绪度评估报告
"""

import json
import os
import sys

# 确保能导入根目录的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rag.text_processor import BangumiTextProcessor

# ── 路径配置 ──────────────────────────────────────────────
RAW_JSONL = "test/mock_data/raw_subjects.jsonl"
OUTPUT_DIR = "test/mock_data"
CHUNKED_JSONL = os.path.join(OUTPUT_DIR, "cleaned_chunks.jsonl")

# ── 切分参数 ──────────────────────────────────────────────
CHUNK_SIZE = 200  # 每个 chunk 的 Token 上限
CHUNK_OVERLAP = 30  # 相邻 chunk 重叠 Token 数


def load_raw_records(path: str) -> list[dict]:
    """从 JSONL 加载原始抓取数据。"""
    if not os.path.exists(path):
        print(f"❌ 未找到原始数据文件: {path}")
        print("   请先运行 python test/test_rawtext.py 生成数据")
        sys.exit(1)

    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    print(f"📂 从 {path} 加载了 {len(records)} 条原始记录")
    return records


def process_record(
    processor: BangumiTextProcessor,
    record: dict,
) -> dict:
    """对单条记录执行清洗 + 切分，返回增强后的结果。"""
    raw_text = record.get("short_summary", "") or ""
    name = record.get("name_cn") or record.get("name", "未知")

    # ── Step 1: 清洗 ──
    cleaned = processor.clean_text(raw_text)

    # ── Step 2: 切分 ──
    chunks = processor.split_text(raw_text)  # split_text 内部会调用 clean_text

    # ── 统计 ──
    record["_raw_chars"] = len(raw_text)
    record["_cleaned_chars"] = len(cleaned)
    record["_chunk_count"] = len(chunks)

    # 记录每个 chunk 供下游使用
    record["_chunks"] = chunks

    return record


def save_cleaned_chunks(records: list[dict], output_path: str) -> str:
    """将清洗后的 chunks 保存为 JSONL，每行一个独立 chunk。"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    chunk_id = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for rec in records:
            subject_id = rec["subject_id"]
            name = rec.get("name_cn") or rec.get("name", "未知")
            tags = rec.get("tags", [])
            score = rec.get("score", 0)
            type_id = rec.get("type", 0)

            for chunk_text in rec.get("_chunks", []):
                chunk_id += 1
                chunk_record = {
                    "chunk_id": chunk_id,
                    "subject_id": subject_id,
                    "name": name,
                    "type": type_id,
                    "score": score,
                    "tags": tags,
                    "text": chunk_text,
                }
                f.write(json.dumps(chunk_record, ensure_ascii=False) + "\n")

    print(f"📄 Chunk 已保存: {output_path} ({chunk_id} 个 chunk)")
    return output_path


def print_evaluation_report(records: list[dict]) -> None:
    """打印 RAG 就绪度评估报告。"""
    total_raw = sum(r["_raw_chars"] for r in records)
    total_cleaned = sum(r["_cleaned_chars"] for r in records)
    total_chunks = sum(r["_chunk_count"] for r in records)
    records_with_text = [r for r in records if r["_raw_chars"] > 0]
    records_empty = [r for r in records if r["_raw_chars"] == 0]

    print(f"\n{'=' * 55}")
    print("📊 RAG 就绪度评估报告")
    print(f"{'=' * 55}")

    # ── 1. 覆盖度 ──
    print("\n【1. 数据覆盖度】")
    print(f"   总条目:      {len(records)}")
    print(f"   有简介文本:   {len(records_with_text)}")
    print(f"   空简介:      {len(records_empty)}")
    if records_empty:
        names = [r.get("name_cn") or r.get("name", "?") for r in records_empty]
        print(f"   空简介条目:   {', '.join(names)}")

    # ── 2. 清洗效果 ──
    print("\n【2. 清洗效果】")
    print(f"   清洗前总字符: {total_raw}")
    print(f"   清洗后总字符: {total_cleaned}")
    if total_raw > 0:
        reduction = (1 - total_cleaned / total_raw) * 100
        print(f"   字符缩减率:   {reduction:.1f}%")
    print(f"   总 chunk 数:  {total_chunks}")

    # ── 3. 各条目详情 ──
    print("\n【3. 各条目详情】")
    print(f"   {'条目':28s} {'原始字符':>8s} {'清洗后':>8s} {'chunks':>6s} {'状态'}")
    print(f"   {'-' * 28} {'-' * 8} {'-' * 8} {'-' * 6} {'-' * 4}")
    for r in records:
        name = (r.get("name_cn") or r.get("name", "?"))[:28]
        raw_c = r["_raw_chars"]
        clean_c = r["_cleaned_chars"]
        n_chunks = r["_chunk_count"]
        status = "✅" if n_chunks > 0 else "⚠️ 空"
        print(f"   {name:28s} {raw_c:>8d} {clean_c:>8d} {n_chunks:>6d} {status}")

    # ── 4. RAG 就绪度评估 ──
    print("\n【4. RAG 管道就绪度评估】")

    issues: list[str] = []

    # 4a. 空文本处理
    if len(records_empty) > 0:
        issues.append(
            f"⚠️  {len(records_empty)} 条条目简介为空，RAG 检索将无文本可嵌。"
            f" 建议: fallback 到其他字段或过滤跳过。"
        )
    else:
        issues.append("✅ 所有条目均有可嵌入文本。")

    # 4b. 清洗质量
    if total_cleaned < total_raw * 0.5:
        issues.append(
            f"⚠️  清洗后字符缩减 {reduction:.0f}%，幅偏大，请检查是否误删有效内容。"
        )
    else:
        issues.append(
            f"✅ 清洗缩减 {reduction:.1f}%，主要是空白/换行归一化，属于正常范围。"
        )

    # 4c. Chunk 粒度
    if total_chunks == len(records):
        issues.append(
            "⚠️  所有条目均未触发切分（每个条目仅 1 个 chunk）。"
            " 建议降低 chunk_size 或确认文本是否足够长。"
        )
    elif total_chunks > len(records) * 2:
        avg_chunks = total_chunks / len(records_with_text) if records_with_text else 0
        issues.append(f"✅ 平均每条目 {avg_chunks:.1f} 个 chunk，切分粒度合理。")
    else:
        avg_chunks = total_chunks / len(records_with_text) if records_with_text else 0
        issues.append(
            f"📌 平均每条目 {avg_chunks:.1f} 个 chunk，部分长文本触发了切分。"
        )

    # 4d. 缺失清洗能力
    issues.append(
        "📌 BBCode 清洗尚未接入（clean_text 中有 TODO）。"
        " 如果后续抓取长评（带 [b]、[url] 等 BBCode），需补充此步骤。"
    )

    for issue in issues:
        print(f"   {issue}")

    print(f"\n{'=' * 55}")


def print_sample_outputs(records: list[dict], sample_count: int = 3) -> None:
    """打印几个代表性样本的清洗前后对比。"""
    print(f"\n{'=' * 55}")
    print("🔍 清洗前后对比样本")
    print(f"{'=' * 55}")

    shown = 0
    for r in records:
        if shown >= sample_count:
            break
        raw = r.get("short_summary", "") or ""
        if len(raw) < 20:
            continue  # 跳过太短的

        name = r.get("name_cn") or r.get("name", "?")
        cleaned = BangumiTextProcessor().clean_text(raw)

        print(f"\n▶ {name} (ID={r['subject_id']})")
        print(f"  ── 清洗前 ({len(raw)} chars) ──")
        print(f"  {repr(raw[:200])}")
        print(f"  ── 清洗后 ({len(cleaned)} chars) ──")
        print(f"  {repr(cleaned[:200])}")

        # 展示前 2 个 chunk
        chunks = r.get("_chunks", [])
        if len(chunks) > 1:
            print("  ── Chunk 1/2 预览 ──")
            print(f"  {chunks[0][:120]}")
            print("  ── Chunk 2/2 预览 ──")
            print(f"  {chunks[1][:120]}")

        shown += 1


def main() -> None:
    print(f"{'=' * 55}")
    print("🧹 Bangumi 数据清洗 & Chunk 切分测试")
    print(f"{'=' * 55}")

    # 1. 加载原始数据
    records = load_raw_records(RAW_JSONL)

    # 2. 初始化处理器
    processor = BangumiTextProcessor(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    print(f"🔧 chunk_size={CHUNK_SIZE}, chunk_overlap={CHUNK_OVERLAP}")
    print(f"🔧 tokenizer={processor.tokenizer.name}\n")

    # 3. 批量处理
    processed: list[dict] = []
    for rec in records:
        processed.append(process_record(processor, rec))

    # 4. 保存 cleaned chunks
    save_cleaned_chunks(processed, CHUNKED_JSONL)

    # 5. 打印评估报告
    print_evaluation_report(processed)

    # 6. 打印样本
    print_sample_outputs(processed)

    print(f"\n{'=' * 55}")
    print("✅ 清洗测试完成！")
    print(f"{'=' * 55}")


if __name__ == "__main__":
    main()
