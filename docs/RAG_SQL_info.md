以下是三类实体完整的数据写入全景：

---

## RagEntity 表结构（共 7 列，三类实体共用）

| 列名 | 类型 | Subject | Character | Person |
|---|---|---|---|---|
| `id` | `str` (PK) | `"subject_10"` | `"character_5"` | `"person_3"` |
| `entity_type` | `str` (索引) | `"subject"` | `"character"` | `"person"` |
| `name` | `str` (索引) | `item["name"]` | `item["name"]` | `item["name"]` |
| `name_cn` | `Optional[str]` | `item["name_cn"]` | `item["name_cn"]` | `item["name_cn"]` |
| `chunk_text` | `str` | `"[作品名] 进击的巨人。{原文}"` | `"[角色] 艾伦，出自《进击的巨人》。{原文}"` | `"[人物] 梶裕贵。{原文}"` |
| `embedding` | `Vector(2048)` | 智谱 embedding-3 对 `chunk_text` 向量化 | 同左 | 同左 |
| `meta_info` | `JSONB` | `SubjectMeta` | `CharacterMeta` | `PersonMeta` |

---

## meta_info 详情

### Subject → `SubjectMeta`

| 字段 | 类型 | 来源 |
|---|---|---|
| `score` | `float` | `item["score"]` |
| `rating_total` | `int` | `item["rating_total"]` |
| `date` | `str\|None` | `item["date"]` |
| `eps` | `int` | `item["eps"]` |
| `nsfw` | `bool` | `item["nsfw"]` |
| `tags` | `list[dict]` | `item["tags"]`（原始 `[{name, count}]` 格式） |

### Character → `CharacterMeta`

| 字段 | 类型 | 来源 |
|---|---|---|
| `role` | `int` | `item["role"]` |
| `collects` | `int` | `item["collects"]` |
| `casts` | `list[CharacterCast]` | `item["casts_raw"]` → 内存重排+去重 → Top 10 |

其中 `CharacterCast` 子结构：

| 字段 | 类型 |
|---|---|
| `subject_id` | `str`（`"subject_xxx"`） |
| `subject_name` | `str` |
| `person_id` | `str\|None`（`"person_xxx"`） |
| `person_name` | `str\|None` |
| `role_type` | `int`（1=主角/2=配角/3=客串） |

### Person → `PersonMeta`

| 字段 | 类型 | 来源 |
|---|---|---|
| `career` | `str` | `item["career"]` |
| `type` | `int` | `item["type"]` |
| `collects` | `int` | `item["collects"]` |
| `works` | `list[PersonWork]` | `item["works_raw"]` → 内存重排+去重 → Top 10 |

其中 `PersonWork` 子结构：

| 字段 | 类型 |
|---|---|
| `subject_id` | `str` |
| `subject_name` | `str` |
| `character_id` | `str\|None`（`"character_xxx"`） |
| `character_name` | `str\|None` |
| `role_type` | `int` |

---

## 写入逻辑流程

```
输入 dict 列表
      │
      ├─ ① 拼接语义前缀 → chunk_text
      │     Subject: "[作品名] {name_cn}。{chunk}"
      │     Character: "[角色] {name_cn}，出自《{subject_name}》。{chunk}"
      │     Person: "[人物] {name_cn}。{chunk}"
      │
      ├─ ② 批量调用智谱 embedding-3 → 获取 2048 维向量
      │
      ├─ ③ [仅 Character/Person] 关联边内存重排
      │     _rerank_casts / _rerank_works:
      │       查本地 RagEntity → 按 rating_total 降序 → seen_subjects 去重 → [:10]
      │
      ├─ ④ Pydantic Meta 契约校验
      │     SubjectMeta / CharacterMeta / PersonMeta.model_dump()
      │
      └─ ⑤ RagEntity 批量 INSERT → COMMIT
```

**关键点**：所有实体类型共享同一张 `rag_entities` 表，`meta_info` JSONB 承担了实体特有的差异化字段，列级字段（`id`、`name`、`chunk_text`、`embedding`）保证统一的检索能力。

已完成: *Test the deduplication logic with sample data.* (3/3)
