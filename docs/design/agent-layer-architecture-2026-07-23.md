# Agent 层架构全览 — 2026-07-23

> 本文档是对 `CLAUDE.md` Agent 层描述的详细补充。涵盖拓扑、分类、共享核心、人格化、记忆、guardrails、state、LLM 工厂。写于 2026-07-22/23 大规模重构之后。

## 规模

4,247 行，17 个文件。

```
agent/
├── classifier.py        # 138 行 — 单阶段 LLM 意图分类
├── llm.py               # 164 行 — create_llm() 多 provider 工厂
├── memory.py            # 379 行 — L1 滑动窗口
├── memory_manager.py    # 1050 行 — L2 语义记忆
├── session_cache.py     # 194 行 — 跨 HTTP 消息缓存
├── profiles.py          # 287 行 — CharacterProfile + AgentProfile
├── prompt_builder.py    # 184 行 — 统一 prompt 组装器
├── reasoning_core.py    # 190 行 — 共享推理辅助函数
├── guardrails.py        # 169 行 — 终端检测、XML 剥离、重复检测
│
├── research/            # 1,052 行
│   ├── state.py         #  80 行
│   ├── graph.py         # 204 行
│   ├── nodes.py         # 488 行（含 Critic + 辅助函数）
│   └── prompts.py       # 280 行
│
└── dialogue/            # 440 行
    ├── state.py         #  58 行
    ├── graph.py         # 141 行
    ├── nodes.py         # 200 行
    └── prompts.py       #  41 行
```

---

## 1. Graph 拓扑

### Research Agent — 3 节点

```
START → reasoning_node ───────────────┐
          │                            │
          ├─ classify_intent_step     │
          ├─ recall_memory_step       │
          ├─ build_system_prompt      │
          └─ LLM invoke (始终 bind_tools)
               │                       │
     ┌────────┼─────────┐              │
     │        │         │              │
  tool_calls chitchat  其他无工具       │
     │     (快速通道)     │              │
     ▼        ▼         ▼              │
  tool_node  END    critic_node ←──────┘
     │             (llm/rule)
     │         ┌──────┴──────┐
     │        PASS       REVISE
     │         │           │
     │        END    reasoning_node ───┘
     │
     └──→ reasoning_node
```

- **节点数**：3（reasoning, tool, critic）
- **最大迭代**：12
- **快速通道**：chitchat → 跳过 Critic → 直接 END
- **Critic**：默认 LLM 模式（三维度：完整性、具体性、工具利用），可切 rule
- **默认输出风格**：neutral

### Dialogue Agent — 2 节点

```
START → dialogue_reasoning_node ───┐
          │                         │
          ├─ classify_intent_step  │
          ├─ recall_memory_step    │
          ├─ build_dialogue_prompt │
          └─ LLM invoke (始终 bind_tools)
               │                    │
         ┌────┴────┐               │
         │         │               │
    tool_calls   无工具              │
         │         │               │
         ▼         ▼               │
     tool_node    END              │
         │                         │
         └──→ dialogue_reasoning ──┘
```

- **节点数**：2（reasoning, tool），无 Critic
- **最大迭代**：4
- **收敛机制**：
  - iter≥3 → 注入 `_LAST_CHANCE_INSTRUCTION` + 强制解绑工具
  - 重复工具调用检测 → 注入 dedup feedback
  - 终端回复逃逸舱（`is_terminal_response`）
- **默认输出风格**：bangumi

### 两个 Agent 的差异

| | Research | Dialogue |
|---|---|---|
| 节点数 | 3 | 2 |
| 最大迭代 | 12 | 4 |
| Critic | ✅ LLM（默认）/ rule | ❌ |
| 默认人格 | neutral | bangumi |
| L1 token 预算 | 8,000 | 4,000 |
| L2 语义阈值 | 0.50 | 0.35 |
| L2 注入预算 | 700 | 300 |
| 收敛方式 | Critic REVISE | last-chance bailout |
| 消化态引导 | ✅ HumanMessage 注入 | ❌ |
| 重复检测 | Critic 内 | node 内 |

---

## 2. 意图分类 (`classifier.py`, 138 行)

单阶段 LLM 分类。2026-07-22 删除了 `INTENT_RULES`（213 行关键词+正则）和 `classify_intent_rule()`。

```
用户输入 → classify_intent()
              │
              ├─ 空消息 → ("chitchat", "llm")
              ├─ llm=None → ("unknown", "llm")
              └─ 正常 → classify_intent_llm()
                           │
                           ├─ LLM invoke (temperature=0, max_tokens=10)
                           ├─ 提取第一个单词
                           ├─ 不在 _VALID_INTENTS → fallback "unknown"
                           └─ 返回 (intent, "llm")
```

**8 个 intent**：chitchat, factual, lookup, discovery, realtime, debate, emotional, unknown

**intent 的用途**（仅两处）：
1. 选择 `INTENT_PROMPTS` 策略变体 → 注入 System Prompt
2. chitchat 走快速通道 → 跳过 Critic（Research 仅此一项）

**intent 不再控制**：工具可用性。LLM 始终 `bind_tools(tools)`。

`INTENT_CLASSIFIER_PROMPT` 包含：
- bare title → lookup 引导（"进击的巨人"、"EVA"）
- 复合查询 → 数据意图优先（"你好，EVA评分？" → lookup）
- 短文本 → 作品名优先判为 lookup
- 未知 → unknown 兜底

---

## 3. 共享推理核心 (`reasoning_core.py`, 190 行)

5 个纯函数/async 函数，两个 node 共用。接收 `dict` 而非 TypedDict，通过 `.get()` 访问字段。

| 函数 | 签名 | 职责 |
|------|------|------|
| `extract_user_input` | `(state: dict) -> str` | 从 messages 提取最后一条 HumanMessage |
| `classify_intent_step` | `(state: dict) -> (intent, method, did_classify)` | 首轮 LLM 分类，后续复用缓存 |
| `recall_memory_step` | `(state, *, max_tokens, recall_threshold=None) -> str` | L2 语义召回 + 格式化 |
| `build_message_list` | `(messages, system_content) -> list` | SystemMessage 替换 + 历史拼接 |
| `guard_xml_leak` | `(response, *, is_digesting, fallback_text, log=None) -> AIMessage` | 消化态 XML 剥离 |

调用方式（以 Research 为例）：

```python
query_intent, intent_method, did_classify = await classify_intent_step(state)
memory_context = await recall_memory_step(state, max_tokens=700)
messages_for_llm = build_message_list(messages, system_content)
response = guard_xml_leak(response, is_digesting=is_digesting, fallback_text="...")
```

---

## 4. 人格化系统

### 4.1 数据模型 (`profiles.py`, 287 行)

```python
@dataclass(frozen=True)
class CharacterProfile:
    key: str              # "bangumi" | "neutral"
    identity: str         # "你是谁"
    motivation: str       # 行为动机
    expression_guide: str # 怎么说
    guardrails: str       # 硬约束（字数、emoji、Markdown 禁令）
    tool_behavior: str     # 对工具/数据的态度

@dataclass(frozen=True)
class AgentProfile:
    key: str              # "dialogue" | "research"
    capabilities: str     # 能力描述
    tool_strategy: str    # 工具调用策略
    output_format_guide: str  # 格式指引
    default_character: str    # 默认角色 key
```

**3 个角色实例**：

| 实例 | key | 特点 |
|------|-----|------|
| `BANGUMI_CHARACTER` | bangumi | 腹黑萝莉，30-80 chars，禁止 emoji/Markdown 表格 |
| `BANGUMI_RESEARCH_CHARACTER` | bangumi_research | 同上但无字数限制，数据完整性优先 |
| `NEUTRAL_CHARACTER` | neutral | 中性助手，简洁具体，禁止 Markdown 表格 |

**2 个 Agent 实例**：

| 实例 | 特点 |
|------|------|
| `DIALOGUE_PROFILE` | 浅层原则，bare title 先问再搜，最多 2 轮工具 |
| `RESEARCH_PROFILE` | 深度链式，search→detail→characters→comments，禁止 Markdown 表格 |

### 4.2 Prompt 组装 (`prompt_builder.py`, 184 行)

统一 builder，13 层组装。**角色优先——角色身份在最前面。**

```
Layer 1:  character.identity + motivation        ← 角色是第一层
Layer 2:  agent_profile.capabilities             ← 能力是附属
Layer 3:  character.tool_behavior                ← 角色对工具的态度
Layer 4:  agent_profile.tool_strategy            ← 具体策略
Layer 5:  tool_constraint（如有）                 ← 依赖规则
Layer 6:  _TOOL_CALLING_RULES                    ← 调工具后必须回复、数据不足时诚实
Layer 7:  _CONTINUITY_RULES                      ← 话题绑定检测
Layer 8:  intent 策略变体                         ← INTENT_PROMPTS
Layer 9:  memory_context（如有）                  ← L2 记忆 + tone hints
Layer 10: critic_feedback（如有）                 ← Critic 定向反馈
Layer 11: character.expression_guide             ← 怎么说
Layer 12: agent_profile.output_format_guide      ← 格式模板
Layer 13: character.guardrails                   ← 硬约束
Layer 14: last_chance_instruction（如有）         ← Dialogue 熔断
```

### 4.3 意图策略变体 (`research/prompts.py`, 280 行)

8 个 `INTENT_PROMPTS`。关键策略：

| intent | 策略要点 |
|--------|---------|
| lookup | search→detail，名称消歧，两次无果诚实告知 |
| discovery | 参考作品→标签→同类；无参考→RAG+fuzzy search |
| realtime | 时效工具并行，最多列 10 条 |
| debate | **用数据支撑观点**，有数据背书的毒舌比空口有力 |
| emotional | 先共情→再推荐，工具是附属品 |
| factual | 尽量不调工具，不确定时优先搜索 |
| chitchat | 尽量不调工具，混入数据查询时正常搜索 |
| unknown | 通用策略，自行判断 |

`TOOL_DEPENDENCY_CONSTRAINT`：显式声明工具串行依赖（search→detail→characters）。

`CRITIC_SYSTEM_PROMPT`：LLM 版 Critic 的三维度评估 prompt + 逃逸舱规则。

### 4.4 Dialogue prompt (`dialogue/prompts.py`, 41 行)

薄封装。`build_dialogue_prompt(memory_context, output_style)` 委托给 `prompt_builder.build_system_prompt()`，不传 intent/critic_feedback。

---

## 5. 记忆系统

### L1 — 滑动窗口 (`memory.py`, 379 行)

- **Token 计数**：tiktoken `cl100k_base` 精确编码（非 `len//4` 估算）
- **预算**：Research 8,000 / Dialogue 4,000
- **策略**：SystemMessage 保留；旧消息从头部丢弃；尾优先遍历
- **孤儿清理**：丢弃 ToolMessage 其配对 AIMessage 已被截断
- **单条截断**：ToolMessage > 1500 tokens → 截断内容

### L2 — 语义记忆 (`memory_manager.py`, 1050 行)

**写入路径**（fire-and-forget，15s 超时）：
```
对话 (Human + AI) → 截断 3000 tokens
→ DeepSeek 摘要 {"summary", "entities", "tone"}
→ Zhipu embedding-3 (2048d)
→ UPSERT session_memories
→ user_id="anonymous" 时短路跳过
```

**召回路径**（双通道 + 时间衰减）：
```
用户查询 → Zhipu embedding-3
├─ 通道 1: 语义检索 → cosine_distance ≤ threshold
│   combined = (1-distance) × 0.5^(days/14)
└─ 通道 2: recency fallback（语义不足时）
    按 created_at DESC + anchor filter (≤0.60)
→ 按 combined_score 排序 → top-5 → 格式化 → ≤max_tokens 注入
```

**降级路径**（7 个 failure point，全部 graceful degradation）：
embedding API 超时 → recency-only；DB 错误 → scored=[]；摘要失败 → reply[:200]；INSERT 失败 → 跳过

### Session Cache (`session_cache.py`, 194 行)

- **存储**：纯内存，不落盘
- **TTL**：1 小时
- **容量**：最多 1000 session
- **并发**：asyncio.Lock per-session
- **不缓存**：SystemMessage（每轮重建）

---

## 6. Guardrails (`guardrails.py`, 169 行)

| 函数 | 职责 | 使用者 |
|------|------|--------|
| `is_terminal_response(content)` | 13 条正则判断追问/澄清/诚实告知 | Dialogue 逃逸舱、Critic rule |
| `strip_tool_call_xml(content)` | 剥离 `<function_calls>` XML | 两个 node（消化态安全网） |
| `check_duplicate_tool_calls(messages)` | 检测连续两轮相同工具调用 | Dialogue dedup、Critic rule |
| `format_tool_error(error)` | 剥离堆栈，只保留类型+消息 | ToolNode |
| `TOOL_CALL_XML_RESIDUE` | XML 残骸检测正则 | Critic rule（第三道防线） |

---

## 7. State 定义

### AgentState (`research/state.py`, 80 行)

```python
class AgentState(TypedDict):
    messages: list          # 完整消息历史
    iterations: int         # 当前轮次
    critic_status: str      # PENDING | PASS | REVISE
    critic_feedback: str    # Critic 定向反馈
    query_intent: str       # 意图分类结果
    session_id: str         # L1 会话标识
    user_id: str            # L2 用户标识
    error_flag: bool        # 兜底模式
    _memory_context: str    # L2 召回缓存
    output_style: str       # 输出风格
```

`_MAX_ITERATIONS = 12`

### DialogueState (`dialogue/state.py`, 58 行)

```python
class DialogueState(TypedDict):
    messages: list
    iterations: int
    query_intent: str
    session_id: str
    user_id: str
    _memory_context: str
    output_style: str
```

无 `critic_status`、`critic_feedback`、`error_flag`。
`_MAX_ITERATIONS = 4`

---

## 8. LLM 工厂 (`llm.py`, 164 行)

`create_llm()` — 多 provider 抽象。通过 pydantic-settings 自动选择：

- `LLM_BASE_URL` 有值 + `LLM_AZURE_ENDPOINT` 空 → 自定义 endpoint（DeepSeek/Qwen/OpenAI 兼容）
- `LLM_AZURE_ENDPOINT` 有值 → Azure OpenAI
- 都没有 → 标准 OpenAI

参数：`temperature=0.3`, `max_tokens=2048`, `request_timeout=60s`（可通过 `.env` 覆盖）。每次调用创建新 `ChatOpenAI` 实例。

分类器调用 `create_llm(temperature=0, max_tokens=10, request_timeout=10)`。

---

## 9. 重构历史（2026-07-22/23）

| 日期 | 改动 | 净删 |
|------|------|------|
| 07-22 | 删除 `_NO_TOOL_INTENTS` → LLM 始终绑工具 | -55 行 |
| 07-22 | 删除 `INTENT_RULES` + `classify_intent_rule()` → LLM-only 分类 | -295 行 |
| 07-22 | 提取 `reasoning_core.py` 共享函数 | -30 行（清零重复） |
| 07-23 | `CRITIC_MODE` 默认 `"llm"` | 1 行 |
| 07-23 | debate prompt "少调工具" → "用数据支撑观点" | 8 行 |
| 07-23 | Dialogue tool_strategy 加 bare title 追问规则 | 1 行 |
| 07-23 | Markdown 表格禁令加入 3 个 guardrails | 6 行 |
| 07-23 | `session_id` 默认 `""` → 自动生成 UUID | 10 行 |
| 07-23 | 新建 `.env.example` | +35 行 |

**累计净删 ~350 行。classifier.py 422→138 行（-67%）。**
