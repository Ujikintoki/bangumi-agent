# 工具操作指南 — 增 / 改 / 删

> 最后更新: 2026-07-22

---

## 1. 架构总览

一个 Bangumi API 工具在代码中经过 **8 层**，每层有明确职责：

```
Layer 1  schemas/tools_input.py    Pydantic args_schema — LLM 看到的函数签名
Layer 2  schemas/__init__.py       import + __all__ 注册
Layer 3  clients/sanitizers.py     字段白名单 + 类型强制 + 文本截断
Layer 4  clients/client.py         HTTP 请求 + 错误处理
Layer 5  tools/bgm_tools.py        @tool 函数 + _format_* 输出 + get_agent_tools() 注册
Layer 6  tools/__init__.py         import + __all__ 注册
Layer 7  agent/research/prompts.py TOOL_DEPENDENCY_CONSTRAINT（有依赖时）
Layer 8  test/                     测试
```

**不需要改的文件**：`main.py`、`agent/*/graph.py`、`agent/*/nodes.py`、`agent/prompt_builder.py`、`agent/classifier.py`、`core/config.py`、`database/`。

原因：ToolNode 从 `get_agent_tools()` 动态获取工具列表，工具注册对图谱拓扑完全透明。

---

## 2. 新增工具

### 2.1 工具规格 YAML

新增工具前，先填写规格文件 `docs/tool-specs/{tool_name}.yaml`。以下以假设的 `get_group_topics` 为例：

```yaml
# ============================================================================
# 工具规格 — get_group_topics
# ============================================================================

name: get_group_topics
display_name: 获取小组讨论
category: community           # subject | character | person | calendar | community | user | search

description: |
  获取 Bangumi 指定小组的讨论帖列表。
  当用户想了解某个小组的讨论热度、最新话题时调用此工具。

  典型场景：
  - "动画小组最近在讨论什么？"
  - "看看音乐小组的热门帖子"
  - "EVA小组里大家怎么看新剧场版？"

# ── API ─────────────────────────────────────────────────────────────
api:
  method: GET
  path: /p1/groups/{group_name}/topics
  auth_required: false          # true → 需要 BANGUMI_ACCESS_TOKEN

# ── 参数（LLM 可见） ─────────────────────────────────────────────────
parameters:
  - name: group_name
    type: str
    required: true
    constraints: {}
    description: >
      Bangumi 小组名称，通常为英文 Unix 名。
      例如 'anime'、'music'、'eva'。用户说的中文名需要先推断英文名。

  - name: limit
    type: int
    required: false
    default: 20
    constraints: {ge: 1, le: 50}
    description: 返回的讨论帖数量上限，默认 20。

# ── Sanitizer 字段映射 ──────────────────────────────────────────────
sanitizer:
  # API 返回的列表字段名（如果是顶层数组，填 null）
  list_field: data

  # 保留字段：source = API 返回的字段路径（支持点号嵌套），target = 清洗后的字段名
  keep_fields:
    - {source: id,            target: id,            type: int,  default: 0}
    - {source: title,         target: title,         type: str,  default: ""}
    - {source: creator.nickname, target: creator_name, type: str, default: ""}
    - {source: reply_count,   target: reply_count,   type: int,  default: 0}
    - {source: created_at,    target: created_at,    type: str,  default: ""}

  # 文本截断规则（可选）
  truncations: []
  # - {field: title, max_len: 100}

  # 特殊变换（可选，需要手写代码）
  transforms: []
  # - magic_number_map: {field: role, map_name: _CHARACTER_ROLES}

# ── 输出格式 ────────────────────────────────────────────────────────
output_format:
  header: '💬 小组「{group_name}」的讨论帖（共 {total} 条）：\n'
  item_template: '{i}. {title} — {reply_count} 回复 | 作者: {creator_name}'
  footer: '\n── 以上为小组「{group_name}」的最近讨论 ──'
  empty_message: '小组「{group_name}」暂无讨论帖。'
  error_message: '系统提示：获取小组讨论失败。{error}'

# ── 依赖关系 ────────────────────────────────────────────────────────
dependencies:
  needs_search: false           # true → 需要先 search 拿 ID，会加入 TOOL_DEPENDENCY_CONSTRAINT
  needs_token: false            # true → 仅在 BANGUMI_ACCESS_TOKEN 存在时注册
  parallel_safe_with:           # 可以和哪些工具并行调用
    - get_calendar
    - get_trending_topics
```

### 2.2 代码生成步骤

按 YAML 规格文件，按以下顺序修改代码。每步标注了模板。

#### Step 1: `schemas/tools_input.py` — Pydantic args_schema

```python
class GetGroupTopicsInput(BaseModel):
    """
    【小组讨论工具】获取 Bangumi 小组的讨论帖列表。

    当用户想了解某个小组的讨论热度、最新话题时调用此工具。
    小组名称通常为英文 Unix 名称，如 'anime'、'music' 等。

    典型场景：
    - "动画小组最近在讨论什么？"
    - "看看音乐小组的热门帖子"
    """

    group_name: str = Field(
        ...,  # 必填
        description="Bangumi 小组名称，如 'anime'、'music'",
    )
    limit: int = Field(
        default=20,
        description="返回的讨论帖数量上限，默认 20",
        ge=1,
        le=50,
    )
```

#### Step 2: `schemas/__init__.py` — 注册

在 import 块加 `GetGroupTopicsInput`，在 `__all__` 加 `"GetGroupTopicsInput"`。

#### Step 3: `clients/sanitizers.py` — 字段白名单

```python
def sanitize_group_topics(raw: dict) -> dict:
    """小组讨论帖清洗 → 白名单字段提取。

    API: GET /p1/groups/{name}/topics

    保留字段（5个）：
    | 字段 | 来源 | 处理 |
    |------|------|------|
    | id | t.id | 直通，默认 0 |
    | title | t.title | 直通 |
    | creator_name | t.creator.nickname | 嵌套提取 |
    | reply_count | t.reply_count | 直通 |
    | created_at | t.created_at | 直通 |

    丢弃字段及理由：
    | 字段 | 理由 |
    |------|------|
    | t.text | 讨论列表不需要正文，正文应在单独的工具中获取 |
    | t.creator.* (除 nickname) | 列表场景只需要作者名 |
    """
    data = raw.get("data", []) or []
    topics = []
    for t in data:
        creator = t.get("creator", {}) or {}
        topics.append({
            "id": t.get("id", 0),
            "title": t.get("title", ""),
            "creator_name": creator.get("nickname", ""),
            "reply_count": t.get("reply_count", 0),
            "created_at": t.get("created_at", ""),
        })
    return {"topics": topics, "total": raw.get("total", len(topics))}
```

**模板**：
```python
def sanitize_{tool_name}(raw: dict) -> dict:
    """{中文描述} → 白名单字段提取。 API: {method} {path}

    保留字段（{N}个）：
    | 字段 | 来源 | 处理 |
    |------|------|------|
    ...

    丢弃字段及理由：
    | 字段 | 理由 |
    |------|------|
    ...
    """
    # 1. 提取列表数据
    # 2. 逐项白名单 + 类型强制 + 默认值
    # 3. 返回 {results_key: [...], "total": N}
```

#### Step 4: `clients/client.py` — HTTP 方法

```python
async def get_group_topics(self, group_name: str, limit: int = 20) -> dict:
    """获取小组讨论帖列表。

    GET /p1/groups/{group_name}/topics

    Args:
        group_name: 小组名称。
        limit: 返回数量上限。

    Returns:
        清洗后的讨论帖列表，或 ``{"_error": ...}``。
    """
    raw = await self._get(
        f"/p1/groups/{group_name}/topics",
        params={"limit": limit},
    )
    if "_error" in raw:
        return raw
    return sanitizers.sanitize_group_topics(raw)
```

**模板**：
```python
async def {tool_name}(self, {params}) -> dict:
    raw = await self._get(f"{path}", params={query_params})  # 或 self._post
    if "_error" in raw:
        return raw
    return sanitizers.{sanitizer_func}(raw)
```

#### Step 5: `tools/bgm_tools.py` — @tool 函数 + 注册

```python
# ── @tool 函数 ──
@tool(args_schema=GetGroupTopicsInput)
async def get_group_topics(group_name: str, limit: int = 20) -> str:
    """获取 Bangumi 指定小组的讨论帖列表。

    当用户想了解某个小组的讨论热度、最新话题时调用此工具。
    小组名称通常为英文 Unix 名称，如 'anime'、'music' 等。

    典型场景：
    - "动画小组最近在讨论什么？"
    - "看看音乐小组的热门帖子"
    - "EVA小组里大家怎么看新剧场版？"

    Args:
        group_name: 小组名称，如 'anime'、'music'。
        limit: 返回讨论帖数量上限，默认 20。

    Returns:
        纯文本格式的讨论帖列表摘要。无结果时返回友好提示。
    """
    async with BangumiClient() as client:
        result = await client.get_group_topics(
            group_name=group_name, limit=limit
        )

    if "_error" in result:
        return f"系统提示：获取小组讨论失败。{result['_error']}"

    topics: list[dict] = result.get("topics", [])
    if not topics:
        return f"小组「{group_name}」暂无讨论帖。"

    lines: list[str] = [
        f"💬 小组「{group_name}」的讨论帖（共 {len(topics)} 条）：\n"
    ]
    for i, t in enumerate(topics, 1):
        lines.append(
            f"{i}. {t['title']}"
            f" — {t['reply_count']} 回复 | 作者: {t['creator_name']}"
        )

    lines.append(f"\n── 以上为小组「{group_name}」的最近讨论 ──")
    return "\n".join(lines)


# ── get_agent_tools() 中注册 ──
# 无条件（不需要 token）：
tools: list = [
    ...
    get_group_topics,  # ← 新增
]
# 或条件（需要 token）：
if token:
    tools.append(get_group_topics)
```

**模板**：
```python
@tool(args_schema={InputClass})
async def {tool_name}({params}) -> str:
    """{LLM-visible description in Chinese}"""
    async with BangumiClient() as client:
        result = await client.{tool_name}({params})

    if "_error" in result:
        return f"系统提示：{friendly_error}。{result['_error']}"

    # 格式化输出
    # agent_type 感知（可选）：
    # if _get_agent_type() == "dialogue":
    #     return _format_compact(result)
    # return _format_full(result)
```

#### Step 6: `tools/__init__.py` — 注册

在 import 块加 `get_group_topics`，在 `__all__` 加 `"get_group_topics"`。

#### Step 7: `agent/research/prompts.py` — 依赖声明（有依赖时）

**仅当工具有链式依赖时需要**（如 `get_bangumi_subject_detail` 需要先 search 拿 subject_id）。

在 `TOOL_DEPENDENCY_CONSTRAINT` 中对应分组加条目：
```
1. 以下工具需要 subject_id 参数：
   - get_group_topic_detail（需要 topic_id，先用 search 在对应小组中定位）
```

**独立端点不需要此步骤。**

#### Step 8: 测试

在 `test/` 中已有的工具测试模式上加条目。

---

## 3. 修改现有工具

### 3.1 修改输出格式

**只改 `tools/bgm_tools.py` 中的 `_format_*` 函数。** 不改 API 调用链。

常见改动：
- Dialogue compact 模式调整 → `_format_subject_detail_compact()`
- Research full 模式调整 → `_format_subject_detail_full()`
- 添加/删除输出字段 → 修改对应格式化函数

### 3.2 修改 Sanitizer 保留字段

**只改 `clients/sanitizers.py` 中对应函数。**

加字段：在 return dict 中加一行 `"new_field": raw.get("source_path", default)`。
删字段：删除对应行。
改截断长度：修改 `_truncate(text, max_len)` 的 `max_len` 参数。

**注意**：加字段后需同步更新工具的 `_format_*` 函数以使用新字段。

### 3.3 修改工具参数

需要改 **3 个文件**：
1. `schemas/tools_input.py` — 更新 Pydantic model
2. `tools/bgm_tools.py` — 更新 @tool 函数签名
3. `clients/client.py` — 如果参数影响 HTTP 请求

### 3.4 修改工具依赖关系

**只改 `agent/research/prompts.py` 的 `TOOL_DEPENDENCY_CONSTRAINT`。**

---

## 4. 删除工具

改 **3 个文件**：

| 文件 | 操作 |
|------|------|
| `tools/bgm_tools.py` | 从 `get_agent_tools()` 中移除函数引用（保留 `@tool` 函数定义以备日后恢复） |
| `tools/__init__.py` | 移除 import + `__all__` |
| `schemas/__init__.py` | 移除 import + `__all__` |

**保留不动**：`@tool` 函数定义、sanitizer、client 方法、Pydantic schema。方便日后恢复。

---

## 5. 检查清单

### 新增工具 □

```
□ 1. 创建 docs/tool-specs/{tool_name}.yaml 规格文件
□ 2. schemas/tools_input.py — Pydantic BaseModel
□ 3. schemas/__init__.py — import + __all__
□ 4. clients/sanitizers.py — sanitize 函数（含字段白名单注释表）
□ 5. clients/client.py — async 方法
□ 6. tools/bgm_tools.py — @tool 函数
□ 7. tools/bgm_tools.py — get_agent_tools() 注册
□ 8. tools/__init__.py — import + __all__
□ 9. agent/research/prompts.py — TOOL_DEPENDENCY_CONSTRAINT（如有依赖）
□ 10. 测试
□ 11. 启动 uvicorn，发真实请求验证
```

### 修改工具 □

```
□ 1. 确定改动层级（sanitizer / client / tool / prompt）
□ 2. 只改对应层
□ 3. 跑 test/test_bgm_tools.py 和全量 pytest
```

### 删除工具 □

```
□ 1. tools/bgm_tools.py — get_agent_tools() 中注释掉（不删函数定义）
□ 2. tools/__init__.py — 注释 import + __all__
□ 3. schemas/__init__.py — 注释 import + __all__
```

---

## 6. YAML → 代码的映射关系

| YAML 字段 | 生成代码位置 |
|-----------|------------|
| `parameters` | `schemas/tools_input.py` — Pydantic Field |
| `api.method` + `api.path` | `clients/client.py` — `self._get/post(url, params)` |
| `sanitizer.keep_fields` | `clients/sanitizers.py` — return dict |
| `sanitizer.truncations` | `clients/sanitizers.py` — `_truncate(field, max_len)` |
| `sanitizer.transforms` | `clients/sanitizers.py` — 手写映射逻辑 |
| `output_format` | `tools/bgm_tools.py` — `_format_*()` 函数 |
| `dependencies.needs_search` | `agent/research/prompts.py` — `TOOL_DEPENDENCY_CONSTRAINT` |
| `dependencies.needs_token` | `tools/bgm_tools.py` — `get_agent_tools()` 条件注册 |
| `dependencies.parallel_safe_with` | `agent/research/prompts.py` — `TOOL_DEPENDENCY_CONSTRAINT` 第4组 |
| `description` | `tools/bgm_tools.py` — @tool 函数的 docstring |

---

## 7. 相关文件速查

| 文件 | 角色 |
|------|------|
| `docs/tool-specs/` | 工具 YAML 规格文件目录 |
| `schemas/tools_input.py` | 14 个 Pydantic args_schema |
| `clients/sanitizers.py` | ~600 行，20+ sanitizer 函数 |
| `clients/client.py` | ~700 行，14 个业务方法 |
| `clients/base.py` | BaseClient — 重试/超时/认证 |
| `tools/bgm_tools.py` | ~1800 行，14 个 @tool + 格式化 + 注册 |
| `agent/research/prompts.py` | `TOOL_DEPENDENCY_CONSTRAINT` + `INTENT_PROMPTS` |
| `agent/guardrails.py` | `format_tool_error`、`check_duplicate_tool_calls` |
| `test/test_bgm_tools.py` | 工具输出格式测试（13 个） |
