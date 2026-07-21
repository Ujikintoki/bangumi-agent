"""
Dialogue Agent 系统提示词

人格化模块架构 v3：DIALOGUE_CORE_PROMPT 已移除，内容迁移至：
- agent/profiles.py — 角色人格 + Agent 配置
- agent/prompt_builder.py — 统一 prompt 组装

本文件保留 build_dialogue_prompt() 作为薄封装，维持与 dialogue/nodes.py 的接口兼容。
"""

from __future__ import annotations

from agent.profiles import get_agent_profile, get_character
from agent.prompt_builder import build_system_prompt as _build


def build_dialogue_prompt(
    memory_context: str = "",
    output_style: str = "bangumi",
) -> str:
    """返回 Dialogue Agent 的完整 System Prompt。

    Dialogue Agent 不需要 intent 变体——所有意图共用同一个核心 prompt。
    人格通过 output_style 控制注入。

    实际组装由 agent.prompt_builder.build_system_prompt() 完成。

    Args:
        memory_context: L2 语义召回 + tone 提示的格式化文本。
        output_style: 输出风格（"neutral" | "bangumi"）。默认 "bangumi"。

    Returns:
        完整 System Prompt 字符串。
    """
    agent = get_agent_profile("dialogue")
    character = get_character(output_style)
    return _build(
        agent_profile=agent,
        character=character,
        memory_context=memory_context,
    )
