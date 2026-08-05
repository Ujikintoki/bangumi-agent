# 架构状态 & 路线图

> 最后更新: 2026-08-05

## 当前状态

| 指标 | 值 |
|------|-----|
| 版本 | v0.2.0-beta |
| Graph 节点 | 8（classify + fetch_search + fetch_detail + realtime_search + profile_search + synthesize + reasoning + tool） |
| 拓扑 | 异质：Pipeline（fetch/realtime/profile）+ ReAct（explore/discuss/fallback）+ chat 直通 |
| 工具 | 16 个（13 无条件 + 3 需 BANGUMI_ACCESS_TOKEN），隐式终止 |
| 记忆 | L1 滑动窗口 + 工具压缩（fast 10000 / deep 16000 tok）+ L2 语义召回（pgvector + 时间衰减） |
| 人格 | 4 角色（bangumi / bangumi_cold / bangumi_cute / neutral）+ 5 档离散参数 + Render 层 |
| 测试 | 534 函数 / 22 文件 |

### 各层健康度

| 层 | 状态 | 待解决问题 |
|---|------|-----------|
| **编排层** | 🟡 稳定（Phase 4.1） | 字数控制、deep 0 工具调用、常识误分类、streaming |
| **人格层** | 🟢 稳定 | cold/cute 措辞微调 |
| **记忆层** | 🟢 稳定 | 长程多轮上下文不足、空字符串缓存 bug |
| **数据层** | 🟢 稳定 | RAG v0/v1 共存、HNSW 索引维度限制 |

---

## 已完成

### Phase 4 — 异质拓扑（v0.2.0-beta）
- classify_node 按 intent 分发到 pipeline 或 ReAct 路径
- Pipeline intents（fetch/realtime/profile）：确定性步骤，独立节点 + prompt + 工具集
- ReAct intents（explore/discuss/fallback）：LLM 自主探索
- Chat intent：直通 END，无工具调用
- 置信度路由（<0.7 → ReAct fallback）

### Phase 4.1 — 隐式终止
- 移除 submit_facts 工具，LLM 输出文本（无 tool_calls）= END
- main.py 统一渲染路径，render 失败降级清理
- Pipeline 硬熔断 + 空搜索检测 + 重复调用检测

### Phase 3 — 图谱重构
- classify_node + reasoning_node 瘦身
- 异质拓扑基础设施

### Phase 2 — 分类器重写
- function calling 意图分类 + 置信度路由
- 7 intent（chat / fetch / explore / discuss / realtime / profile / fallback）

### Phase 1 — 数据层
- AgentState TypedDict + 工具子集 + tool_choice 策略

### Phase 5 — 记忆系统
- L1 滑动窗口 + 工具压缩 + SystemMessage 保护
- L2 双通道语义召回 + 时间衰减
- Session 缓存

> 详细演化历史见 [`docs/design/architecture-evolution.md`](architecture-evolution.md)。

---

## 待解决

### P0 — 产品级 Bug

| # | 问题 | 定位 | 改动量 |
|---|------|------|--------|
| 1 | 字数控制形同虚设——fast 模式近半数回复超过限制 | `persona/render.py` `_WORD_LIMIT` | prompt 强化或硬截断 |
| 2 | Deep 模式 ReAct intent 偶发 0 工具调用 | `orchestrate/prompt_builder.py` | prompt 策略调整 |
| 3 | "今天星期几"等常识问题误分类为 realtime | `orchestrate/classifier.py` | 加纯常识判断 |

### P1 — 影响体验

| # | 问题 | 定位 | 改动量 |
|---|------|------|--------|
| 4 | 长程多轮（8 轮+）话题跳转后失忆 | `memory/short_term.py` | fast 预算策略调整 |
| 5 | Streaming 仅节点级（非逐 token） | `main.py` `/chat/stream` | 升级到 astream_events |

### P2 — 技术债

| # | 问题 | 定位 | 改动量 |
|---|------|------|--------|
| 6 | `_memory_context` 空字符串缓存 bug（`""` 是 falsy） | `memory/cache.py` | ~5 行 |
| 7 | 记忆阈值命名过时（`MEMORY_DIALOGUE_*`） | `core/config.py` | 重命名 |
| 8 | Bare title 不追问确认 | `orchestrate/strategies.py` | 追问策略 |
| 9 | `create_llm()` 无缓存 | `agent/llm.py` | 加 lru_cache |
| 10 | RAG v0/v1 共存 | `rag/` | 大 |
| 11 | HNSW index 创建失败（2048d > pgvector 上限 2000d） | `database/` | 中等 |

---

## 计划

### 短期（v0.2）

- 修复 P0 三项 + P1 streaming
- 字数控制硬截断
- classifier 常识判断增强

### 中期

- 逐 token streaming
- 长程多轮记忆优化
- cold/cute 人格措辞调优
- 配置清理（命名统一、废弃项删除）

### 长期

- 小组讨论抓取、网页搜索、文本润色等新工具
- 按 intent 自适应人格参数
- 事件驱动主动推送

---

## 设计文档索引

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](../../CLAUDE.md) | 项目架构、四层详解、调参速查、编码规范 |
| [`architecture-evolution.md`](architecture-evolution.md) | 架构演化历史（Phase 1–10） |
| [`architecture-review-2026-07-22.md`](architecture-review-2026-07-22.md) | 宏观架构 review（Phase 6 重构依据） |
| [`phase1-3-audit.md`](phase1-3-audit.md) | Phase 1-3 兼容性审查 |
| [`phase5-memory-system-design.md`](phase5-memory-system-design.md) | Phase 5 记忆系统设计 |
| [`data-layer-redesign-discussion.md`](data-layer-redesign-discussion.md) | 工具层 str→dict 迁移决策 |
| [`bangumi-api-schema-methodology.md`](bangumi-api-schema-methodology.md) | A/B/C/D 字段方法论 |
| [`claude-on-bangumi-vision.md`](claude-on-bangumi-vision.md) | 产品愿景 |
| [`evolution-roadmap-phase7-9.md`](evolution-roadmap-phase7-9.md) | Phase 7-9 分层演进路线 |
