根据我对该项目源码的完整追踪，以下是它通过 Bangumi API 抓取并喂给 LLM 的**精确数据字段**。

---

## 一、调用的 API 端点

只有**一个端点**：

```
GET https://api.bgm.tv/v0/subjects
  ?type=2        # 2 = 动画类型
  &year={Y}
  &month={M}
  &sort=rank     # 按排名排序
  &limit={N}
  &offset={M}
```

按季度展开：每个 season 遍历 3 个月窗口，每月按分页拉取（默认 `page_limit=4, per_page=25`，即每季最多 300 条）。

---

## 二、原始 API 返回 → 归一化 → 喂给 LLM

### 2.1 归一化提取的字段 (`normalize_subject()`)

从 Bangumi API 原始 JSON 中提取以下字段：

| 字段 | 来源路径 | 类型 | 进入 LLM? |
|---|---|---|---|
| `subject_id` | `raw["id"]` | int | ✅ 间接（标题列表） |
| `name` | `raw["name"]` | str | ✅ 直接 |
| `name_cn` | `raw["name_cn"]` | str | ✅ 直接 |
| `image_url` | `raw["images"]["common"]` 或 `large/medium/small` | str | ❌ 仅前端展示 |
| `season_label` | 请求参数注入 | str | ✅ 直接 |
| `air_date` | `raw["date"]` 或 `raw["air_date"]` | str | ❌ 仅用于日期过滤 |
| `score` | `raw["rating"]["score"]` | float | ✅ 核心指标 |
| `rating_total` | `raw["rating"]["total"]` | int | ✅ 核心指标 |
| `rank` | `raw["rank"]` | int | ❌ 未直接传入 LLM |
| `collection_total` | `raw["collection"]["total"]` | int | ❌ |
| `collection_wish/doing/done/on_hold/dropped` | `raw["collection"][*]` | int | ❌ |

**关键发现**：collection 系列字段被提取了，但在喂给 LLM 的环节中**并未使用**。

---

### 2.2 实际喂给 LLM 的数据

retrieval specialist 的工具 `fetch_runtime_bangumi_snapshots()` 返回的 JSON 结构：

```json
{
  "requested_seasons": ["2025-spring", "2025-summer"],
  "page_limit": 4,
  "per_page": 25,
  "min_rating_total": 30,
  "snapshots": [
    {
      "season_label": "2025-spring",
      "n_titles": 87,
      "sample_titles": ["进击的巨人 / Attack on Titan", "..."],
      "top_by_rating_total": [
        { "title": "...", "score": 8.5, "rating_total": 12000 }
      ],
      "top_by_score": [
        { "title": "...", "score": 9.1, "rating_total": 8000 }
      ]
    }
  ]
}
```

**所以真正进入 LLM 上下文的是**：
- 每季的**条目数量** `n_titles`
- 每季的**样本标题名**（前5个）
- **按热度 Top 5**：标题名 + score + rating_total
- **按评分 Top 5**：标题名 + score + rating_total

EDA specialist 进一步处理后，传入 LLM 的是结构化的 `CohortSummary`：

| 指标 | 进入 LLM |
|---|---|
| `avg_score`（平均分） | ✅ |
| `avg_rating_total`（平均热度） | ✅ |
| `avg_gap` / `median_gap`（score_z - popularity_z 的 gap） | ✅ |
| `top_positive_gap_titles`（口碑>热度，即"冷门佳作"） | ✅ |
| `top_negative_gap_titles`（热度>口碑，即"德不配位"） | ✅ |

---

## 三、与你项目的对比

| 维度 | 该项目 | 你的项目 |
|---|---|---|
| **API 端点** | `/v0/subjects`（按年月拉条目列表） | 你说的"总的条目 tag, summary" |
| **进入 LLM 的字段** | `name`, `name_cn`, `score`, `rating_total`, 衍生 gap 指标 | 仅 tag + summary |
| **每季拉取量** | 3 个月 × 4 页 × 25 条 = 最多 300 条/季 | ？ |
| **过滤条件** | `min_rating_total >= 30`，必须在季度日期窗口内 | ？ |

---

## 四、结论

该项目的 API 抓取策略是**广度优先、浅层信息**：只拉 `/v0/subjects` 列表端点，不深入单个条目的详情页（`/v0/subjects/{id}`）。它依赖的核心信号只有两个：

> **`score`（评分）和 `rating_total`（评分人数/热度代理）**

其余一切（gap、z-score、cohort 摘要）都是通过 pandas 计算出来的**衍生指标**，不是 API 直接返回的。

如果你目前只能拿到 tag 和 summary，可以考虑增加 `/v0/subjects` 端点来补全 `score` 和 `rating_total` 这两个最核心的定量信号——它们在分析"口碑 vs 热度"时是不可或缺的。

已完成: *Compare with current project* (3/4)
