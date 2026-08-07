# RAG Eval 管线 — 会话交接文档

> 最后更新: 2026-08-07

## 1. 核心任务目标

建立 RAG 检索的 **evidence-based eval 管线**，用指标数据评估检索质量，而非凭感觉判断。

**三步走：**
1. ~~全量数据摄入~~（已完成）
2. ~~搭建 eval 管线~~（代码完成，等待人工标注）
3. **计算指标 + 出报告**（当前阻塞点：人工标注）

---

## 2. 技术背景

### 数据库

`rag_entities` 表有 1500 条实体（2026-08-06 灌入）。

| 类型 | 头部（rank 选取） | 短尾随机 | 合计 |
|------|------------------|---------|------|
| Subject 动漫 | 800 | 100 | 900 |
| Subject 书籍 | 100 | 30 | 130 |
| Character | 300 | 50 | 350 |
| Person | 100 | 20 | 120 |

随机种子 `TAIL_SEED=42`。

### 表结构关键字段

- `embed_text` → embedding-2 向量化 → `embedding Vector(1024)`
- `rag_output` → 预构建 JSON dict（对齐 API sanitizer schema），检索时 `json.loads()` 零转换
- `meta_info` → JSONB（`tags[]`, `score`, `year`, `career[]`, `collects` 等）

### Eval 设计思路

两种 Ground Truth 来源：

| 来源 | 原理 | Recall 精度 | 数量 |
|------|------|------------|------|
| **自动 GT** | 从 `meta_info` JSONB 查 tag/year/score/career，SQL 直接出完整答案 | 精确 | 25 条 |
| **池化标注** | 多路检索池化 top-20 → 人工判 relevance → 相关实体 = GT | 近似（可能有遗漏） | 10 条 |

5 个指标：**Recall@K, Precision@K, MRR, NDCG@K, Hit Rate@K**（@1, @3, @5, @10）。

**为什么不用 RAGAS：** RAGAS 评估的是生成质量（faithfulness, answer relevancy），我们这个 eval 是纯检索质量评估，没有生成环节。

---

## 3. 已完成的文件修改

### 新增文件

```
scripts/eval/
├── evaluate.py                  # 评测脚本: --build 生成模板 / --evaluate 算指标
├── ground_truth_auto.json       # 25 条自动 GT（已生成，无需人工）
└── pooled_annotate.json         # 10 条语义查询 → 177 个候选待标注 ← 你需要改这个
```

### 修改文件

| 文件 | 改动 |
|------|------|
| `scripts/discover_corpus.py` | 目标数量调整：动漫 1000、书籍 200、角色 400、人物 150 |
| `scripts/ingest_corpus.py` | 新增头部/尾部拆分逻辑（`HEAD_SIZES` + `TAIL_DISTRIBUTION` + 随机采样） |
| `rag/ingestion.py` | embed_text / rag_output 分离；新增 `_build_*_embed_text()` 和 `_build_*_rag_dict()` |
| `rag/enricher.py` | BBcode 清洗；CharacterEnricher/PersonEnricher 补全 infobox + comment |
| `rag/retriever.py` | `RagSearchResult` 新增 `rag_output` / `nsfw` 字段 |
| `database/rag_tables.py` | `RagEntity` 列重命名 `chunk_text` → `rag_output` |
| `tools/bgm_tools.py` | `search_local_bangumi` 返回类型 `str` → `dict` |
| `rag/example.sql` | 用真实示例数据重写 |

### 自动 GT 的 25 条查询

```
tag (12): 芳文社, CloverWorks, 京都动画, ufotable, Production.I.G, TRIGGER,
          动画工房, 虚渊玄, MADHouse, BONES, P.A.WORKS, 吉卜力
year (2): 2023年的动画, 2022年的动画
score (1): 高分神作 (≥8.8)
career (3): 声优, 歌手艺人, 动画制作人
exact (7): 孤独摇滚, 進撃の巨人, 命运石之门, 化物語, CLANNAD, 牧瀬紅莉栖, 花澤香菜
```

GT 大小范围：1–58（无空 GT，无超大 GT）。

---

## 4. 当前阻塞：人工标注

### 你需要做什么

打开 **`scripts/eval/pooled_annotate.json`**，有 10 条语义查询，每条下有 ~16–20 个候选实体。

每个候选实体有：
```json
{
  "entity_id": "subject_428735",
  "name": "BanG Dream! It's MyGO!!!!!",
  "name_cn": "",
  "entity_type": "subject",
  "snippet": "「能一辈子和我搞乐队吗？」...",
  "relevant": null   ← 改这里
}
```

**把 `null` 改为：**
- **`true`** — 搜这个问题时，这个实体*应该*出现在结果中
- **`false`** — 不应该出现

每条候选有名称 + 简介片段（snippet），帮你判断，不需要额外查资料。

### 10 条待标注查询

| ID | 查询 | 候选数 | 类型 |
|----|------|--------|------|
| p01 | 关于音乐乐队的百合动画 | 18 | subject |
| p02 | 异世界转生冒险 | 16 | subject |
| p03 | 机甲战斗科幻 | 15 | subject |
| p04 | 悬疑推理惊悚 | 17 | subject |
| p05 | 温馨轻松的日常故事 | 19 | subject |
| p06 | 制作精良的剧场版动画电影 | 20 | subject |
| p07 | 傲娇双马尾美少女 | 20 | character |
| p08 | 帅气冷酷的男性角色 | 19 | character |
| p09 | 知名动画导演 | 16 | person |
| p10 | 配乐出色的作曲家 | 17 | person |

共 **177 个候选实体**需要标注。

---

## 5. 标注完成后的下一步

```bash
source .venv/bin/activate
python scripts/eval/evaluate.py --evaluate
```

会输出：
1. 全局指标（MRR, Hit@K, Precision@K, Recall@K, NDCG@K）
2. 按 category 细分（tag / year / score / career / exact / semantic）
3. 按 GT 来源细分（auto 精确 vs human 近似）
4. 详细 JSON 报告 → `ground_truth_merged.json`

### 报告解读指引

| 指标 | 好 | 差 | 说明 |
|------|----|----|------|
| Recall@5 | >0.5 | <0.2 | 正确答案能找到多少 |
| Precision@5 | >0.6 | <0.3 | 返回结果质量 |
| MRR | >0.7 | <0.3 | 正确答案排名 |
| NDCG@5 | >0.6 | <0.3 | 排序质量 |
| Hit@5 | >0.8 | <0.4 | 至少命中一个的 query 比例 |

- **auto 组 Recall 是精确的**（GT 完整），human 组 Recall 是近似下界
- **exact 类**预期最高（名称精确匹配），**semantic 类**预期最低（最难）
- 如果某个 category 明显低，去检查 `embed_text` 构造或检索参数

---

## 6. 关键命令速查

```bash
# 激活虚拟环境
source .venv/bin/activate

# 重新采集 ID（如需要）
python scripts/discover_corpus.py

# 重新灌入数据库
python scripts/ingest_corpus.py --clear

# 重新生成 eval 模板（会覆盖 pooled_annotate.json！）
python scripts/eval/evaluate.py --build

# 计算指标（标注完成后）
python scripts/eval/evaluate.py --evaluate

# 快速检索测试
python -c "
from rag.retriever import RagEntityRetriever
from database.engine import engine
from core.config import get_settings
s = get_settings()
r = RagEntityRetriever(engine=engine, zhipu_api_key=s.ZHIPU_API_KEY)
results = r.hybrid_search('孤独摇滚', limit=5)
for x in results: print(x.entity_id, x.name, x.final_score)
"
```
