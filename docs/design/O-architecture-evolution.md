# 架构演化历史

> 从第一个 ReAct Agent 到当前四层架构的完整演化过程。

## 时间线

```
2026-05~06    06-09          07-21        06-17~07-22    07-25/26        07-27            07-27          07-27
Phase 1-3      Phase 4        Phase 5      Phase 5.5      Phase 6         Phase 6.5        Phase 8         Phase 9
地基            双 Agent       记忆          人格化          合并双 Agent     解耦风格          Context重构      人格深化
──■───────────■─────────────■────────────■──────────────■──────────────■───────────────■──────────────■────→
FastAPI        拆 Research   L1 滑动窗口   CharacterProfile 合并双 Agent    render_node      三级预算         Critic 屏蔽
BangumiClient  + Dialogue    L2 语义召回   AgentProfile      depth 参数      风格解耦          TOOL_GUIDANCE   5档离散人格
RAG + pgvector 引入 Critic   L3 废弃       角色优先          纯 ReAct 拓扑    极简 prompt      工具压缩         四种人格模式
第一个 ReAct    ← Tool Agent 错配开始 →                                                   SystemMsg免疫    Render重设计
```

## 关键转折

**Phase 4（2026-06-09）引入了双 Agent 架构**——Research Agent（深度研究 + Critic）和 Dialogue Agent（快速聊天）。这个决策的根因是：架构按 Tool Agent 心智模型设计（"深度链式"、"数据完整性优先"），但产品定位是 Companion Agent（"查数据是为了聊天"）。

此后四个 Phase 都在纠正这个错配：
- **Phase 6（2026-07-25/26）**：纠正拓扑错配——合并双 Agent 为单一 Companion Agent，`depth` 参数替代 `agent_type`，Critic 仅 deep 时路由
- **Phase 6.5（2026-07-27）**：纠正输出风格错配——新增 render_node，Agent 负责准确、Render 负责风格，主 prompt 移除数据解释指令
- **Phase 8（2026-07-27）**：纠正 Context 管理错配——L1 按 depth 三级预算、SystemMessage 永不截断、TOOL_GUIDANCE 五合一、工具结果压缩
- **Phase 9（2026-07-27）**：深化人格表达——Critic 屏蔽（纯 ReAct）、人格参数 5 档离散化、四种人格模式、Render 重设计

## Phase 详情

### Phase 1-3：地基（2026-05 ~ 06 初）

FastAPI + Bangumi API Client + pgvector + 第一个 ReAct Agent。假设是 Tool Agent——"用户问、Agent 查、报答案"。

### Phase 4：双 Agent（2026-06-09）

拆成 Research Agent（深度研究 + Critic）和 Dialogue Agent（快速聊天）。埋下了架构与产品定位错配的根因。

### Phase 5：记忆系统（2026-07-21）

L1 滑动窗口 + L2 语义召回（双通道 + 时间衰减）。L3 用户画像设计后废弃。

### Phase 5.5：人格化（2026-06-17 ~ 07-22）

CharacterProfile/AgentProfile dataclass + 角色优先 prompt 组装。人格层独立出来的起点。

### Phase 6：合并双 Agent（2026-07-25/26）

合并双 Agent 为单一 Companion Agent，`depth` 替代 `agent_type`，纯 ReAct 拓扑，`agent/dialogue/` 删除。

### Phase 6.5：解耦风格（2026-07-27）

新增 render_node。Agent 负责准确，Render 负责风格。主 prompt 移除数据解释指令（-22%），expression_guide 与 Render 职责分离。

### Phase 8：Context & Memory 重构（2026-07-27）

解决 deep 模式死亡螺旋和多轮隐式引用丢失。L1 按 depth 三级预算、SystemMessage 永不截断、TOOL_GUIDANCE 五合一、工具结果压缩、孤儿 ToolMessage 清理。

### Phase 9：人格系统深化（2026-07-27）

Critic 屏蔽（纯 ReAct）+ 人格参数 5 档离散 + 四种人格模式（bangumi/bangumi_cold/bangumi_cute/neutral）。Render 重设计：per-personality voice hints + 参数感知风格微调。确认 Character Card 和 Render 是两层管线。

### Phase 10：基础功补强（2026-07-28 ~ 至今）

HNSW 修复（embedding-3 → embedding-2，2048d → 1024d）+ Critic 代码清理 + 数据层架构文档 + 评测体系设计。

## 核心教训

**架构假设和产品定位必须一致。** Tool Agent 的架构 + Companion Agent 的定位 = 四个阶段的修正。当前四层架构中，编排层不再预设"查数据是为了交报告"，人格层用两层管线（Card + Render）独立表达。
