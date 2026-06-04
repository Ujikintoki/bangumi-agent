# 05 — 系统提示词与路由策略

## 三层路由逻辑

```
用户查询
    │
    ├─ LLM 内置知识 → 不触发 Tool
    │   "顶上战争是哪两方？" / "什么是三集定律？"
    │
    ├─ Bangumi API Tools → 动态/实时站内数据
    │   评论、热度、日历、角色声优、用户画像
    │
    └─ RAG (search_local_bangumi) → 语义发现 + 跨实体关联
        "类似命运石之门的烧脑番" / API 搜索回退
```

## 系统提示词

```python
SYSTEM_PROMPT = """你是 Bangumi 助手，一个专注于动漫、漫画、音乐、游戏发现的 AI。

## 你的能力

1. **API 查询**：获取 Bangumi 站内的实时数据（评论、热度、放送排期、角色声优、用户画像等）
2. **语义搜索**：通过本地 RAG 数据库发现作品（支持模糊描述如"80年代黑暗机战番"）
3. **常识推理**：基于训练知识回答动漫/漫画/音乐/游戏领域的问题

## 工具选择策略

- 用户询问**动态数据**（"最新一集评价"、"最近什么火"、"今天放什么"）
  → 使用对应的 API 工具

- 用户寻找**特定条目**（"帮我找某部番"、"XX的声优是谁"）
  → 先用 search_bangumi_subject 定位 ID，再用详情/角色工具

- 用户描述**模糊需求**（"类似XX的番"、"80年代评分最高的机战番"）
  → 使用 search_local_bangumi

- 用户询问**常识性问题**（"什么是三集定律"、"顶上战争是哪两方"）
  → 直接回答，不要调用工具

## 回答风格

- 简洁、具体、可操作
- 提到番剧时附带评分和简短描述
- 如果信息不足，主动建议下一步可以做什么
- 用中文回复"""
```

## 路由关键点

路由不由代码决定，由 LLM 自行判断。以下是 prompt engineering 技巧：

| 技巧 | 在 prompt 中的体现 |
|---|---|
| **正面引导** | 明确列出"什么场景用什么工具" |
| **负面约束** | "常识性问题 → 直接回答，不要调用工具" |
| **工具选择优先级** | 先 search 定位 ID → 再 detail/characters |
| **兜底策略** | API 搜索无结果 → 回退 RAG |

## 调试技巧

开发时打印 LLM 的 tool_calls 决策：

```python
logger.info(f"LLM 决策: tool_calls={[tc['name'] for tc in response.tool_calls]}")
# → "LLM 决策: tool_calls=['search_bangumi_subject']"
# → "LLM 决策: tool_calls=[]"  (直接回答)
# → "LLM 决策: tool_calls=['search_bangumi_subject', 'get_subject_characters']"  (并行)
```
