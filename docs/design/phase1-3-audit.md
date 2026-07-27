# Phase 1-3 地基审查：Companion Agent 兼容性

> 2026-07-26 | Phase 6 Companion Agent 架构重构的前置审查

## 审查范围

Phase 1-3 是项目的早期地基：API 客户端、RAG 检索、工具函数、数据库 ORM、配置系统、FastAPI 入口。审查目标：确认这些层在新 Companion Agent 架构下是否需要调整。

---

## ✅ 不需要改的（7 层，与 agent 拓扑完全解耦）

| 层 | Phase | 文件 | 原因 |
|-----|-------|------|------|
| 客户端 | 1 | `clients/client.py` | 纯 HTTP 客户端 + sanitizer。和 agent 拓扑完全解耦。无任何 `agent_type` 引用。 |
| Sanitizer | 2 | `clients/sanitizers.py` | 纯函数，A/B/C/D 字段方法论。和数据消费者无关。 |
| RAG | 2 | `rag/retriever.py`, `rag/text_processor.py`, `rag/ingestion.py` | 纯检索层。`hybrid_search()` 接口不变。 |
| 工具 | 2 | `tools/bgm_tools.py` | dict 返回格式在 Companion 模式下完全适用。详细讨论见 [主对话记录]。 |
| 数据库 ORM | 1 | `database/models.py`, `database/engine.py` | RagEntity 模型。和数据消费方式无关。 |
| 数据库记忆 | 5 | `database/memory_tables.py` | `SessionMemory` 表无 `agent_type` 列——不需要迁移。`public_memories` 表已创建但未使用（Phase 6 预留）。 |
| Schema | 2 | `schemas/tools_input.py`, `schemas/__init__.py` | Pydantic 工具输入 schema。工具不改则不碰。 |
| 意图分类 | 4 | `agent/orchestrate/classifier.py` | 8 种意图分类逻辑不变。可以加一个深度信号检测函数（独立于现有分类器）。 |
| 护栏 | 4 | `agent/orchestrate/guardrails.py` | 终端回复检测（逃逸舱）、XML 泄漏剥离、重复工具调用检测——Companion 仍然需要全部。 |
| 共享辅助 | 4 | `agent/orchestrate/helpers.py` | `classify_intent_step`、`recall_memory_step`、`build_message_list` 均与 agent 拓扑无关。 |
| 记忆 L1 | 5 | `agent/memory/short_term.py` | 滑动窗口 + tiktoken 截断。纯 Token 管理，和 agent 类型无关。 |
| 记忆 L2 | 5 | `agent/memory/long_term.py` | 语义召回 + 时间衰减。和 agent 拓扑无关。但 `recall_for_prompt()` 的 `max_tokens` 和 `recall_threshold` 参数由调用方传入——调用方需要传 Companion 适用的值（见下 config 部分）。 |
| Session 缓存 | 5 | `agent/memory/cache.py` | 跨 HTTP 请求缓存。和 agent 拓扑无关。 |

---

## ⚠️ `core/config.py` — 默认值错配 + 残留死代码

### 问题 1（严重）：`LLM_TEMPERATURE = 0.3`

```python
LLM_TEMPERATURE: float = 0.3
# 注释："工具调用场景建议 0.1-0.3"
```

**这是 Tool Agent 世界观的配置。** Companion 损友需要创造力、不可预测性、偶尔的惊喜——temperature 0.3 在主动压制 agent 的"人味"。工具调用的稳定性在 Companion 模式下不应该是最高优先级（deep 模式可以单独设置低 temperature）。

推荐值：Companion 模式 `0.7-0.9`。或者保留全局 `0.3` 给 deep 模式，Companion 在 `create_llm()` 时 override。

### 问题 2（中等）：双套记忆配置

```python
MEMORY_MAX_INJECT_TOKENS: int = 700       # 注释："Research 用 700，Dialogue 用 300"
MEMORY_DIALOGUE_RECALL_THRESHOLD: float = 0.35
MEMORY_RECALL_THRESHOLD: float = 0.5      # 注释："Research Agent 用"
MEMORY_DIALOGUE_MAX_INJECT_TOKENS: int = 300
```

Phase 6 只有一个 Agent。双套阈值是旧双 agent 架构的遗物。

- 700 tokens 注入对 Companion（回复 30-150 字）太多——~300 合适
- 建议合并为两套：`MEMORY_QUICK_*`（Companion 默认模式）和 `MEMORY_DEEP_*`（depth="deep" 模式）

### 问题 3（低）：Critic 相关配置

```python
LLM_CRITIC_MODEL: str = ""
CRITIC_MODE: str = "llm"
```

Critic 在 Companion 默认模式下不运行，但 **depth="deep" 时仍然需要**。不删除——但注释和默认值需要更新为"仅 depth="deep" 生效"。

### 问题 4（低）：L3 废弃配置残留

```python
MEMORY_MIN_SESSIONS_FOR_PROFILE: int = 5
# 注释："[L3 deprecated] 画像已移除，此项保留以备未来重新激活"
```

零消费者。可以删除或在 Phase 6 清理时一并处理。

---

## 🔴 `main.py` — Phase 6 Step 2 核心重构目标

### 完整问题清单（6 项）

| # | 行号 | 严重度 | 问题 |
|---|------|--------|------|
| 1 | 75-78, 191-193 | 🔴 关键 | `agent_type: Literal["dialogue", "research"]` 路由。必须替换为 `depth` 参数。 |
| 2 | 79-83, 105-125 | 🔴 关键 | `output_style` 默认值映射（`dialogue→bangumi, research→neutral`）。Companion 默认 bangumi。`output_style` 参数保留但简化。 |
| 3 | 196-320 | 🔴 关键 | `_chat_dialogue()` + `_chat_research()` 两个 handler，~80% 代码重复。合并为一个 `_chat()`。 |
| 4 | 323-404 | 🟡 高 | `/chat/stream` 同样在 `agent_type` 上分叉。合并为单一路径。 |
| 5 | 392-395 | 🟢 低 | Critic 节点 SSE 事件推送。Companion 默认模式无 Critic，但 deep 模式仍有——保留但条件化。 |
| 6 | 499-501 | ℹ️ 已知 | `/chat/stream` 未接入记忆写入。已有注释标注，非本次引入。 |

### ChatRequest 目标形态

```python
class ChatRequest(BaseModel):
    message: str
    depth: Literal["auto", "quick", "deep"] = "auto"
    output_style: Literal["neutral", "bangumi"] = "bangumi"
    session_id: str = ""
    user_id: str = "anonymous"
    # agent_type 保留但标记 deprecated，映射到 depth
```

### ChatResponse 目标形态

```python
class ChatResponse(BaseModel):
    reply: str
    iterations: int
    tools_used: list[str]
    query_intent: str
    output_style: str
    depth: str  # 新增：返回实际使用的深度模式
```

---

## 🔄 已自动迁移的（linter 更新）

以下文件已被 linter 更新为 Phase 6 目标结构，不必手动改：

| 文件 | 变化 |
|------|------|
| `agent/persona/profiles.py` | `BANGUMI_CHARACTER` 重写为 Companion 损友人格；`BANGUMI_RESEARCH_CHARACTER` 删除；`DIALOGUE_PROFILE` + `RESEARCH_PROFILE` 合并为 `COMPANION_PROFILE`；`get_character()` 不再接受 `agent_type` 参数 |
| `agent/orchestrate/prompt_builder.py` | 14 层 → 8 层；`expression_guide` 从 layer 12 提前到 layer 2；`build_system_prompt()` 接受 `depth` 参数；`_DATA_INTERPRETATION` 通过 `data_guide` 参数条件注入 |
| `agent/orchestrate/strategies.py` | **新建** — Companion 浅层意图策略（`COMPANION_INTENT_PROMPTS`） |
| `agent/orchestrate/deep_strategies.py` | 重构为深度模式专用：保留 `INTENT_PROMPTS`、`TOOL_DEPENDENCY_CONSTRAINT`、`_DATA_MODEL_CONSTRAINT`、`CRITIC_SYSTEM_PROMPT` |
| `test/test_prompts.py` | Profile 测试更新：检查 `COMPANION_PROFILE`、`"二次元损友"`、`"让对话有趣"`；新增 shallow vs deep 分支测试 |

---

## 实施优先级

| 优先级 | 范围 | 说明 |
|--------|------|------|
| 🔴 先做 | `main.py` 合并两个 handler | Phase 6 Step 2 的核心代码改动。依赖 profiles/prompts 已自动迁移。 |
| 🟡 其次 | `core/config.py` 默认值调整 | `LLM_TEMPERATURE`、记忆配置合并。向后兼容保留旧 key。 |
| 🟢 可后做 | `main.py` stream 端点 | 复用合并后的 graph。深度有限——stream 本身只有 node-level 事件。 |
| ℹ️ 不改 | Phase 1-3 其他层 | Client、RAG、工具、数据库——全都不需要动。 |

---

## 相关文件

- `CLAUDE.md` — Phase 6 完整蓝图
- `docs/design/ROADMAP.md` — 开发路线图（需要更新 Phase 6 状态）
- `docs/tmp/real_data_test.md` — 9 个测试用例 + 实验数据
- `docs/design/data-layer-redesign-discussion.md` — str→dict 迁移过程
- `docs/design/bangumi-api-schema-methodology.md` — A/B/C/D 字段方法论
