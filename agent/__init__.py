"""Agent 编排模块 — LangGraph 状态管理与图谱编排。

Phase 6: 统一 Companion Agent。单一 graph + state，depth 参数控制模式。

共用层（agent/ 根）:
  - state.py: 统一 AgentState（含 depth 字段）
  - graph.py: 统一 StateGraph（depth 条件启用 Critic）
  - nodes.py: reasoning_node + critic_node
  - profiles.py: CharacterProfile + AgentProfile
  - prompt_builder.py: 统一 prompt 组装（8 层）
  - prompts.py: Companion 浅层 intent 策略
  - llm.py: create_llm() 多 Provider 工厂
  - memory.py: tiktoken 滑动窗口截断
  - classifier.py: LLM 单阶段意图分类

Research Skill（agent/research/）:
  - prompts.py: 深度 INTENT_PROMPTS + CRITIC_SYSTEM_PROMPT
"""

from agent.graph import agent_app, build_graph
from agent.state import AgentState

__all__ = ["AgentState", "agent_app", "build_graph"]
