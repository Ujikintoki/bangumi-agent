# 工具层 (tools/)

> 数据层子系统 · 1118 行源码 | 状态：✅ 稳定

---

## 一、架构概览

```
                    编排层 (orchestrate/)
                          │
                          │ get_agent_tools() 返回可用工具列表
                          ▼
┌─────────────────────────────────────────────────┐
│            tools/bgm_tools.py                    │
│                                                   │
│  16 个 @tool 函数                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │ 13 无条件注册                                │ │
│  │ 3 需 BANGUMI_ACCESS_TOKEN（条件注册）         │ │
│  └─────────────────────────────────────────────┘ │
│                                                   │
│  search_bangumi_subject()    get_calendar()       │
│  get_bangumi_subject_detail()  get_trending_...() │
│  get_character_detail()      get_hot_topics()     │
│  get_person_detail()         get_episode_...()    │
│  get_subject_opinions()      get_subject_ep...()  │
│  get_entity_comments()       get_subject_cha...() │
│  search_local_bangumi()  ← 唯一本地 RAG 工具       │
│  ─────────────────────────────────────────────   │
│  get_user_profile()*    get_blog()*               │
│  get_user_timeline()*   (*需 Access Token)        │
└──────────────────────┬──────────────────────────┘
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
   BangumiClient   RagEntity-    schemas/
   (clients/)      Retriever     tools_input.py
                   (rag/)
```

---

## 二、16 个工具全景

### 2.1 无条件注册（13 个）

| # | 工具名 | 输入 Schema | 端点/数据源 | 返回 | 核心场景 |
|---|--------|------------|------------|------|---------|
| 1 | `search_bangumi_subject` | `SearchBangumiInput` | `POST /p1/search/{entity}s` | `dict` | 名字→ID 映射 |
| 2 | `get_bangumi_subject_detail` | `GetSubjectDetailInput` | `GET /p1/subjects/{id}` | `dict` | 条目详情 |
| 3 | `get_character_detail` | `GetCharacterDetailInput` | `GET /p1/characters/{id}` | `dict` | 角色详情 |
| 4 | `get_person_detail` | `GetPersonDetailInput` | `GET /p1/persons/{id}` | `dict` | 人物详情 |
| 5 | `get_calendar` | `GetCalendarInput` | `GET /p1/calendar` | `dict` | 每日放送 |
| 6 | `get_trending_subjects` | `GetTrendingSubjectsInput` | `GET /p1/trending/subjects` | `dict` | 热门条目 |
| 7 | `get_hot_topics` | `GetHotTopicsInput` | `GET /p1/trending/subjects/topics` | `dict` | 热议话题 |
| 8 | `get_episode_comments` | `GetEpisodeDiscussionInput` | `GET /p1/episodes/{id}` + comments | `dict` | 单集讨论 |
| 9 | `get_subject_opinions` | `GetSubjectOpinionsInput` | `GET /p1/subjects/{id}/comments` + reviews | `dict` | 条目口碑 |
| 10 | `get_subject_episodes` | `GetSubjectEpisodesInput` | `GET /p1/subjects/{id}/episodes` | `dict` | 剧集列表 |
| 11 | `get_entity_comments` | `GetEntityCommentsInput` | `GET /p1/{type}s/{id}` + comments | `dict` | 角色/人物评论 |
| 12 | `get_subject_characters` | `GetSubjectCharactersInput` | `GET /p1/subjects/{id}/characters` | `dict` | 条目角色 |
| 13 | `search_local_bangumi` | `LocalSearchInput` | `rag_entities` 表（本地 pgvector） | **`str`** | 语义搜索 |

### 2.2 条件注册（3 个，需 `BANGUMI_ACCESS_TOKEN`）

| # | 工具名 | 输入 Schema | 端点 | 返回 | 核心场景 |
|---|--------|------------|------|------|---------|
| 14 | `get_user_profile` | `GetUserProfileInput` | `GET /p1/users/{name}` + 多端点 | `dict` | 用户画像 |
| 15 | `get_blog` | `GetBlogInput` | `GET /p1/blogs/{id}` + comments + subjects | `dict` | 日志分析 |
| 16 | `get_user_timeline` | `UserTimelineInput` | `GET /p1/users/{name}/timeline` | `dict` | 用户动态 |

### 2.3 工具特性矩阵

| 特性 | 数量 | 工具 |
|------|------|------|
| **纯 GET** | 15 | 全部除 `search_bangumi_subject`(POST) |
| **返回 dict** | 15 | 全部除 `search_local_bangumi`(str) |
| **并发调用** | 5 | opinions, episode_comments, entity_comments, user_profile, blog |
| **本地数据** | 1 | `search_local_bangumi` |
| **需要 embedding API** | 1 | `search_local_bangumi` (Zhipu embedding-3) |
| **支持 entity_type 分发** | 2 | `search_bangumi_subject` (3 种), `search_local_bangumi` (4 种含 all) |

---

## 三、动态注册机制

```python
def get_agent_tools() -> list:
    """根据当前配置动态返回 Agent 可用工具列表。"""
    settings = get_settings()

    tools = [
        search_bangumi_subject,
        get_bangumi_subject_detail,
        get_character_detail,
        get_person_detail,
        get_calendar,
        get_trending_subjects,
        get_hot_topics,
        get_episode_comments,
        get_subject_opinions,
        get_subject_episodes,
        get_entity_comments,
        get_subject_characters,
        search_local_bangumi,
    ]

    # 条件追加：仅当 Access Token 可用时
    if settings.BANGUMI_ACCESS_TOKEN:
        tools.extend([get_user_timeline, get_user_profile, get_blog])

    return tools
```

编排层通过 `get_agent_tools()` 获取工具列表后绑定到 LLM（`llm.bind_tools(tools)`）。

---

## 四、错误处理模式

### 4.1 标准错误包装（大多数工具）

```python
@tool(args_schema=SearchBangumiInput)
async def search_bangumi_subject(keyword, entity_type="subject", ...):
    async with BangumiClient() as client:
        result = await client.search(input)
    if "_error" in result:
        return {"_error": f"搜索失败。{result['_error']}"}
    return result
```

三层错误传递：
1. `BaseClient._request()` → `{"_error": "请求超时"}`
2. `BangumiClient.method()` → 检查 `_error`，提前返回
3. `@tool` 函数 → 包装为 `"操作失败。{原始错误}"`

### 4.2 Token 门控错误（3 个需认证的工具）

```python
@tool(args_schema=GetUserProfileInput)
async def get_user_profile(username, ...):
    if not get_settings().BANGUMI_ACCESS_TOKEN:
        return {
            "_error": "系统未配置 Bangumi Access Token，无法获取用户画像。"
                      f"可前往 https://bgm.tv/user/{username} 直接查看。"
        }
```

未配置 token 时，返回兜底 URL 而非静默失败。

### 4.3 RAG 工具特殊错误处理

`search_local_bangumi` 是唯一有三阶段错误的工具：

```python
def _search_local_bangumi_sync(query, ...):
    # 阶段 1: RAG 模块导入失败
    try:
        from rag.retriever import RagEntityRetriever
    except ImportError as exc:
        return f"系统提示：本地搜索引擎模块加载失败。错误：{exc}"

    # 阶段 2: 检索器初始化失败
    try:
        retriever = RagEntityRetriever(engine=engine, zhipu_api_key=...)
    except Exception as exc:
        return f"系统提示：本地搜索引擎初始化失败。错误：{exc}"

    # 阶段 3: 检索执行失败
    try:
        results = retriever.hybrid_search(...)
    except Exception as exc:
        return f"系统提示：语义检索过程中发生异常。错误：{exc}"

    # 无结果 → 建议
    if not results:
        return f"未找到与「{query}」相关的条目。建议：尝试使用更宽泛的关键词..."

    # 正常 → 格式化结果
    return format_results(results)
```

### 4.4 已知不一致

| 问题 | 工具 | 详情 |
|------|------|------|
| 错误前缀不一致 | `get_entity_comments`, `get_user_profile`, `get_blog`, `get_user_timeline` | 返回 `{"_error": result["_error"]}` 无前缀（其他工具格式为 `"操作失败。{error}"`) |
| 返回类型不一致 | `search_local_bangumi` | 唯一返回 `str` 的工具（其余 15 个返回 `dict`） |
| 默认值不一致 | `get_episode_comments` | 函数签名 `comments_limit=30`，但 Schema 默认 `15`（Schema 优先） |

---

## 五、RAG 工具特殊路径

`search_local_bangumi` 是 16 个工具中唯一的本地工具，具有独特架构：

```
search_local_bangumi (async @tool, 返回 str)
  │
  │ asyncio.to_thread()  ← 同步 RAG 代码在线程池中运行
  │
  ▼
_search_local_bangumi_sync (同步函数)
  │
  ├─ 1. 创建 RagEntityRetriever(engine, zhipu_api_key)
  ├─ 2. retriever.hybrid_search(query, entity_type, limit, ...)
  │      ├─ 智谱 embedding-3 向量化 query
  │      ├─ pgvector cosine_distance 召回 limit×2 候选
  │      ├─ 距离阈值过滤 (0.65)
  │      ├─ 多态分桶排序 (subject→rating_total, character/person→collects)
  │      └─ MMR 同源去重
  └─ 3. 格式化结果
       ├─ subject: 评分 + 排名 + 年份 + 平台 + 标签 + 派生信号
       ├─ character: 收藏数 + 出演作品
       └─ person: 收藏数 + 职业 + 代表作
```

**为什么用 `asyncio.to_thread`？** RAG 检索器使用 SQLAlchemy 同步 Session + 智谱同步 API，在线程池中运行避免阻塞 asyncio 事件循环。

---

## 六、输入 Schema 约束

所有 Schema 在 `schemas/tools_input.py`（365 行）中定义，使用 Pydantic v2 `BaseModel` + `Field(description=...)`。关键约束：

| Schema | 约束 |
|--------|------|
| `SearchBangumiInput` | `limit`: 1-8, `subject_type`: 1-6 |
| `GetCalendarInput` | `weekday`: Literal["today","mon"..."all"], `limit_per_day`: 1-15 |
| `GetEpisodeDiscussionInput` | `comments_limit`: 1-40 |
| `GetSubjectOpinionsInput` | `limit`: 1-15 |
| `GetSubjectEpisodesInput` | `limit`: 1-50 |
| `GetTrendingSubjectsInput` | `limit`: 1-12 |
| `GetEntityCommentsInput` | `entity_type`: Literal["character","person"], `limit`: 1-25 |
| `GetUserProfileInput` | `collections_limit`: 1-40 |
| `LocalSearchInput` | `entity_type`: Literal["subject","character","person","all"], `limit`: 1-20 |
| `UserTimelineInput` | `limit`: 1-20 |

所有约束通过 `Field(ge=..., le=...)` 或 `Literal[...]` 强制执行。

---

## 七、派生信号计算

`_compute_subject_signals()` 从评分分布和收藏分布中计算三个派生信号，供 LLM 判断作品口碑：

| 信号 | 算法 | 含义 |
|------|------|------|
| **完成率** | `看过÷(看过+搁置+抛弃)` | 用户粘性 |
| **口碑集中度** | `(最高档+次高档)÷总评分数` 占比 | 口碑一致性 vs 两极化 |
| **热度评分比** | `评分人数÷(评分人数+100)×评分` | 平衡热度与质量 |

这些信号无硬编码"过誉/冷门"标签——只给数字+自然语言描述，让 LLM 结合语境判断。

---

## 八、已知问题

| # | 问题 | 文件 | 严重度 |
|---|------|------|--------|
| 1 | `search_local_bangumi` 返回 `str`，其他 15 个返回 `dict` | `bgm_tools.py:875` | 🟡 不一致 |
| 2 | `_ROLE_MAP` 和 `_TYPE_ICONS` 定义但从未使用 | `bgm_tools.py:50-63` | 🟢 死代码 |
| 3 | 4 个工具的 `_error` 前缀格式与其余 12 个不一致 | `bgm_tools.py` | 🟢 不一致 |
| 4 | `get_episode_comments` 函数默认值与 Schema 默认值不同 | `bgm_tools.py:464` | 🟢 不一致 |
| 5 | `search_local_bangumi` 每次调用新建 `RagEntityRetriever` | `bgm_tools.py:926` | 🟡 性能 |
| 6 | RAG 数据库为空 → `search_local_bangumi` 对用户总是返回"无结果" | `rag_entities` | 🔴 功能不可用 |
