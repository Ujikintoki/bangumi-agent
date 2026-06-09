"""Dialogue Agent — 快速对话助手（Bangumi娘人格）。

2 节点 ReAct 拓扑：reasoning → (条件) tool/END，无 Critic。
速度 > 准确，回复 30-150 字，<2s 延迟。

共用 agent/ 根层: llm.py, memory.py, classifier.py
共用工具层: tools/bgm_tools.py

模块结构:
    - state.py:   DialogueState（5 字段，_MAX_ITERATIONS=3）
    - prompts.py: Bangumi娘 System Prompt（腹黑萝莉人格）
    - nodes.py:   dialogue_reasoning_node（极简推理）
    - graph.py:   build_dialogue_graph() + dialogue_app 实例
"""
