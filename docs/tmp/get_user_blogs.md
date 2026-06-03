# Tool8: 获取用户日志与评论

## 1. 核心意图

获取指定用户在 Bangumi 上发表的日志列表及单篇日志的详细内容与社区评论。用于回答"XX 用户在 Bangumi 上写了什么""大家怎么评论这篇日志"等涉及用户生成内容的问题。

⚠️ **调用前置判断**：用户的个人简介、注册时间等信息属于数据库元数据，**无需调用此工具**。LLM 在拿到日志正文后，可以结合自身知识对日志中提到的作品/人物提供补充分析。

## 2. 聚合的 GET API 路由

1. `[GET] /p1/users/{username}/blogs` （获取用户发表的日志列表）
2. `[GET] /p1/blogs/{entryID}` （获取单篇日志详情，按需调用）
3. `[GET] /p1/blogs/{entryID}/comments` （获取该日志的社区评论）

## 3. Pydantic 入参建议 (Input)

* `username` (str): Bangumi 用户名（即个人主页 URL 中的用户名部分）。如 `"deepseek_jiang"`。
* `limit` (int, 可选，默认 5，最大值 20): 返回日志条数。通过 `Field(le=20)` 硬性限流。
* `include_detail` (bool, 可选，默认 False): 是否展开第一篇日志的正文与评论。为 `True` 时顺序调用 API 2 和 API 3。

## 4. 核心 JSON 字段提取 (Output 契约)

**API 1: `/p1/users/{username}/blogs`**
*(返回日志列表，每条日志仅保留摘要级信息)*

* `username` (str): 用户名（回显，帮助 LLM 确认上下文）。
* `total` (int): 该用户发表的日志总数。
* `blogs` (list[dict]): 日志摘要列表，每个元素仅包含：
  * `entry_id` (int): 日志纯数字 ID（用于后续展开详情）。
  * `title` (str): 日志标题。
  * `summary` (str): 日志摘要或正文前 200 字符（**必须在 Client 层硬截断**）。
  * `created_at` (str): 发布日期（如 `"2025-12-01"`）。
  * `replies` (int): 回复数（反映互动热度）。
* `hint` (str, 条件性): 如果 `total > limit`，追加 `"还有 {N} 篇日志，如需查看请指定更大的 limit"`。

**API 2: `/p1/blogs/{entryID}`**（仅在 `include_detail=True` 时调用）
*(返回单篇日志的完整正文——这是 Bangumi 独有的 UGC 内容)*

* `title` (str): 日志标题。
* `content` (str): 日志正文（BBCode 格式）。**必须在 Client 层清洗为纯文本并硬截断至前 800 字符**，防止超长日志撑爆 Context Window。
* `created_at` (str): 发布日期。
* `related_subjects` (list[str]): 关联条目的名称列表（不含 ID），帮助 LLM 快速理解日志的话题领域。

**API 3: `/p1/blogs/{entryID}/comments`**（仅在 `include_detail=True` 时调用）
*(返回该日志的社区评论——纯 UGC)*

* `comments` (list[str]): 纯文本评论列表。
  * **格式定义**：`"[{floor}楼] {content}"`
  * **噪音过滤**：剔除 `content` 长度小于 4 个字符的短评。
  * **示例**：`"[3楼] 这部作品的分镜确实值得深入分析..."`
* `total` (int): 评论总数。
* `hint` (str, 条件性): 如果评论过多（>30 条），追加 `"... 还有 {N} 条评论未展示"`。

## 5. 业务防御与特殊说明

* **渐进式加载策略**：日志列表不一次性返回全文。默认 `include_detail=False` 仅返回摘要。LLM 根据用户意图（如"帮我看看第一篇写了什么"）再传 `include_detail=True` 展开正文和评论。这避免了不必要的 API 调用和 Token 浪费。
* **BBCode 清洗**：Bangumi 日志正文使用 BBCode 格式（`[b]`、`[url]`、`[img]` 等）。Client 层必须在返回前做 `BBCode → 纯文本` 转换，去除所有格式标记和图片链接。**绝不将原始 BBCode 传给 LLM**。
* **数据截断 (Data Truncation)**：
  * 日志列表摘要：硬截断至前 200 字符。
  * 日志正文：硬截断至前 800 字符。
  * 评论列表：取前 30 条，超出时追加提示。
* **404 异常熔断**：用户不存在时，捕获 404 转译为 `"未找到用户 {username}，请检查用户名拼写是否正确"`。
* **空数据降级**：用户无日志时返回 `{"username": "...", "total": 0, "blogs": [], "hint": "该用户暂未发表日志"}`。
* **认证要求**：这三条 API 无需 Access Token 即可访问公开数据。但若用户设置了隐私保护，可能返回空列表或 403。捕获 403 转译为 `"该用户设置了隐私保护，无法查看其日志"`。
