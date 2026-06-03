# Tools list
**Using these tools to interact with Bangumi API**
## 0. search_bangumi - 名字→ID 搜索工具
### 0.1 聚合路由
1. POST /p1/search/subjects — 搜索条目
2. POST /p1/search/characters — 搜索角色
3. POST /p1/search/persons — 搜索人物
### 0.2 设计思路
统一的名字→ID 映射入口。用户说"帮我查《平家物语》"，LLM 先调此 Tool 得到 id=348335，再用该 ID 调用其他 Tool。支持精确/模糊匹配，返回结果按相关度排序。角色和人物搜索共享同一个 Tool，用 entity_type 区分。
### 0.3 是否需要 Auth
否（三个端点均无 auth 要求）
### 0.4 Input Schema
```python
class SearchBangumiInput(BaseModel):
    keyword: str = Field(..., description="搜索关键词")
    entity_type: Literal["subject", "character", "person"] = "subject"
    limit: int = Field(default=5, ge=1, le=10)
    subject_type: Optional[int] = None  # 仅 subject: 1=book 2=anime 3=music 4=game 6=real
    nsfw: Optional[bool] = None         # 仅 character: 是否包含NSFW
```
### 0.5 Tool 内部逻辑
1. 根据 entity_type 选择端点：subject→POST /p1/search/subjects，character→POST /p1/search/characters，person→POST /p1/search/persons
2. 请求 body 构造成 {keyword, sort:"match", filter:{type:[...]}}（filter 按需）
3. 返回精简摘要：只保留 id, name, nameCN, type, score, rank, nsfw
4. 单条结果时直接返回（LLM 无需选择）；多条时返回列表供 LLM 进一步推理
5. 0 条结果时返回空列表 + 提示"未找到匹配结果，尝试调整关键词"
### 0.6 返回裁剪字段
| 保留 | 裁剪掉 |
|---|---|
| id, name, nameCN, type | info（长字符串） |
| rating.score, rating.rank , rating.total(评分人数，可以间接说明热度)| , rating.count（数组） |
| nsfw | images（5种尺寸URL） |
| — | locked |

## 1. get_blog - 日志分析
### 1.1 聚合路由：
1. GET /p1/blogs/{entryID} — 正文
2. GET /p1/blogs/{entryID}/comments — 评论
3. GET /p1/blogs/{entryID}/subjects — 关联条目
### 1.2 设计思路：
 一个 Tool 一次返回三个维度的数据。LLM 拿到"正文 + 评论反应 + 关联作品"才能做完整分析，而不是调用三次。
### 1.3 是否需要Auth:
是
### 1.4 Input Schema:
class GetBlogInput(BaseModel):
    entry_id: int
    include_comments: bool = True    # 默认带上评论
    include_subjects: bool = True    # 默认带上关联条目
### 1.5 Tool 内部逻辑
1. 并行请求三个端点（用 asyncio.gather）
2. normalize: 展平 blog.content（⚠️YAML 字段名是 content 不是 text）, blog.user.nickname, blog.createdAt
3. blog.tags 保留（字符串数组，可帮助 LLM 理解主题）
4. comments 截断至最近 30 条（默认值，可选），每条 content 截断 200 字（默认值，可选）
5. comments 的 reactions 只保留计数（reactions.length），不展开嵌套用户列表
6. subjects 只保留 id + nameCN + type + score
7. 返回结构化 JSON
### 1.6 返回裁剪字段（对照 YAML BlogEntry + Comment + SlimSubject schema）
| 保留 | 裁剪掉 |
|---|---|
| entry.id, entry.title, entry.content(截断300字（默认值，可选）), entry.tags | entry.icon, entry.type, entry.uid, entry.views, entry.noreply |
| entry.createdAt, entry.replies(评论数) | entry.related, entry.public, entry.updatedAt |
| user.nickname | user.avatar, user.sign, user.group, user.joinedAt, user.id, user.username |
| comments[].id, comments[].content(截断200字（默认值，可选）), comments[].createdAt | comments[].mainID, comments[].creatorID, comments[].relatedID, comments[].state, comments[].relatedPhotoID |
| comments[].user.nickname | comments[].user 完整 SlimUser 对象 |
| comments[].reactions_count (reactions.length) | comments[].reactions 完整 Reaction[] 嵌套 |
| subjects[].id, subjects[].nameCN, subjects[].type, subjects[].rating.score, subjects[].rating.total(评分人数，可以间接说明热度)| subjects[].rating.count(10元素数组), , subjects[].images, subjects[].locked |

## 2. get_calendar - 番组表
### 2.1 聚合路由
1. GET /p1/calendar — 整周数据
### 2.2 设计思路
设计思路： API 返回的是一周的列表，按星期几分组的数组。它把 { "1": [...], "2": [...] } 标准化为带中文标签的结构。你的 Tool 可以内置"今天星期几"的过滤逻辑，让 LLM 直接拿到当日数据。
### 2.3 是否需要Auth:
否
### 2.4 Input Schema:
class GetCalendarInput(BaseModel):
    weekday: Optional[Literal["today", "mon", "tue", "wed", "thu", "fri", "sat", "sun", "all"]] = "today"
    limit_per_day: int = Field(default=10, ge=1, le=50)
### 2.5 Tool 内部逻辑
1. 调用 GET /p1/calendar
2. normalize: 展平 subject.name_cn, subject.rating.score
3. 按 weekday 过滤
4. 返回: [{weekday: "星期一", items: [{id, name_cn, score, rank, watchers}]}]

## 3. get_episode_discussion — 单集讨论
### 3.1 聚合路由
1. GET /p1/episodes/{episodeID} — 单集详情
2. GET /p1/episodes/{episodeID}/comments — 吐槽箱
### 3.2 设计思路
这是一个核心分析场景。用户问"第 X 集大家怎么看"，Tool 拿到单集元数据 + 吐槽箱，LLM 从中提取情感倾向、高频词、争议点。注意 episode.comments 返回的是纯数组（不是 {data:[], total}），而 subject.comments 返回带 total 的对象。episode.sort 应映射为 ep_number 字段帮助 LLM 理解"这是第几集"。
### 3.3 是否需要 Auth
否（但 Token 可选：NSFW 条目无 token 会返回 404）
### 3.4 Input Schema
```python
class GetEpisodeDiscussionInput(BaseModel):
    episode_id: int
    comments_limit: int = Field(default=30, ge=1, le=200)
```
### 3.5 Tool 内部逻辑
1. 并行请求: GET episode + GET comments（两个端点独立，asyncio.gather）
2. normalize episode: 保留 id, sort→ep_number, nameCN, airdate, duration, desc(截断300字（默认值，可选）), subject→{id, nameCN, score}
3. normalize comments: 纯数组直接处理，每条保留 id, user.nickname, content(截断200字（默认值，可选）), createdAt, reactions数量, replies数量(不展开嵌套)
4. ⚠️ YAML Comment schema 包含 reactions[]（Reaction = {value: int, users: SimpleUser[]}），保留 reactions 计数即可，对 LLM 分析社区情绪有用
5. 裁剪掉: episode.subject 的完整嵌套（只保留摘要）、images、locked、disc、subjectID、state
6. 错误处理:
   - 404 + 无 auth → "可能是 NSFW 条目，请在配置中提供 Access Token"（参考 bgm-cli 的 handleEpisodeListError）
   - 403 → "该条目需要更高权限才能访问"
6. 返回: {episode: {...}, comments: [...], comment_count: N}

## 4. get_user_profile — 用户画像
### 4.1 聚合路由
1. GET /p1/users/{username} — 用户资料
2. GET /p1/users/{username}/collections/subjects — 条目收藏
3. GET /p1/users/{username}/collections/characters — 角色收藏
4. GET /p1/users/{username}/collections/persons — 人物收藏
5. GET /p1/users/{username}/blogs — 日志列表
### 4.2 设计思路
这是"品味画像分析"的完整数据源。一次调用返回用户五维数据，LLM 可以做：
评分分布分析 → "偏好小众文艺"
收藏类型分布 → "主攻动画和音乐"
角色收藏聚类 → "偏爱傲娇系角色"
博客标题扫描 → "经常写深度分析"
### 4.3 是否需要Auth：
- /p1/users/{username} 不需要 auth（公开资料）
- /p1/users/{username}/blogs 需要 auth（auth: true）
- collections系列不强制auth
### 4.4 Input Schema
class GetUserProfileInput(BaseModel):
    username: str
    collections_limit: int = Field(default=50, ge=1, le=200)
    include_blogs: bool = True
    include_characters: bool = False     # 默认关闭，按需开启
    include_persons: bool = False        # 默认关闭，按需开启
### 4.5 Tool内部逻辑
1. 并行请求: user + collections/subjects（必须），characters/persons/blogs（按需）
2. normalize:
   - subject collections → 展平 subject.name_cn, collection.rate, collection.type
   - collections 中 subject 数据去重（多个入口可能指向同一 subject）
3. 聚合统计: 自动计算评分均值/中位数/分布、收藏类型分布
4. 返回: { user, collection_stats, recent_blogs, top_characters }
5. 防御: 如果 total 不可靠，兜底顺序抓取（参考 fetchAllCollections）

## 5. get_subject_discussion — 条目讨论全景
### 5.1 聚合路由：
1. GET /p1/subjects/{subjectID}/comments — 吐槽箱
2. GET /p1/subjects/{subjectID}/reviews — 长篇评测
3. GET /p1/subjects/{subjectID}/topics — 讨论帖
4. GET /p1/subjects/{subjectID}/episodes — 剧集列表（用于定位单集）
### 5.2 设计思路：
这是"全面了解一部作品的社区评价"的入口。四个端点的数据各有侧重：
- comments → 短评、吐槽、评分分布（情感温度计）
- reviews → 深度分析（观点挖掘）
- topics → 社区讨论热点（议题发现）
- episodes → 剧集结构（帮助 LLM 理解"第 X 集是转折点"）
### 5.3 需要 Auth：
均不强制auth
### 5.4 Input Schema：
class GetSubjectDiscussionInput(BaseModel):
    subject_id: int
    data_types: list[Literal["comments", "reviews", "topics", "episodes"]] = ["comments", "reviews"]
    limit: int = Field(default=10, ge=1, le=50)
### 5.5 Tool 内部逻辑
1. 并行请求选定的 data_types
2. normalize:
   - comments → 展平 id, user.nickname, type(收藏状态), rate, comment, updatedAt, reactions_count
     ⚠️ YAML SubjectInterestComment schema 包含 reactions[] 字段
   - reviews → ⚠️ YAML SubjectReview = {id, user, entry: SlimBlogEntry}
     数据在 entry 子对象中: 保留 entry.title, entry.summary(截断200字), entry.createdAt, user.nickname
   - topics → ⚠️ YAML Topic schema 有 creator (SlimUser) 字段
     保留 id, title, creator.nickname, replyCount, createdAt, updatedAt
   - episodes → 按 sort 排序，只保留 type=0(main)，每条保留 id, sort→ep_number, nameCN, airdate, comment(吐槽数)
3. 输出时附带聚合统计:
   - comments 评分分布: {1-3: x条, 4-6: y条, 7-8: z条, 9-10: w条}
   - topics 总回复量: sum(reply_count)
4. 防御: NSFW 返回空数据时加提示

## 6. get_trending — 热门趋势
### 6.1 聚合路由：
1. GET /p1/trending/subjects — 热门条目
2. GET /p1/trending/subjects/topics — 热门讨论
### 6.2 设计思路：
这是回答"最近什么火"的数据源。两个维度：
- 作品热度（subjects）→ "XXX 番最近热度飙升"
- 讨论热度（topics）→ "XXX 话题正在被激烈讨论"
### 6.3 是否需要 Auth：
否
### 6.4 Input Schema：
class GetTrendingInput(BaseModel):
    category: Literal["subjects", "topics", "both"] = "both"
    subject_type: Optional[Literal["anime", "book", "music", "game", "real"]] = None
    limit: int = Field(default=10, ge=1, le=30)
### 6.5 Tool 内部逻辑
1. 根据 category 决定请求哪些端点: subjects→GET trending/subjects, topics→GET trending/subjects/topics
2. trending/subjects 返回 {data:[{subject:{...}, count:N}], total} → 裁剪 subject 为只保留 id, nameCN, type, score, rank
3. trending/topics 返回 {data:[{id, title, replyCount, createdAt, subject:{...}, creator:{...}}], total} → 裁剪 subject 为摘要, creator 只保留 nickname
4. 并行请求两个端点（both 模式下），合并返回
5. 返回: {subjects: [{id, nameCN, type, score, rank, heat_count}], topics: [{id, title, reply_count, subject_name, creator_name}]}
### 6.6 返回裁剪
| 保留 | 裁剪掉 |
|---|---|
| subject.id, subject.nameCN, subject.rating.score | subject.info, subject.rating.count, subject.images |
| count/heat_count | subject.locked, subject.nsfw |
| topic.id, title, replyCount, createdAt | topic.state, display, replies(空) |
| creator.nickname | creator.avatar, creator.sign, creator.joinedAt |

## 7. get_entity_comments — 角色/人物评论（统一 Tool）
### 7.1 聚合路由：
1. GET /p1/characters/{characterID}/comments
2. GET /p1/persons/{personID}/comments
### 7.2 设计思路：
角色和人物的评论接口结构完全一致，可以合并为同一个 Tool，用 entity_type 参数区分。这样减少了 Tool 数量，LLM 只需要学一个接口。
### 7.3 是否需要Auth:
否
### 7.4 Input Schema：
class GetEntityCommentsInput(BaseModel):
    entity_type: Literal["character", "person"]
    entity_id: int
    limit: int = Field(default=20, ge=1, le=100)
### 7.5 Tool 内部逻辑
1. 根据 entity_type 构造路由: character→GET /p1/characters/{id}/comments, person→GET /p1/persons/{id}/comments
2. ⚠️ 注意: 此端点返回纯数组 [{...}]，不是 {data:[], total}，与 subject.comments 不同
3. YAML Comment schema = CommentBase + {replies: CommentBase[]}
   CommentBase: id, mainID, creatorID, relatedID, createdAt, content, state, user (SlimUser), reactions[]
4. 每条 comment 保留: id, content(截断200字（默认值，可选）), createdAt, user.nickname, reactions数量, replies数量(不展开嵌套)
5. 裁剪掉: mainID, creatorID, relatedID, state, relatedPhotoID, user 的 avatar/sign/joinedAt/group
6. 返回: {entity_type, entity_id, comments: [{id, user, content, created_at, reactions_count, reply_count}]}
### 7.6 返回裁剪（对照 YAML Comment schema）
| 保留 | 裁剪掉 |
|---|---|
| id, content(截断200字（默认值，可选）), createdAt | mainID, creatorID, relatedID, state, relatedPhotoID |
| user.nickname | user.id, user.username, user.avatar, user.sign, user.group, user.joinedAt |
| reactions_count (reactions.length) | reactions 完整嵌套（Reaction = {value, users: SimpleUser[]}) |
| replies_count (replies.length) | replies 完整嵌套树 |

---

## 公共裁剪规则速查

以下规则在所有 Tool 中统一应用：

| 原始字段 | 处理方式 | 原因 |
|---|---|---|
| `images` (large/common/medium/small/grid) | **全删** | LLM 无法处理图片 URL，省 ~200B/条 |
| `locked`, `state`, `display`, `accessible` | **全删** | 平台内部状态字段 |
| `rating.count` [10元素数组] | **全删**，只留 score/total/rank | 评分分布对 LLM 噪音大 |
| `desc` / `summary` / `content` | **截断** >300字 → 300字 + `...(已截断)` | 避免单条数据占满上下文 |
| `subject` 嵌套对象 | **只保留摘要**: {id, nameCN, type, score} | 避免递归膨胀 |
| `infobox` [{key, values:[{v,k}]}] | **扁平化**: "key1: v1; key2: v2" | 减少 JSON 嵌套开销 |
| `replies` 嵌套树 | **不展开**: 只保留 replies.length | 避免递归爆炸 |
| `reactions` 数组 | **不展开**: 只保留 reactions.length (反应人数/种类) | YAML Reaction = {value: int, users: SimpleUser[]}，LLM 可能需要知道"有 N 种反应" |
| `avatar`, `sign`, `joinedAt`, `group` | **全删** | 用户资料噪声，对分析无价值 |
| `info` | **保留** | Bangumi 格式化的作品描述，信息密度高 |
| `tags` (BlogEntry/SlimBlogEntry) | **保留** | 字符串数组，帮助 LLM 理解内容主题 |

> ⚠️ **YAML Schema vs 实际 API 返回**: YAML 中有些字段标记为 required 但实际 API 可能返回空值（如 BlogEntry.replies=0 时）。你的 normalize 层要做好 `None` 兜底。
>
> **新增发现（来自 YAML）**:
> - BlogEntry 有 `tags`（字符串数组）和 `views` 字段 → tags 保留、views 裁剪
> - SubjectInterestComment 和 Comment 都有 `reactions` 字段 → 保留计数，不展开嵌套
> - SubjectReview 数据在 `entry` (SlimBlogEntry) 子对象中，SlimBlogEntry 只有 `summary` 而非完整 `content`
> - Topic 有 `creator` (SlimUser) 字段 → 保留 creator.nickname
> - UserStats.subject 可替代 collections 请求做快速画像（无需 Auth）
>
> 原则：LLM 上下文每 1 token 都很贵，只保留用于分析/推理/总结的字段，其他全部裁剪。

---

## Auth 总览

| Tool | 是否需要 Token | 无 Token 行为 |
|---|---|---|
| search_bangumi | ❌ 不需要 | 正常使用 |
| get_calendar | ❌ 不需要 | 正常使用 |
| get_trending | ❌ 不需要 | 正常使用 |
| get_episode_discussion | ⚠️ 可选 | NSFW 条目可能返回 404 |
| get_subject_discussion | ⚠️ 可选 | NSFW 条目可能返回 404 |
| get_entity_comments | ❌ 不需要 | 正常使用 |
| get_blog | ✅ 必须 | 直接 401，Tool 不可用 |
| get_user_profile | ⚠️ 部分需要 | user 基本信息正常；blogs/collections 可能受限 |

> 建议: Agent 启动时检查 token 是否存在，无 token 时自动禁用 get_blog 和 get_user_profile 中的 blogs 子功能。
