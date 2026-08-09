"""
RAG 模块内部共享工具函数

私有模块（_ 前缀），不对外暴露。提供 enricher / ingestion / retriever
三个子模块共用的文本清洗和截断工具。
"""

from __future__ import annotations

import html
import re

# 安全上限：embedding-2 单条 ≤512 tokens，CJK ~1.5–2 tokens/char
_MAX_EMBED_CHARS = 400

# BBcode 标签（Bangumi wiki 文本中常见）
_BBCODE_RE = re.compile(
    r'\[(?:url[=\]][^\]]*?|/url|b|/b|i|/i|s|/s|u|/u|'
    r'quote|/quote|img|/img|color[=\]][^\]]*?|/color|'
    r'size[=\]][^\]]*?|/size|align[=\]][^\]]*?|/align|'
    r'list|/list|\\*|/\\*|code|/code|pre|/pre)\]',
    re.IGNORECASE,
)


def _clean_text(text: str) -> str:
    """清洗文本：BBcode 剔除 + HTML unescape + 空白规范化。

    适用场景：enricher 输出的 chunk_text、ingestion 的 embed_text 构建。

    Args:
        text: 原始文本（可能含 BBcode、HTML 实体、零宽字符等）。

    Returns:
        清洗后的纯文本。
    """
    if not text:
        return ""

    # 1. 剔除 BBcode 标签
    text = _BBCODE_RE.sub("", text)

    # 2. HTML unescape
    text = html.unescape(text)

    # 3. 去首尾引号和空白
    text = text.strip().strip('"').strip("'")

    # 4. 全角空格 → 半角
    text = text.replace("　", " ")

    # 5. 移除零宽字符
    text = re.sub(r"[​‌‍‎‏﻿]", "", text)

    # 6. 统一换行 + 压缩连续空行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 7. 压缩连续空白
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\t+", " ", text)

    return text.strip()


def _first_sentence(text: str, max_chars: int = 120) -> str:
    """取文本第一句（截断到 max_chars），用于 embed_text 末尾的语义补充。"""
    if not text:
        return ""
    # 在第一个句号/换行处截断
    for sep in ("。", "\n", "！", "？", "；"):
        idx = text.find(sep)
        if 10 < idx < max_chars:
            return text[:idx]
    return text[:max_chars]
