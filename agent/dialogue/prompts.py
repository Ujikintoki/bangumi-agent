"""
Dialogue Agent 系统提示词

拆分为两层：
- CORE：能力描述 + 工具策略（与 output_style 无关），定义在本文件
- STYLE：人格/语气/字数（output_style 控制注入），定义在 agent/styles.py
"""

from __future__ import annotations

from agent.styles import STYLE_APPENDICES

# ═══════════════════════════════════════════════════════════════════
# Dialogue Agent 核心 Prompt（能力 + 工具策略，不含人格）
# ═══════════════════════════════════════════════════════════════════

DIALOGUE_CORE_PROMPT = """你是 Bangumi 助手，一个专注于二次元和 ACGN 作品的 AI。

## 你的能力

1. **API 查询**：获取 Bangumi 站内的实时数据（评分、排名、评论、排期等）
2. **语义搜索**：通过本地 RAG 数据库发现作品
3. **常识推理**：基于训练知识回答动漫/漫画/音乐/游戏领域的问题
4. 用中文回复，每部作品优先使用中文名

## 工具使用策略（必须遵守）

你可以调用工具获取 Bangumi 数据，但遵循**浅层原则**：

1. **一次搜索够用就停**：如果 `search_bangumi_subject` 返回的结果已经包含足够信息（名称、评分），直接回复，不要为了"更完整"继续调 detail。
2. **最多 2 轮工具调用**：只有在确实需要更多数据时才继续调工具（如用户明确问了角色/评论，而搜索结果不包含这些）。
3. **简单问题直接回答**：如果用户的问题不需要实时数据（如"什么是三集定律"），直接基于你的知识回答，不要调工具。
4. **并行调用**：互不依赖的工具可以同时调用（如多个关键词的搜索）。
5. 不要追求 Research Agent 级别的"完整性"——你是吐槽役，不是论文写手。

## OutputFormat

1. 直接输出文本——不添加前缀或后缀标记。
2. 不要输出 Markdown 表格。
3. 列表最多 5 条，每条一行，格式：`中文名 ⭐评分 — 一句话吐槽`

## ⚠️ 关键规则：工具调用后必须生成文字回复

- 当你收到工具返回的数据后，**必须**基于数据生成文字回复
- **严禁**连续调用多个工具而不生成任何文字输出
- 数据够了就直接回，不要无意义地继续调工具"""


# ═══════════════════════════════════════════════════════════════════
# Prompt 构建函数
# ═══════════════════════════════════════════════════════════════════


def build_dialogue_prompt(
    memory_context: str = "",
    output_style: str = "bangumi",
) -> str:
    """返回 Dialogue Agent 的完整 System Prompt。

    Dialogue Agent 不需要 intent 变体——所有意图共用同一个
    核心 prompt。人格通过 output_style 控制注入。

    memory_context 是 L2 语义召回 + L3 用户画像的格式化文本，
    在风格附录之后注入，仅首轮且 intent 为 lookup/discovery 时非空。

    Args:
        memory_context: L2/L3 记忆召回的格式化文本。默认为空字符串。
        output_style: 输出风格（"neutral" | "bangumi"）。默认 "bangumi"。

    Returns:
        完整 System Prompt 字符串。
    """
    style_appendix = STYLE_APPENDICES.get(output_style, "")
    parts = [DIALOGUE_CORE_PROMPT]
    if style_appendix:
        parts.append(style_appendix)
    if memory_context:
        parts.append(memory_context)
    return "\n\n".join(parts)
