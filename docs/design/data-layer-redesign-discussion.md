# 数据层重构：讨论过程与决策记录

> 2026-07-23，与 Claude Code 的架构讨论。记录我们如何从"凭直觉的白名单"走到"精细化的分级数据提取"。

---

## 出发：发现的问题

审查基建层时发现，数据从 Bangumi API 到 LLM 经过了**三层各自为政的处理**：

```
API → sanitizer (字段白名单 + 截断) → client (内联 dict 构造，有时绕过 sanitizer)
     → tool (_format_*() → 自然语言 str) → LLM
```

没有一个层对"数据本身"负责：
- sanitizer 用硬编码白名单决定保留/丢弃哪些字段，LLM 看不到被丢弃的字段
- client 部分方法绕过 sanitizer 自己手写 dict，两套平行的清洗逻辑
- tool 用 `_format_*()` 把 dict 转成 emoji + 自然语言文本，LLM 失去排序、过滤、统计能力
- `_compute_subject_signals()` 在 Python 里用硬编码阈值做业务分析，结论塞给 LLM

**核心症状**：为了知道"API 返回什么、LLM 需要什么"，花大量时间肉眼观察 API 返回、猜测"这个字段重不重要"。这是架构问题——白名单模式要求开发者"全知全能"。

---

## 第一轮：提出"透传"方案

**想法**：sanitizer 不再裁剪字段，只做类型转换（魔数→中文标签）+ 文本截断 + 噪音过滤。不认识的字段原样保留。LLM 自行判断哪些字段有用。

**为什么被否决**：实测发现 Bangumi API 返回了大量对 LLM 无意义的数据：
- `images` 4 种尺寸的图片 URL（单条搜索结果的 **53%** 体积）
- `infobox` 59 条双层嵌套数据（~750 tokens），大量与主字段重复（话数 vs eps）
- `metaTags` 与 `info` 字段信息重复
- 角色列表包含 100+ 位灯光师、原画师、特效师——LLM 只需要导演和声优

Bangumi API 是**通用数据平台**接口，为前端页面渲染服务，不是为 AI Agent 设计的。透传会让 LLM 淹没在噪音里。

---

## 第二轮：提出"信号/噪音分析"方案

**想法**：不是"全透传"也不是"老白名单"，而是**基于每个工具的使用场景，逐字段判断它是信号还是噪音**。

和老白名单的区别：

| | 老白名单 | 信号/噪音分析 |
|---|---|---|
| 决策依据 | 开发者直觉 | 工具使用场景（"LLM 用这个工具回答什么问题"） |
| 默认姿态 | 不列出的全丢 | 保留，除非有明确理由丢弃 |
| 丢弃的理由 | 无——没列出来就没了 | 必须有：重复信息 / 噪音 / 体积过大 / 其他工具已提供 |
| 可解释性 | 读代码才知道保留了啥 | 每个字段标注了决策理由 |

---

## 落地方案：分级数据提取 + 方法论

### 三层数据等级

一个条目的信息分为三个等级，不同工具返回不同等级：

| 等级 | 包含内容 | 典型体积 | 所属工具 |
|------|---------|---------|---------|
| **L1 摘要** | id, name, name_cn, type, score, rank | ~12 token/项 | search |
| **L2 详情** | L1 + 评分分布 + 收藏分布 + 简介 + 核心制作人员 + 关联条目 + 标签 | ~800 tokens | detail |
| **L3 完整** | 完整列表数据，limit 参数控制 | 1000+ tokens | characters, discussion, comments |

### 字段决策四分类

- **A. 保留**：轻量 + 核心标识信息
- **B. 保留 + 扁平化**：有价值但嵌套壳浪费 token（如 `rating: {score, rank}` → `score, rank`）
- **C. 保留 + 压缩**：有价值但含冗余（如 `images` 4 种尺寸 → 1 个 URL，`staff` 全部 → 只保留导演/原作/音乐/声优）
- **D. 丢弃**：确定对 LLM 无价值（如 `locked`, `redirect`, 重复的 infobox 条目）

### 方法论文档

详细的方法论在 [bangumi-api-schema-methodology.md](./bangumi-api-schema-methodology.md)，包含：
- 五步流程（场景 → API探查 → 字段决策 → 等级验证 → 注释）
- 实战清单
- Bangumi API 通用模式速查表

---

## 新的整体架构

工具从返回"翻译好的自然语言"变成返回"高信号密度的结构化 dict"：

```
API → sanitizer (信号/噪音分析，逐字段决策)
    → tool (直接 return dict，不调用 _format_*，不调用 _compute_subject_signals)
    → LLM (收到结构化数据，自行分析和呈现)
```

**要改的四层**：

| 层 | 现在 | 改成 |
|---|------|------|
| sanitizers.py | 白名单——"我只保留这些字段" | 信号/噪音过滤器——按方法论逐字段决策 |
| client.py | 部分方法绕过 sanitizer 手写 dict | 全部委托给 sanitizer |
| tools/bgm_tools.py | `_format_*()` 转自然语言，return str | 删除格式化函数，直接 return dict |
| System Prompts | 无数据解读指南 | 加字段说明 + 分析公式参考 |

**不变的部分**：
- HTTP 层、参数校验、数据库、推理拓扑、记忆系统、入口端点

---

## 相关文件

- [bangumi-api-schema-methodology.md](./bangumi-api-schema-methodology.md) — 可复用的方法论指南
- [architecture-review-2026-07-22.md](./architecture-review-2026-07-22.md) — Agent 层宏观评审（发现一~三）
- `clients/sanitizers.py` — 待按方法论逐个改造
- `clients/client.py` — 待统一委托 sanitizer
- `tools/bgm_tools.py` — 待删除 `_format_*()` 和 `_compute_subject_signals()`
