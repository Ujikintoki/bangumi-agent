# Phase 3 启动上下文 — 新对话 Handoff

> 从主 session 移交。主 session 完成了 Phase 1-2（Client 统一、12 Tool、Sanitizer、RAG、274 tests）。

---

## 新对话要做什么

1. **深入学习 LangGraph ReAct Agent 开发**
2. **参考其他开源 AI Agent 项目的设计模式**
3. **为本项目设计 Phase 3 的系统提示词和 Agent 架构**

不是写代码，是设计阶段。可参考的项目和资料由你在对话中自行引入。

---

## 项目当前快照

### 已就位的基础设施

```
12 个 @tool 函数 (tools/bgm_tools.py)
  9 个无条件 + 3 个 Token 门控
  → 搜索/详情/日历/趋势/评论/讨论/角色声优/用户画像/日志/时光机/RAG

BangumiClient (clients/client.py)
  10 个 p1 API 业务方法

Sanitizer (clients/sanitizers.py)
  BBCode 剥离、噪音过滤、评论排序、白名单提取

RAG (rag/)
  RagEntity 单表多态、hybrid_search、E2E 测试通过

274 tests (test/)
  test_schemas / sanitizers / client / tools / rag
```

### Agent 当前状态（需要被替换的占位代码）

```
agent/state.py    — AgentState TypedDict (messages: list[str], iterations, critic_status, needs_tool, error_flag)
agent/nodes.py    — reasoning_node (关键词匹配 _detect_tool_intent) / tool_node (空占位) / critic_node (iterations < 2)
agent/graph.py    — 图谱拓扑: START → reasoning → tool/critic → END/retry (拓扑正确，节点是桩)
main.py           — 只有 /health，没有 /chat
```

### 架构文档

```
docs/phase3/00_OVERVIEW.md      — Phase 3 总览 + 7 步开发顺序
docs/phase3/01_STATE.md          — AgentState 消息类型升级 (str → BaseMessage)
docs/phase3/02_REASONING.md      — reasoning_node LLM function-calling
docs/phase3/03_TOOL_EXECUTION.md — ToolNode 替换
docs/phase3/04_CRITIC.md         — critic_node (LLM 评估 + 规则备选)
docs/phase3/05_SYSTEM_PROMPT.md  — 系统提示词 + 三层路由策略（初版）
docs/phase3/06_MEMORY.md         — 短期记忆管理
docs/phase3/07_ENDPOINT.md       — /chat 端点 + 流式输出
docs/phase3/08_TESTING.md        — 测试策略
```

---

## 开放的 Prompt 设计问题

这些是主 session 尚未定案、留给新对话讨论的：

1. **系统提示词的具体措辞** — 文档里有一个初版，但需要打磨（工具选择策略、回答风格、边界情况）
2. **Critic 提示词** — PASS/REVISE 的判定标准写多细？
3. **是否需要多个人格** — 专业助手 vs 轻度吐槽 vs 详细分析？
4. **温度参数** — 工具调用需要 0.3，但最终回复可能需要更高的创造性
5. **RAG 结果如何嵌入回复** — 是让 LLM 自由引用，还是规定"必须引用搜索结果"？
6. **用户画像/偏好记忆的 prompt 注入** — 长期记忆如何在系统提示词中体现？

---

## 外部参考资料

主 session 中讨论过的：

- **AI.js** (`test/AI.js`) — Bangumi 社区油猴脚本。Prompt 设计值得参考：结构化人格分层 (Role→Skills→Goals→Constrains→OutputFormat)、"你正在和用户一起浏览"的语境注入、反面约束与正面引导配合。但它的场景是"单次页面评论"，而你的 Agent 是多轮工具调用。
- **Bangumi OpenAPI** (`docs/bangumi_openapi_p_short.yaml`) — p1 private API 完整 spec

---

## 关键约束

| 约束 | 说明 |
|---|---|
| LLM 兼容 | OpenAI SDK 兼容 (GPT-4o / DeepSeek / Qwen) |
| 工具格式 | LangChain `@tool(args_schema=...)` 已全部就位 |
| 图谱不重建 | `agent/graph.py` 拓扑正确，只改节点内容 |
| 温度 | 工具调用阶段需低温度 (~0.3)，避免编造参数 |
| Token 预算 | 工具返回经 Sanitizer 精简，RAG 按热度排序截断 |
