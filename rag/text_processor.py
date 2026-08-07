'''
[DEPRECATED — 2026-08-05] 整个模块已废弃。

原因:
  - Bangumi summary 大多数 < 300 tokens，无需滑动窗口 chunking
  - 语义前缀逻辑在 ingestion.py 的 _build_*_chunk_text() 中实现
  - create_entity_documents 的 Parent-Child Retriever 模式未接入生产管线
  - clean_text() 已迁移至 ingestion.py 的 _clean_text()

保留此文件供参考，实际 chunking 和前缀逻辑见 rag/ingestion.py。

原文件内容:
---
RAG 文本预处理模块

提供面向 Bangumi 番剧文本（简介、长评等）的数据清洗与滑动窗口切分能力。
不依赖 LangChain / LlamaIndex，基于原生 Python 列表运算 + tiktoken 实现。
---

import html
import re
from typing import Any, List

import tiktoken


class BangumiTextProcessor:
    """Bangumi 文本处理器。

    负责将原始番剧文本清洗后，按 Token 维度切分为语义连贯的文本块，
    每个块在 chunk_size 与 chunk_overlap 控制下保持上下文重叠。

    Attributes:
        tokenizer: tiktoken 编码器实例（cl100k_base）。
        chunk_size: 每个文本块的 Token 上限。
        chunk_overlap: 相邻文本块之间的 Token 重叠量。
    """

    def __init__(
        self,
        chunk_size: int = 300,
        chunk_overlap: int = 50,
    ) -> None:
        """初始化文本处理器。

        Args:
            chunk_size: 每个文本块的 Token 上限，默认 300。
            chunk_overlap: 相邻块之间的重叠 Token 数，默认 50。
                必须严格小于 chunk_size，否则滑动窗口步长会为零或负。
        """
        if chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap ({chunk_overlap}) 必须小于 chunk_size ({chunk_size})"
            )

        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def clean_text(self, text: str) -> str:
        """清洗原始文本，移除噪音并规范化空白字符。

        按以下顺序执行清洗：
        1. 移除首尾无意义引号（单/双引号）。
        2. 全角空格 → 半角空格。
        3. 连续换行 → 单个换行。
        4. 连续空格 → 单个空格。

        Args:
            text: 原始文本字符串。

        Returns:
            清洗后的规范文本。若输入为空字符串，返回空字符串。
        """
        if not text:
            return ""

        text = html.unescape(text)

        # 1. 移除首尾无意义引号
        text = text.strip().strip('"').strip("'")

        # 2. 全角空格 → 半角空格
        text = text.replace("　", " ")

        # 3. 统一换行符：\r\n → \n，连续换行（\n{2,}）→ 单个换行
        # 移除不可见的零宽字符 / 控制字符
        text = re.sub(r"[​‌‍‎‏﻿]", "", text)

        text = text.replace("\r\n", "\n")
        text = re.sub(r"\n{2,}", "\n", text)

        # 4. 连续空格 → 单个空格
        text = re.sub(r" {2,}", " ", text)

        return text

    def split_text(self, text: str | None) -> List[str]:
        """使用滑动窗口将文本切分为语义块..."""
        if not text:
            return []

        cleaned = self.clean_text(text)
        if not cleaned:
            return []

        tokens = self.tokenizer.encode(cleaned)
        total_tokens = len(tokens)

        if total_tokens <= self.chunk_size:
            return [cleaned]

        step = self.chunk_size - self.chunk_overlap
        chunks: List[str] = []

        for start in range(0, total_tokens, step):
            end = min(start + self.chunk_size, total_tokens)
            chunk_tokens = tokens[start:end]
            chunk_text = self.tokenizer.decode(chunk_tokens)
            chunk_text = chunk_text.strip("�")
            if chunk_text.strip():
                chunks.append(chunk_text)

        return chunks

    def create_entity_documents(self, entity_type, entity_id, name="", name_cn="", summary=None, tags=None, subject_name=""):
        """为任意类型实体创建父子文档结构..."""
        cleaned_summary = self.clean_text(summary or "")
        parent_parts: list[str] = []

        if entity_type == "subject":
            prefix = f"[作品名] {name_cn}。" if name_cn else "[作品名] "
            if tags:
                tags_str = ", ".join(tags)
                parent_parts.append(f"标签: {tags_str}")
        elif entity_type == "character":
            prefix = f"[角色] {name_cn}" if name_cn else "[角色]"
            if subject_name:
                prefix += f"，出自《{subject_name}》"
            prefix += "。"
        elif entity_type == "person":
            prefix = f"[人物] {name_cn}。" if name_cn else "[人物] "
        else:
            prefix = ""

        if cleaned_summary:
            parent_parts.append(f"{prefix}{cleaned_summary}")
        elif prefix:
            parent_parts.append(prefix.strip("。"))

        parent_text = "\n".join(parent_parts)
        parent = {"entity_id": entity_id, "entity_type": entity_type, "text": parent_text, "meta_info": {"chunk_type": "parent", "entity_type": entity_type, "tags": tags or []}}
        children: list[dict[str, Any]] = []
        if cleaned_summary:
            child_chunks = self.split_text(cleaned_summary)
            for chunk_text in child_chunks:
                children.append({"entity_id": entity_id, "entity_type": entity_type, "text": chunk_text, "meta_info": {"chunk_type": "child", "entity_type": entity_type, "parent_entity_id": entity_id}})

        return {"parent": parent, "children": children}

    def create_parent_child_documents(self, subject_id, tags, summary):
        """[DEPRECATED] 已废弃，请使用 create_entity_documents..."""
        return self.create_entity_documents(entity_type="subject", entity_id=subject_id, summary=summary, tags=tags)
'''
