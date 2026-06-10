# 边缘情况审计（2026-06-10）

## 🔴 P0 — 可能崩溃或静默挂起

| # | 问题 | 文件 | 现象 |
|---|------|------|------|
| 1 | **LLM 调用无超时** | `agent/llm.py` `create_llm()` | API 挂起时请求永久阻塞 |
| 2 | **短作品名误判** | `agent/classifier.py` | "EVA""86""K"<5字，走LLM fallback→chitchat→不绑工具→搜不到 |

## 🟡 P1 — 功能降级但不崩溃

| # | 问题 | 文件 | 现象 |
|---|------|------|------|
| 3 | **Dialogue 无重复工具调用检测** | `dialogue/nodes.py` | LLM 在 3 轮熔断前反复调同一工具，浪费配额 |
| 4 | **Dialogue 无逃逸舱** | `dialogue/nodes.py` | 数据不存在时 LLM 继续搜到熔断，不会诚实告知 |
| 5 | **Dialogue XML 泄漏无防护** | `dialogue/nodes.py` | chitchat/factual 不绑工具时，DeepSeek 可能泄漏 `<function_calls>` |
| 6 | **messages 为空路由崩溃** | `research/graph.py:83` `dialogue/graph.py:80` | 读 `messages[-1]` 无空检查 → `IndexError` |
| 7 | **`_extract_final_reply` 兜底无区分度** | `main.py:320` | 异常/超限/工具失败统一返回同一句话 |
| 8 | **Critic `<20字` 硬阈值误伤** | `research/nodes.py:359` | "数据不足建议扩大搜索"13字不匹配逃逸舱→REVISE |
| 9 | **tiktoken `encode()` 无异常保护** | `agent/memory.py:52` | 破坏性 unicode 序列→`encode()` 抛异常→整条记忆管理崩溃 |

## 🟢 P2 — 边缘场景，概率极低

| # | 问题 | 文件 | 现象 |
|---|------|------|------|
| 10 | **多 SystemMessage 被静默丢弃** | `memory.py:193` `nodes.py:111` | 跳过后再追加新 SystemMessage，旧的 lost |
| 11 | **critic_feedback 格式无校验** | `research/prompts.py` | LLM 输出偏离 `"缺陷\|建议\|缺失"` 格式时直接注入 |
| 12 | **ToolNode 错误堆栈泄漏** | `graph.py:165` `dialogue/graph.py:117` | `handle_tool_errors=True` 错误消息含文件路径/堆栈帧进入 LLM 上下文 |

## ⚪ P3 — 性能/技术债（不造成故障）

| # | 问题 | 文件 |
|---|------|------|
| 13 | `create_llm()` 每次调用新建 ChatOpenAI 实例 | `agent/llm.py` |
| 14 | RAG retriever 每次调用重建（含 embedding 初始化） | `tools/bgm_tools.py:1151` |
| 15 | `session_id`/`user_id` 存在但无持久化 | `main.py` `state.py`（Phase 5 解决） |

---

## 修复记录（2026-06-10）

### 架构变更概览

| 维度 | 修改前 | 修改后 |
|------|--------|--------|
| Guardrail 函数 | 仅存在于 `research/nodes.py`，Dialogue 无任何防护 | 提取到 `agent/guardrails.py` 共享模块，两个 agent 统一使用 |
| LLM 超时 | 无超时控制，API 挂起永久阻塞 | `LLM_REQUEST_TIMEOUT=60s` 默认，分类器 10s 短超时 |
| 短作品名分类 | <5 字走 LLM fallback，可能误判 chitchat | 直接返回 `"unknown"`（绑工具），LLM 自行判断 |
| Dialogue 防护 | 无重复检测、无逃逸舱、无 XML 防护 | 三大 guardrail 全部补齐 |
| ToolNode 错误 | 堆栈帧/文件路径泄漏到 LLM 上下文 | `format_tool_error` callable 剥离堆栈 |
| Critic 阈值 | <20 字硬阈值 + 12 条逃逸舱正则 | <10 字 + 15 条逃逸舱正则 |
| 兜底消息 | 统一返回"抱歉，无法处理您的请求。" | 4 种区分化消息（超时/超限/无文本/通用） |
| 意图分类关键词 | 缺少用户查询相关词条 | 新增 `@`、`班友`、`用户` + `@\S{1,20}` 正则 |

### 新建文件

**`agent/guardrails.py`** — 共享 Guardrail 模块

从 `agent/research/nodes.py` 提取并扩展，供 Research Agent 和 Dialogue Agent 共用：

| 导出 API | 用途 |
|----------|------|
| `TERMINAL_RESPONSE_PATTERNS` (15 条) | 终端回复识别（逃逸舱），新增 3 条数据不足告知模式 |
| `is_terminal_response(content) -> bool` | 判断回复是否为合法终端状态 |
| `TOOL_CALL_XML_BLOCK` / `TOOL_CALL_XML_RESIDUE` | XML 泄漏检测正则 |
| `strip_tool_call_xml(content) -> tuple[str, bool]` | XML 泄漏剥离 |
| `check_duplicate_tool_calls(messages) -> str` | 重复工具调用检测 |
| `format_tool_error(error: Exception) -> str` | ToolNode 错误格式化（剥离堆栈） |

### P0 修复

**#1 LLM 调用无超时**
- `core/config.py`: 新增 `LLM_REQUEST_TIMEOUT: float = 60.0`
- `agent/llm.py`: `create_llm()` 新增 `request_timeout` 参数，Azure/Custom/OpenAI 三个分支均传递 `request_timeout=resolved_timeout`
- `agent/research/nodes.py` L88、`agent/dialogue/nodes.py` L61: 分类器 LLM 使用 `request_timeout=10`（10s 足够 10 token 输出）

**#2 短作品名误判**
- `agent/classifier.py` L211-212: `len(msg) < 5` 时不再 `return None`（LLM fallback），改为 `return "unknown"`
- `"unknown"` 不在 `_NO_TOOL_INTENTS` 中，工具会绑定，LLM 自行决定是否调用
- 寒暄关键词（"你好"、"嗨"等）在循环中已被优先匹配，不受影响
- 测试更新: `test_short_message_falls_back_to_llm` → `test_short_message_returns_unknown`

### P1 修复

**#3 Dialogue 无重复工具调用检测**
- `agent/dialogue/nodes.py`: 导入 `check_duplicate_tool_calls`，LLM 调用前检测，检测到重复时注入 HumanMessage 引导

**#4 Dialogue 无逃逸舱**
- `agent/dialogue/nodes.py`: 导入 `is_terminal_response`，消化态 + 终端回复时设置 `iterations = _MAX_ITERATIONS`，路由函数熔断到 END

**#5 Dialogue XML 泄漏无防护**
- `agent/dialogue/nodes.py`: chitchat/factual 无工具通道 → LLM 响应后调用 `strip_tool_call_xml` 剥离，剥离后为空则替换兜底回复

**#7 `_extract_final_reply` 兜底无区分度**
- `main.py`: `_extract_final_reply()` 新增 `error_flag`、`iterations`、`max_iterations` 参数
- 4 种区分化兜底消息：超时 / 超限 / 有工具无文本 / 通用
- `_chat_dialogue` 和 `_chat_research` 调用处传递对应上下文

**#8 Critic `<20 字` 硬阈值**
- 阈值 20 → 10（中文每字信息密度高，如"未找到该条目"仅 6 字）
- `agent/guardrails.py` TERMINAL_RESPONSE_PATTERNS 新增 3 条正则：
  - `r"数据不足.{0,10}(建议|请|可)"`
  - `r"(结果|数据|信息).{0,5}(较少|不足|有限|不多)"`
  - `r"(可|请).{0,5}(扩大|放宽|调整|更换).{0,5}(搜索|范围|关键词)"`

**#9 tiktoken `encode()` 无异常保护**
- `agent/memory.py` `count_tokens()`: try/except，异常时退避 `len(text) // 2`
- `agent/memory.py` `_truncate_text_by_tokens()`: try/except，异常时退避字符截断 `text[:max_tokens * 2]`

### P2 修复

**#10 多 SystemMessage 被静默丢弃**
- `agent/research/nodes.py`、`agent/dialogue/nodes.py`: 跳过 SystemMessage 时统计数量并 DEBUG 日志

**#11 critic_feedback 格式无校验**
- `agent/research/prompts.py` `build_system_prompt()`: 检查 `|` 分隔符 + 超长截断 + WARNING 日志，不丢弃反馈

**#12 ToolNode 错误堆栈泄漏**
- `agent/research/graph.py` L165、`agent/dialogue/graph.py` L117: `handle_tool_errors=True` → `handle_tool_errors=format_tool_error`
- `format_tool_error` 位于 `agent/guardrails.py`，仅保留异常类型名和消息

**#6 (已确认无需修改)**: `messages[-1] if messages else None` guard 已存在于两个 graph 的路由函数

### 分类器增强（测试驱动）

`agent/classifier.py` lookup 规则新增：
- 关键词: `@`、`班友`、`用户`
- 正则: `@\S{1,20}`（匹配 @用户名 格式）
- 移除 `评价` 关键词（过于宽泛），保留在 pattern 中作为组合条件

### 未修复项

- **P3-13**: `create_llm()` 实例缓存 — Phase 5
- **P3-14**: RAG retriever 重建 — Phase 5
- **P3-15**: session_id 持久化 — Phase 5

---
