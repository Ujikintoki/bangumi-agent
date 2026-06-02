"""
RAG 文本预处理模块

提供面向 Bangumi 番剧文本（简介、长评等）的数据清洗与滑动窗口切分能力。
不依赖 LangChain / LlamaIndex，基于原生 Python 列表运算 + tiktoken 实现。
"""

import html
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
        5. （TODO）未来根据实际情况待添加

        Args:
            text: 原始文本字符串。

        Returns:
            清洗后的规范文本。若输入为空字符串，返回空字符串。

        Example:
            >>> processor = BangumiTextProcessor()
            >>> processor.clean_text('  "hello　　world\\n\\n\\nfoo"  ')
            'hello world\\nfoo'
        """
        if not text:
            return ""

        text = html.unescape(text)

        # 1. 移除首尾无意义引号
        text = text.strip().strip('"').strip("'")

        # 2. 全角空格 → 半角空格
        text = text.replace("\u3000", " ")

        # 3. 统一换行符：\r\n → \n，连续换行（\n{2,}）→ 单个换行
        import re

        # 2. 移除不可见的零宽字符 / 控制字符
        text = re.sub(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]", "", text)

        text = text.replace("\r\n", "\n")
        text = re.sub(r"\n{2,}", "\n", text)

        # 4. 连续空格 → 单个空格
        text = re.sub(r" {2,}", " ", text)

        # TODO: 未来在这里接入 BBCode to Markdown 转换器

        return text

    def split_text(self, text: str | None) -> List[str]:
        """使用滑动窗口将文本切分为语义块。

        先清洗文本，再编码为 Token 序列，按 chunk_size 截取窗口，
        步长为 chunk_size - chunk_overlap，保证相邻块之间有重叠。

        Args:
            text: 待切分的原始文本。若为 None 或空字符串，返回空列表。

        Returns:
            切分后的文本块列表。若原始 Token 数不超过 chunk_size，
            返回包含完整文本的单元素列表。

        Example:
            >>> processor = BangumiTextProcessor(chunk_size=100, chunk_overlap=20)
            >>> chunks = processor.split_text("这是一段很长的文本...")
            >>> len(chunks) > 0
            True
        """
        if not text:
            return []

        cleaned = self.clean_text(text)
        if not cleaned:
            return []

        # 编码为 Token 整数序列
        tokens = self.tokenizer.encode(cleaned)
        total_tokens = len(tokens)

        # 文本较短，无需切分
        if total_tokens <= self.chunk_size:
            return [cleaned]

        # 滑动窗口切分
        step = self.chunk_size - self.chunk_overlap
        chunks: List[str] = []

        for start in range(0, total_tokens, step):
            end = min(start + self.chunk_size, total_tokens)
            chunk_tokens = tokens[start:end]
            chunk_text = self.tokenizer.decode(chunk_tokens)

            # 清除 BPE 切分导致的边界乱码（\ufffd 是 UTF-8 替换字符）
            chunk_text = chunk_text.strip("\ufffd")

            # 丢弃解码后为空的块（边界情况）
            if chunk_text.strip():
                chunks.append(chunk_text)

        return chunks

    def create_entity_documents(
        self,
        entity_type: str,
        entity_id: int,
        name: str = "",
        name_cn: str = "",
        summary: str | None = None,
        tags: list[str] | None = None,
        subject_name: str = "",
    ) -> dict[str, Any]:
        """为任意类型实体创建父子文档结构（多态版）。

        遵循 Parent-Child Retriever 模式。父文档根据实体类型拼接不同语义前缀，
        子文档为清洗后摘要的滑动窗口切片。

        Args:
            entity_type: 实体类型，``"subject"`` / ``"character"`` / ``"person"``。
            entity_id: Bangumi 原始数字 ID。
            name: 实体原文名称。
            name_cn: 实体中文名称。
            summary: 实体简介文本。若为空，仅生成父文档。
            tags: 标签列表（仅 subject 有效）。
            subject_name: 角色所属作品名（仅 character 有效）。

        Returns:
            父子文档字典，与 ``create_parent_child_documents`` 格式兼容。
        """
        cleaned_summary = self.clean_text(summary or "")

        # ── 根据实体类型组装父文档前缀 ────────────────────────
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

        parent: dict[str, Any] = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "text": parent_text,
            "meta_info": {
                "chunk_type": "parent",
                "entity_type": entity_type,
                "tags": tags or [],
            },
        }

        # ── 切分子文档 ────────────────────────────────────────
        children: list[dict[str, Any]] = []
        if cleaned_summary:
            child_chunks = self.split_text(cleaned_summary)
            for chunk_text in child_chunks:
                children.append(
                    {
                        "entity_id": entity_id,
                        "entity_type": entity_type,
                        "text": chunk_text,
                        "meta_info": {
                            "chunk_type": "child",
                            "entity_type": entity_type,
                            "parent_entity_id": entity_id,
                        },
                    }
                )

        return {"parent": parent, "children": children}

    def create_parent_child_documents(
        self,
        subject_id: int,
        tags: list[str],
        summary: str | None,
    ) -> dict[str, Any]:
        """[DEPRECATED] 为单个番剧条目创建父子文档结构。

        .. deprecated::
            请迁移至 ``create_entity_documents``，支持多态实体类型。

        遵循 Parent-Child Retriever 模式：父文档携带完整的标签与简介
        富文本上下文，子文档为清洗后摘要的滑动窗口切片，用于高精度
        语义检索。检索命中子文档后，可通过 ``parent_subject_id`` 回溯
        父文档获取完整信息。

        Args:
            subject_id: Bangumi 条目 ID，用于关联父子文档。
            tags: 条目标签列表，如 ``["百合", "科幻", "2023"]``。
                若为空列表，父文档中不渲染标签行。
            summary: 条目原始简介文本。若为 ``None`` 或空字符串，
                父文档仅包含标签信息，子文档列表为空。

        Returns:
            包含父子文档的字典。
        """
        return self.create_entity_documents(
            entity_type="subject",
            entity_id=subject_id,
            summary=summary,
            tags=tags,
        )
