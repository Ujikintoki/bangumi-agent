# Phase 3: Agent 接入真实 LLM — 总览

> 目标：将硬编码占位 Agent 替换为真正的 LLM 驱动的 ReAct Agent，并加入意图分类、定向 Critic 反馈、三层记忆架构

---

## 当前状态 vs 目标

| 组件 | 当前（占位） | 目标（真实） |
|---|---|---|
| `reasoning_node` | `_detect_tool_intent()` 关键词匹配 | 意图分类 + LLM function-calling 决策 |
| `tool_node` | `"[Tool] Tool execution successful."` | 执行 `get_agent_tools()` 返回的工具（LangGraph `ToolNode`） |
| `critic_node` | `iterations < 2 → REVISE` | LLM 三元维度评估（完整性/具体性/工具利用），输出定向反馈 |
| `AgentState.messages` | `list[str]` | `list[BaseMessage]`（LangChain 消息类型） |
| 意图分类 | 无 | 两阶段分类器（规则 + LLM fallback），输出 `query_intent` |
| Critic 反馈 | 无（二元 PASS/REVISE） | `critic_feedback: str`，REVISE 时附带具体改进建议 |
| 记忆管理 | 无 | 三层架构：滑动窗口（实现）+ 会话记忆 + 用户画像（预留） |
| `main.py` | 只有 `/health` | 新增 `POST /chat` + `POST /chat/stream` |

## 新增架构：意图分类层

在 reasoning_node 内部增加预处理步骤，不改变图拓扑：

```
用户查询
    │
    ▼
意图分类器（reasoning_node 第一步）
    │
    ├─ chitchat / factual → LLM 直接回复，强制不调工具
    ├─ lookup / realtime  → 标准 ReAct（LLM function-calling）
    ├─ discovery          → RAG 优先，API 补充
    └─ unknown            → 标准 ReAct（LLM 自行判断）
```

分类器采用两阶段设计：规则匹配覆盖 80% 常见查询（零延迟），LLM fallback 处理模糊边界。

## 开发顺序

按依赖关系排列，每步可独立验证：

```
1. AgentState 消息类型升级 (str → LangChain BaseMessage)
       │
2. reasoning_node: 意图分类 + LLM function-calling
       │
3. tool_node 接入真实工具 (ToolNode + 12 tools)
       │
4. critic_node: 三元维度评估 + 定向反馈
       │
5. 系统提示词: 意图特定 prompt 变体 + 工具依赖约束
       │
6. 短期记忆管理: 滑动窗口 + Token 预算 (Layer 1)
       │   会话记忆 + 用户画像 (Layer 2/3 预留接口)
       │
7. POST /chat 端点 + 流式输出 + 错误处理
       │
8. 测试: 节点单元 → 图谱集成 → 端到端
```

## 文件索引

```
docs/Agent/
├── 00_OVERVIEW.md           ← 本文件
├── 01_STATE.md              ← AgentState 升级（含新字段 + last_tool_calls 生命周期约束）
├── 02_REASONING.md          ← 优先级意图分类 + LLM function-calling
├── 03_TOOL_EXECUTION.md     ← tool_node: 真实工具执行 + 并行边界 + last_tool_calls 安全约束
├── 04_CRITIC.md             ← 定向反馈: 三元评估 + 逃逸舱 + critic_feedback
├── 05_SYSTEM_PROMPT.md      ← 意图特定 prompt 变体 + 路由策略
├── 06_MEMORY.md             ← 三层记忆架构 + tiktoken 精确计数
├── 07_ENDPOINT.md           ← /chat 端点 + 流式输出
└── 08_TESTING.md            ← 测试策略
```

## 关键架构决策（已确定）

| 决策 | 结论 |
|---|---|
| Agent 框架 | LangGraph StateGraph（`tool → reasoning` 固定边，消化态解绑工具） |
| LLM 接入方式 | OpenAI SDK 兼容（支持 OpenAI / DeepSeek / Qwen） |
| 工具调用 | LangGraph `ToolNode` + 本项目 `get_agent_tools()` |
| 意图分类 | 两阶段：优先级规则匹配（复合意图先于简单意图）+ LLM fallback |
| 自省策略 | LLM 三元维度评估 + 定向反馈 + 逃逸舱（数据缺失时强制 PASS） |
| Critic 版本策略 | 先用规则版验证流程，再切换到 LLM 版——两版接口相同 |
| 迭代上限 | 10 轮（`_MAX_ITERATIONS = 10`） |
| 消息类型 | LangChain `BaseMessage`（HumanMessage, AIMessage, ToolMessage） |
| 记忆架构 | Layer 1 滑动窗口（tiktoken 精确计数）；Layer 2/3 会话 + 画像（预留接口） |
| 工具依赖约束 | Prompt 中明确：需要前序结果的工具不能并行调用 |
| State 生命周期 | `last_tool_calls` 仅 reasoning_node 写入；tool_node/critic_node 禁止触碰 |

## 不做的事（明确排除）

- MCP server/tool 集成
- Plan-and-Execute 模式
- 多 Agent 投票/对抗验证
- Token 级流式输出（Phase 3 只做节点级 SSE）
- 工具返回结果 LLM 摘要（标注为 Phase 4+ 优化项）
