"""Agent 编排模块 — LangGraph 状态管理与图谱编排。

共用层（agent/ 根）:
  - llm.py: create_llm() 多 Provider 工厂
  - memory.py: tiktoken 滑动窗口截断
  - classifier.py: 两阶段意图分类（规则优先 + LLM fallback）

Research Agent（agent/research/）:
  深度研究助手 — 3 节点 ReAct + Critic 质量自省。准确 > 速度。

Dialogue Agent（agent/dialogue/）:
  快速对话助手 — 无 Critic，速度 > 准确。回复 ~100 字节。
"""

from agent.research.graph import agent_app, build_graph
from agent.research.state import AgentState

__all__ = ["AgentState", "agent_app", "build_graph"]
