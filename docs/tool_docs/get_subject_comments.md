# Tool5: 获取条目的评论
## 1. 核心意图
[大模型调用此工具的意图]
获取条目吐槽

## 2. 聚合的 GET API 路由
1. `[GET] /p1/subjects/{subjectID}/comments` （获取条目吐槽）

## 3. Pydantic 入参建议 (Input)
[大模型调用此工具时需要传入的参数。]
* `subjectID` (int): 条目ID

## 4. 核心 JSON 字段提取 (Output 契约)
**API 1: `/p1/subjects/{subjectID}/comments`**
*(极致瘦身与降噪方案：完全抛弃 JSON 对象，过滤无价值短评，格式化为纯文本列表)*

* `comments` (list[str]): 纯文本吐槽列表。
  * **格式定义**：`"[x星] {comment_text}"` （如果 rate 为 0，则显示为 `[未评分]`）。
  * **示例**：
    * `"[8星] 虽然都是现实中绝对不会遇到的神人，但故事却意外的有真实感..."`
    * `"[10星] 能够家里蹲，本身就是一件奢侈的事..."`

## 5. 业务防御与特殊说明
* **噪音过滤 (Noise Filtering)**：
  通过 Pydantic 暴露一个 `limit` 参数（默认 50，最大 100），只取前 N 条。，必须在 Client 层清洗掉对大模型毫无分析价值的废话，节省 Token。
  * 规则 1：剔除 `comment` 长度小于 4 个字符的评论（过滤掉“补标”、“神作”、“标记”等）。
  * 规则 2：剔除完全由日期、纯数字或标点符号组成的评论（如 "2025-01-28"）。
* **格式极简压缩**：
  直接丢弃 `user`, `id`, `updatedAt`, `type`。不要返回包含 dict 的 list，而是直接拼装成包含 N 个格式化 string 的 list。
