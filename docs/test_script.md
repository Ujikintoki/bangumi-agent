# test 解耦文件
```text
  test/
  ├── conftest.py          ← 共享 fixtures（make_state, make_mock_llm, MOCK_TOOLS）
  ├── test_state.py        ← 13 tests  — AgentState + 路由 + extract_user_input
  ├── test_classifier.py   ← 34 tests  — 规则层 + LLM fallback + 两阶段入口
  ├── test_llm.py          ←  5 tests  — LLM 工厂 + API Key 解析
  ├── test_prompts.py      ←  8 tests  — 系统提示词 + Critic prompt
  ├── test_reasoning.py    ← 10 tests  — reasoning_node（mock LLM）
  ├── test_tool_node.py    ←  5 tests  — ToolNode 执行 + 生命周期
  ├── test_critic.py       ← 13 tests  — 规则版 + LLM 版 Critic
  ├── test_critic.py       ← 13 tests  — 规则版 + LLM 版 Critic
  ├── test_graph.py        ←  4 tests  — 图谱集成
  └── test_tools.py (etc)  ← 原有文件不动
```
# agent test 文件
```text
  独立运行
  ├── test_prompts.py      ←  8 tests  — 系统提示词 + Critic prompt
  ├── test_reasoning.py    ← 10 tests  — reasoning_node（mock LLM）
  ├── test_tool_node.py    ←  5 tests  — ToolNode 执行 + 生命周期
  ├── test_critic.py       ← 13 tests  — 规则版 + LLM 版 Critic
  ├── test_graph.py        ←  4 tests  — 图谱集成
  └── test_tools.py (etc)  ← 原有文件不动
```

# 全部测试
```bash
  python -m pytest test/ -q
```
# 单个模块
```bash
  python -m pytest test/test_classifier.py -v
  python -m pytest test/test_classifier.py -v
  python -m pytest test/test_critic.py -v
```
# 单个测试类
```bash
  python -m pytest test/test_classifier.py::TestIntentClassifierRule -v
  python -m pytest test/test_critic.py::TestCriticNodeLLM::test_escape_hatch -v
```
##  跳过需要数据库的
```bash
  python -m pytest test/ --ignore=test/test_rag.py
```
