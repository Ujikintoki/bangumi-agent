# Bangumi API 工具数据 Schema 设计方法论

> 适用场景：新增工具、API 变更、或审查现有工具的返回数据 schema。
> 目标：将"前端展示用 API 返回"转化为"LLM 高信号密度输入"。

## 背景

Bangumi API 是一个通用数据平台接口，不是为 AI Agent 设计的。它的返回结构为前端页面渲染服务——infobox 的 59 条数据用于填充网页信息框、images 的 4 种尺寸用于响应式图片、角色列表包含全部制作人员用于折叠面板。这些对 LLM 大多是噪音。

---

## 第一步：明确场景

写清楚使用场景，要具体到"用户会怎么问"。不要写"获取条目详情"——这是功能描述，不是场景描述。

```
【示例：search_bangumi_subject】
用户说"帮我搜《进击的巨人》""声优花泽香菜配过哪些角色""推荐评分高的科幻动画"
→ LLM 需要快速判断搜索结果中哪个是用户要的，然后拿到 ID 进行下一步

【示例：get_bangumi_subject_detail】
用户说"这部番评分怎么样""导演是谁""和同类比口碑如何""有没有续作"
→ LLM 需要评分/排名/分布、核心制作人员、关联条目、标签题材
```

---

## 第二步：探查 API 真实返回

用 curl 直接调用，不要猜。

```bash
TOKEN=$(grep BANGUMI_ACCESS_TOKEN .env | cut -d= -f2 | tr -d '[:space:]')

# 找一个热门条目（数据最全）作为样本
curl -s -H "Authorization: Bearer $TOKEN" "https://next.bgm.tv/p1/subjects/265" | python3 -m json.tool

# 如果 p1 报错，试 v0
curl -s "https://api.bgm.tv/v0/subjects/265" | python3 -m json.tool
```

用脚本分析每个字段的体积和内容：

```python
import json, sys
raw = json.load(sys.stdin)

total_s = json.dumps(raw, ensure_ascii=False)
print(f"全量 token: ~{len(total_s)//4}\n")

for k, v in raw.items():
    s = json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        print(f"  {k}: list[{len(v)}], ~{len(s)//4} tokens")
        if v: print(f"    首元素: {json.dumps(v[0], ensure_ascii=False)[:200]}")
    elif isinstance(v, dict):
        print(f"  {k}: dict[{len(v)}], ~{len(s)//4} tokens")
    elif isinstance(v, str):
        print(f"  {k}: str[{len(v)}], ~{len(s)//4} tokens")
    else:
        print(f"  {k}: {type(v).__name__} = {v}")
```

### 选样本原则

- 挑一个**热门作品**（数据最全：EVA、巨人、化物语）
- 挑一个**冷门作品**（防止假设热门才有某字段）
- 挑一个**不同类型**（游戏/书籍的字段可能与动画不同）

---

## 第三步：逐字段做决策

对每个字段，归入以下四类之一：

### A. 保留（直接透传）

条件：轻量 + 核心标识信息，LLM 回答问题时几乎一定需要。

| 信号示例 | 理由 |
|---------|------|
| id | 工具链传递必需 |
| name / name_cn | 核心标识 |
| score / rank | 用户高频询问 |
| type（转中文后） | 筛选和推荐必需 |

### B. 保留 + 扁平化

条件：有价值 + 但 API 嵌套结构浪费了大量 token 在壳上。

| 信号示例 | 处理方式 |
|---------|---------|
| `rating: {score, rank, total, count: [...]}` → `score, rank, total_ratings, rating_count` | 去掉中间层 |
| `collection: {"1": N, "2": N, ...}` → `collection: {"想看": N, "看过": N, ...}` | 魔数 key → 中文 |
| `infobox: [{key, values: [{v}]}]` → `infobox: {key: value}` | 列表→字典 |

### C. 保留 + 压缩

条件：有价值 + 体积较大 + 包含 LLM 不需要的冗余。

| 信号示例 | 处理方式 |
|---------|---------|
| tags 30 条全保留 | 30 条 × ~8 token = ~240 tokens，可控，全保留 |
| images 4 种尺寸 → 1 个 URL | common 尺寸足够，其余三种无意义 |
| staff 列表 → 按 role 过滤 | 保留导演/原作/音乐/声优，丢弃灯光师/摄影/原画师 |
| summary → 截断 500 字 | 太长淹没关键数据，500 字够理解作品 |
| 角色列表 → 只保留角色名+声优名+ID | 背景故事是 get_character_detail 的职责 |

### D. 丢弃

条件：确定对 LLM 无价值。

| 噪音示例 | 理由 |
|---------|------|
| images 的所有尺寸（搜索场景） | 搜索结果中不需要图片 URL |
| locked, redirect | 内部管理字段 |
| 与主字段重复的 infobox 条目 | 话数 vs eps、中文名 vs name_cn |
| metaTags（与 info 重复时） | 保留 info（更结构化） |

### 压缩 vs 丢弃的判断标准

```
该字段的每一项体积 × 项数（最坏情况）< 200 tokens ？
  → 是 → 全保留（无害）

  → 否 →
    每一项都是同质的吗（如 tags: 每条都是 {name, count}）？
      → 是 → 可以全保留（LLM 能批量处理）

      → 否 →
        每一项的复杂度差异大吗（如 characters: 有的是主角有长背景，有的是路人）？
          → 是 → 只保留核心信息（name + role + cv），细节用专项工具获取
```

---

## 第四步：验证数据等级

一个条目的信息分为三个等级：

| 等级 | 密度 | 包含内容 | 典型体积 | 所属工具 |
|------|------|---------|---------|---------|
| **L1 摘要** | 极高 | id, name, name_cn, type, score, rank | ~50 token/项 | search |
| **L2 详情** | 高 | L1 + 评分分布 + 收藏分布 + 简介 + 核心制作人员 + 关联条目 + 标签 + 独有 infobox | ~800-1200 tokens | detail |
| **L3 完整** | 中 | 完整列表数据，limit 参数控制 | 1000+ tokens | characters, discussion, comments |

交叉验证规则：
- **搜索工具**返回 L1 即可。如果往搜索结果里塞 tags/简介/评分分布 → 等级错误。
- **详情工具**返回 L2。如果往 detail 里塞完整角色背景或 100+ 条评论 → 等级错误，让 LLM 调专项工具。
- **数据只在需要时才给**。不要预判"LLM 可能需要"而塞入数据。

---

## 第五步：编写 schema 注释

每个字段在代码中标注决策理由，方便未来维护：

```python
def sanitize_subject_detail(raw: dict) -> dict:
    """将 subject detail API 返回转为 LLM 高密度输入。
    
    保留理由：
    - 核心标识：id, name, name_cn
    - 评分相关：score, rank, total_ratings, rating_count（扁平化自 rating.*）
    - 收藏分布：collection（key 从数字转为中文）
    - 核心制作人员：staff 中 role 为导演/原作/音乐/主要声优的条目
    - 关联条目：relations 全部（体积可控）
    - 标签：tags 全部（30 条 × ~8 token）
    
    压缩理由：
    - infobox：扁平化（{key: [{v}]} → {key: value}），去掉和主字段重复的条目
    - images：4 种尺寸 → 1 个 URL（common）
    
    丢弃理由：
    - locked, redirect: 管理字段
    - staff 中 role 为原画师/摄影/特效等的条目: 非核心
    """
```

---

## 实战清单

每审查一个工具，按此清单走：

- [ ] 写了使用场景（用户会怎么问）
- [ ] curl 跑了真实 API 返回（热门 + 冷门样本）
- [ ] 每个字段标注了体积和语义
- [ ] 每个字段做了 A/B/C/D 分类决策，有理由
- [ ] 验证了数据等级（L1/L2/L3 没有串位）
- [ ] 写了 schema 注释（字段级决策理由）

---

## 通用 Bangumi API 常见模式

基于实测，Bangumi API 的返回结构有一些通用规律：

| API 模式 | 问题 | 处理方式 |
|---------|------|---------|
| `rating: {score, rank, total, count}` | 嵌套壳占 token | 全部扁平化到顶层 |
| `collection: {"1": N, "2": N, ...}` | 魔数 key | key 转为中文标签 |
| `infobox: [{key, values: [{v}]}]` | 双层嵌套，50+ 条 | 扁平化为 `{key: value}`，去重 |
| `images: {large, common, medium, small}` | 4 种尺寸 | 只保留 common |
| `tags: [{name, count}] × 30` | 固定 30 条，体积可控 | 全保留 |
| `metaTags: [str]` vs `info: str` | 信息重复 | 保留 info，丢弃 metaTags |
| `name` + `nameCN` 风格混用 | 驼峰 vs 下划线 | 统一转为 `name_cn` |
| `locked, redirect, nsfw` | 管理/过滤字段 | 丢弃 locked/redirect，nsfw 保留 |
| 列表/子对象可能为 null | 需兜底 | 所有列表加 `or []`，子对象加 `or {}` |

## 实测案例：search_bangumi_subject

应用本方法论，搜索工具的字段分析如下：

**场景**：用户搜索作品/角色/人物名称 → LLM 需要快速判断哪个是目标，拿到 ID。

**API 返回** (p1/search/subjects, limit=5):
- 全量 ~830 tokens/5 条，单条 ~166 tokens
- 最大噪音源：`images` 占 88t/条（53%），4 种尺寸的图片 URL

**逐字段决策**：

| 字段 | 体积 | 决策 | 理由 |
|------|------|------|------|
| id | 1t | 保留 | 工具链必需 |
| name | 1t | 保留 | 核心标识 |
| nameCN | 1t | 保留 → name_cn | 统一命名 |
| type | <1t | 保留+转换 | 魔数→中文 |
| info | 7t | 保留 | 消歧关键（"139话 / 諫山創 / 講談社"） |
| rating.score | (嵌套) | 扁平化 | 评分，用户高频询问 |
| rating.rank | (嵌套) | 扁平化 | 排名，用户高频询问 |
| images | 88t | **丢弃** | 对 LLM 无意义，单条 53% 体积 |
| metaTags | 7t | 丢弃 | 与 info + type 重复 |
| locked | 1t | 丢弃 | 管理字段 |
| nsfw | 1t | 丢弃 | subject 无 nsfw 概念 |

**压缩后**：~12t/条（id + name + name_cn + type + info + score + rank），5 条 ≈ 60t（vs 830t）。
