# Tool6: 获取角色吐槽
## 1. 核心意图
[大模型调用此工具的意图]
获取某个角色的社区吐槽箱，用于分析观众对该角色的评价、人气风向或名场面讨论。**注意**：角色的基本信息（如声优、所属作品）属于 LLM 常识，无需通过此工具获取——此工具仅提供 LLM 不知道的用户生成内容。

## 2. 聚合的 GET API 路由
1. `[GET] /p1/characters/{characterID}/comments` （获取角色吐槽箱）

## 3. Pydantic 入参建议 (Input)
[大模型调用此工具时需要传入的参数。]
* `character_id` (int): 角色纯数字 ID。通常由上游搜索工具透传。
* `limit` (int, 可选，默认 20，最大值 50): 返回吐槽条数。通过 `Field(le=50)` 硬性限流。

## 4. 核心 JSON 字段提取 (Output 契约)
**API 1: `/p1/characters/{characterID}/comments`**
*(极致瘦身：抛弃 user、id、createdAt、mainID、relatedID、state 等全部元数据，只保留吐槽内容和热度)*

* `comments` (list[str]): 纯文本吐槽列表。
  * **格式定义**：`"[{likes}赞] {content}"`
  * **likes 计算**：`reactions` 数组中所有 `reaction.users` 长度的总和（而非只取某一类 reaction）。
  * **reply 处理**（可选）：若 `replies` 非空，取第一条回复内容追加：`"[{likes}赞] {content} —— 回复: {reply_content}"`。超过一条回复则截断。
  * **示例**：`"[45赞] 艾伦的角色弧光太震撼了，从热血少年到灭世者"`

## 5. 业务防御与特殊说明
* **噪音过滤 (Noise Filtering)**：
  * 规则 1：剔除 `comment` 长度小于 4 个字符的短评（过滤 "好帅""可爱""神" 等无价值内容）。
  * 规则 2：剔除纯数字、纯日期、纯标点符号组成的评论。
* **数据截断**：API 返回的完整列表可能非常长，通过 `limit` 参数控制（默认 20，最大 50）。如果实际评论数超过 limit，在返回末尾追加 `"... 还有 {N} 条评论，如需更多请指定更大的 limit"`。
* **角色名提示**：如果 Client 层已知角色名，在返回 JSON 顶部增加 `"character": "{角色名}"` 字段，帮助 LLM 快速确认上下文。
* **404 异常熔断**：捕获 404，转译为 `"未找到 ID 为 {characterID} 的角色，可能已被删除或合并"`。
* **空评论处理**：如果角色暂无吐槽，返回 `{"character": "...", "comments": [], "hint": "该角色暂无社区吐槽"}`。
* **通用异常熔断**：捕获超时、502、503 等异常，转译为自然语言，绝不允许抛出 Exception 导致状态机崩溃。
