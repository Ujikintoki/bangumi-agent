"""
RAG 文本预处理模块

提供面向 Bangumi 番剧文本（简介、长评等）的数据清洗与滑动窗口切分能力。
不依赖 LangChain / LlamaIndex，基于原生 Python 列表运算 + tiktoken 实现。
"""

from typing import List

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

        Example:
            >>> processor = BangumiTextProcessor()
            >>> processor.clean_text('  "hello　　world\\n\\n\\nfoo"  ')
            'hello world\\nfoo'
        """
        if not text:
            return ""

        # 1. 移除首尾无意义引号
        text = text.strip().strip('"').strip("'")

        # 2. 全角空格 → 半角空格
        text = text.replace("\u3000", " ")

        # 3. 统一换行符：\r\n → \n，连续换行（\n{2,}）→ 单个换行
        import re

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
