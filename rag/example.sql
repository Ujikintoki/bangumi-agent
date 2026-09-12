-- ============================================================================
-- BGM Agent — RAG 检索数据库表结构 + 完整教学注释
-- ============================================================================
--
-- 文件路径:   rag/example.sql
-- 对应 ORM:   database/rag_tables.py → class RagEntity(SQLModel)
-- 对应摄入:   rag/ingestion.py → RagEntityIngestor
-- 对应检索:   rag/retriever.py → RagEntityRetriever.hybrid_search()
-- 最后更新:  2026-08-09（新增 subject_type 列 + 索引）
--
-- ============================================================================
-- 〇、给后端初学者的前置概念（5 分钟读懂）
-- ============================================================================
--
-- 这个项目到底在做什么？
-- ─────────────────────
-- BGM Agent 是部署在 bangumi.tv（一个动漫/书籍/音乐数据库网站）的 AI 聊天机器人。
-- 当用户问"最近有什么好看的轻百合动画？"时，不能每次都去调用外部 API（太慢、有限流），
-- 所以我们需要把网站里最常用的数据预先存到自己的数据库里，快速检索。
--
-- 存下来的数据大概长这样：
--   动漫（Subject）：进击的巨人、孤独摇滚、CLANNAD …     ≈1000 条
--   角色（Character）：牧濑红莉栖、千反田爱瑠 …          ≈350 条
--   人物（Person）：花泽香菜、虚渊玄、新房昭之 …         ≈120 条
--
-- 这么多数据，用户问一句"推荐些芳文社的萌系日常番"，怎么在两秒钟内找到最匹配的？
-- 答案是 RAG（Retrieval-Augmented Generation，检索增强生成）：
--   Step 1: 把每条数据变成一串数字（向量），存进数据库
--   Step 2: 用户的问题也变成一串数字
--   Step 3: 在数据库里找到"数字串最相似的几条" → 这就是答案
--
--
-- 为什么只有一个表 rag_entities，而不是 subject / character / person 三张表？
-- ─────────────────
-- 这叫"单表多态"（Single Table Polymorphism）。原因是：
--   1. 这三类实体在检索时经常一起出现（"花泽香菜配过哪些动画女主角？"）
--   2. 它们的核心字段 90% 相同（都有名称、向量、热度），只是 meta_info 里不同
--   3. 一张表的向量索引比三张表分别建索引快得多
--   4. 跨类型查询不需要 JOIN，性能更好
--
-- 用 entity_type 列区分类型（'subject' / 'character' / 'person'），
-- 用 subject_type 列进一步区分动漫(2)和书籍(1)。
--
--
-- 什么是向量（embedding）？
-- ─────────────────
-- 把一段文字（如"进击的巨人 战斗 热血 WIT STUDIO 2013 8.5分"）送进 AI 模型，
-- 模型返回 1024 个浮点数，如 [0.0156, -0.0234, 0.0089, ...]。
-- 这段数字就叫"向量"。两段语义相近的文字，它们的向量在数学上也很"近"。
--
-- pgvector 是 PostgreSQL 的向量扩展，可以高效地对这些 1024 维向量做"最近邻搜索"。
--
--
-- 什么是 JSONB？
-- ─────────────────
-- PostgreSQL 的一种列类型，可以存任意 JSON 数据，还能对其内部字段建索引、做查询。
-- 比存纯文本 VARCHAR 的优势：可以直接写 SQL 查 JSON 内部的某个字段，如
--   WHERE meta_info->>'year' = '2023'       -- 查年份
--   WHERE meta_info->'tags' @> '[{"name":"芳文社"}]'  -- 查标签
--
--
-- 什么是索引？
-- ─────────────────
-- 索引就像书的目录。没有索引时，数据库要逐行扫描（全表扫描），1000 行还能忍，
-- 10 万行就崩了。加索引后，数据库"直接翻目录"找到目标，速度提升 100~10000 倍。
-- 代价是写入会慢一点、占更多磁盘——但对我们的场景（读多写少）完全值得。
--
-- ============================================================================
-- 一、建表 DDL（11 列，2026-08-09 最新版）
-- ============================================================================
--
-- 前置条件：PostgreSQL 需安装这两个扩展
--   CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector，向量存储和检索
--   CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trigram 模糊匹配，加速 LIKE/ILIKE
--
-- 实际建表不由本 SQL 执行，而是由 database/engine.py 的 init_db() 通过
-- SQLModel.create_all() 自动完成。这里的 DDL 供你理解表结构用。

DROP TABLE IF EXISTS rag_entities;

CREATE TABLE rag_entities (

    -- ── 主键：全局唯一 ID ────────────────────────────────────────────
    -- 不是纯数字，而是加了前缀，防止不同类实体的 ID 撞车。
    -- 比如 Bangumi 上 subject id=51 是 CLANNAD，character id=51 是别的角色，
    -- 如果只存 51，就无法区分。所以我们存 "subject_51" / "character_12393"。
    id              VARCHAR PRIMARY KEY,

    -- ── 实体类型标签 ──────────────────────────────────────────────────
    -- 'subject'   = 动漫/书籍条目（一部作品）
    -- 'character' = 角色（作品里出现的虚构人物，如"牧濑红莉栖"）
    -- 'person'    = 现实人物（声优、导演、作者等，如"花泽香菜"）
    entity_type     VARCHAR NOT NULL,

    -- ── 名称 ──────────────────────────────────────────────────────────
    -- name:    原文名称（日文或英文），如 "ぼっち・ざ・ろっく！"
    -- name_cn: 中文名称，如 "孤独摇滚！"。可能为空（冷门作品可能没有中文译名）
    name            VARCHAR NOT NULL,
    name_cn         VARCHAR,

    -- ── 安全护栏 ──────────────────────────────────────────────────────
    -- 是否 R18 内容。默认 false。
    -- 检索时默认排除 nsfw=true 的行（WHERE nsfw=false），除非用户明确要求。
    nsfw            BOOLEAN NOT NULL DEFAULT false,

    -- ── Subject 子类型（2026-08-09 新增）─────────────────────────────
    -- 仅 entity_type='subject' 时有值，区分动画和书籍。
    --   1 = 书籍（漫画、轻小说、画集）
    --   2 = 动画（TV 动画、剧场版、OVA、Web 动画）
    --   NULL = 不是 subject（character 或 person，没有子类型概念）
    --
    -- 为什么要加这一列？
    --   以前所有 subject 混在一起，检索"海贼王"时同时返回动画版和漫画版，
    --   82 个同名碰撞导致评测指标失真、标注时无法判断 relevance。
    --   现在检索时加 WHERE subject_type=2 就能只看动画版的结果。
    subject_type    INTEGER,

    -- ── 热度信号 ──────────────────────────────────────────────────────
    -- subject → 评分人数（rating_total），越大越热门
    -- character / person → 收藏数（collects）
    --
    -- 用于检索排序：语义相近的结果里，热门的排前面。
    -- 不是简单粗暴地按热度排——那样会淹没冷门佳作——而是先按"语义距离"分梯队，
    -- 梯队内再按热度排。详见 retriever.py 的"多态阶梯分桶排序"。
    popularity      INTEGER NOT NULL DEFAULT 0,

    -- ── 预构建返回数据 ────────────────────────────────────────────────
    -- 存的是一段 JSON 字符串，结构对齐了 Bangumi API 的返回格式。
    -- 举例：检索命中"孤独摇滚！"后，直接把 rag_output 这个 JSON 字符串
    -- json.loads 解析成 dict 返回给调用方，零额外处理。
    --
    -- 为什么预先拼好 JSON 而不是用时再拼？
    --   速度。检索本身已经够重了（要算 1024 维向量的余弦距离），
    --   如果每条结果还要重新构造返回格式、查 meta_info 里的字段，
    --   响应时间会翻倍。预构建 → 检索 → 直接返回，一气呵成。
    rag_output      VARCHAR NOT NULL,

    -- ── 嵌入用文本 ────────────────────────────────────────────────────
    -- 一段关键词密集的文本，仅供 embedding 向量化使用，不直接展示给用户。
    -- 格式：名称 → 标签 → 年代 → 平台 → 评分 → 简介首句
    -- 如："孤独摇滚！ 芳文社 音乐 轻百合 CloverWorks 2022 动画 8.4分 作为网络吉他手..."
    --
    -- 和 rag_output 的区别：
    --   rag_output = 给用户看的结构化数据（含完整摘要、评分、标签列表等）
    --   embed_text = 给 AI 模型向量化用的精简文本（关键词为主，≤400 字符）
    embed_text      VARCHAR,

    -- ── 向量嵌入（核心！）─────────────────────────────────────────────
    -- 把 embed_text 送进智谱 embedding-2 模型，得到的 1024 个浮点数。
    -- 这是整个 RAG 检索的核心——所有"语义搜索"本质都是在比较这列向量。
    --
    -- pgvector 的 vector(1024) 类型专门存储这类向量，支持：
    --   余弦距离（<=> 操作符）：1 - cos(θ)，0=完全相同，2=完全相反
    --   HNSW 索引：近似最近邻搜索，100 万条数据也能毫秒级返回
    embedding       vector(1024),

    -- ── 结构化元数据 ──────────────────────────────────────────────────
    -- PostgreSQL 的 JSONB 列，存每种实体特有的结构化字段。
    -- 入库前由 Pydantic v2 强校验，确保格式不会出错。
    --
    -- 为什么用 JSONB 而不是拆成多列？
    --   Subject 有评分/标签/年份/集数，Character 有出演作品，Person 有职业/代表作——
    --   如果拆成几十列，表会又宽又空（90% 的格子是 NULL）。JSONB 只在需要时存，
    --   还能对其内部字段建查询（如 WHERE meta_info->>'year'='2023'）。
    --
    -- Subject 的 meta_info 结构（SubjectMeta 校验）：
    --   {"score":8.37, "rank":72, "year":2022, "platform":"TV", "eps":12,
    --    "rating_total":40378, "rating_count":[194,49,...],
    --    "collection":{"想看":4372,"看过":64456,...},
    --    "tags":[{"name":"芳文社","count":10817},...]}
    --
    -- Character 的 meta_info 结构（CharacterMeta 校验）：
    --   {"role":1, "collects":3851, "summary":"...","info":"...",
    --    "casts":[{"subject_id":"subject_10380","subject_name":"命运石之门",
    --              "person_id":null,"person_name":null,"role_type":1},...]}
    --
    -- Person 的 meta_info 结构（PersonMeta 校验）：
    --   {"career":["artist","seiyu"],"type":1,"collects":3016,
    --    "summary":"...","info":"...",
    --    "works":[{"subject_id":"subject_1","subject_name":"...","positions":[...]},...]}
    meta_info       JSONB DEFAULT '{}'::jsonb
);


-- ============================================================================
-- 二、索引（8 个，每个都有存在的理由）
-- ============================================================================
--
-- 索引的本质：用"空间换时间"。写入数据时多维护一个数据结构，查询时就能跳过全表扫描。
-- 每个索引对应一种查询模式，下面逐一解释。

-- 索引 1: entity_type — B-Tree
-- 查询模式: WHERE entity_type = 'subject'
-- 场景: 用户只搜动漫 → 先过滤掉 character 和 person，再在剩下的 subject 里做向量比对。
--       如果不建索引，每次都要扫全表 1500 行才知道哪些是 subject。
CREATE INDEX ix_rag_entities_entity_type ON rag_entities (entity_type);

-- 索引 2: name — B-Tree
-- 查询模式: WHERE name = '進撃の巨人'
-- 场景: 精确名称查找。"用户打出一个完整作品名"是最高频的检索场景之一。
CREATE INDEX ix_rag_entities_name ON rag_entities (name);

-- 索引 3: nsfw — B-Tree
-- 查询模式: WHERE nsfw = false
-- 场景: 安全护栏。几乎所有查询都要加 nsfw=false，它是高频过滤条件。
CREATE INDEX ix_rag_entities_nsfw ON rag_entities (nsfw);

-- 索引 4: popularity — B-Tree
-- 查询模式: ORDER BY popularity DESC
-- 场景: 分桶排序。语义相近的候选集里，按热度降序排列。
--       B-Tree 索引天然支持 ORDER BY 加速（叶子节点已排序）。
CREATE INDEX ix_rag_entities_popularity ON rag_entities (popularity);

-- 索引 5: subject_type — B-Tree（2026-08-09 新增）
-- 查询模式: WHERE subject_type = 2
-- 场景: 检索时只看动画版结果，不混入书籍、漫画。
--       数据库迁移脚本: scripts/migrate_subject_type.py
CREATE INDEX ix_rag_entities_subject_type ON rag_entities (subject_type);

-- 索引 6: name trigram — GIN + pg_trgm
-- 查询模式: WHERE name LIKE '%ぼっち%' 或 ILIKE
-- 场景: 模糊搜索。"用户只记得作品名的一部分"时，trigram 索引比全表 LIKE 快百倍。
--       pg_trgm 把字符串切成每 3 个字符一组的片段（如"孤独摇滚"→"孤独摇"+"独摇滚"），
--       对这些片段建索引后，模糊匹配不用逐行扫描。
CREATE INDEX ix_rag_entities_name_trgm ON rag_entities USING gin (name gin_trgm_ops);

-- 索引 7: rag_output trigram — GIN + pg_trgm
-- 查询模式: rag_output 内容内的关键词匹配
-- 场景: 检索时需要从 rag_output JSON 里提取 summary 片段用于展示。
--       和 name_trgm 原理相同，但作用于 rag_output（含完整摘要文本）。
CREATE INDEX ix_rag_entities_rag_output_trgm
    ON rag_entities USING gin (rag_output gin_trgm_ops);

-- 索引 8: embedding — HNSW（最重要的索引！）
-- 查询模式: ORDER BY embedding <=> $query_vector LIMIT 10
-- 场景: 向量语义检索。这是整个 RAG 最核心的查询——"找到与用户问题最相似的实体"。
--
-- HNSW（Hierarchical Navigable Small World）是一种近似最近邻搜索算法：
--   精确计算 1024 维向量 × 1500 条数据的余弦距离非常慢，
--   HNSW 会"跳着找"——先粗略定位到候选区域，再精细比对，大幅加速。
--   vector_cosine_ops 指定用余弦距离（<=>）而不是欧氏距离（<->）。
CREATE INDEX ix_rag_entities_embedding
    ON rag_entities USING hnsw (embedding vector_cosine_ops);


-- ============================================================================
-- 三、示例数据（2026-08-06 真实灌入数据，每种实体取 popularity 最高的一条）
-- ============================================================================
--
-- 每条 INSERT 展示了一行完整数据，你可以对照上面的列定义逐列理解。
-- 向量（embedding）用 '[0.0156, ...]'::vector 简写占位——真实数据有 1024 个浮点数。

-- 3.1 SUBJECT — 孤独摇滚！（entity_type='subject', subject_type=2 动画）
-- ─────────────────────────────────────────────────
-- 注意 rag_output 是一段完整的 JSON 字符串（VARCHAR 类型存），
-- 结构和 Bangumi API 的 /p1/subjects/{id} 返回格式一致，外加 "_source":"rag" 标记来源。
INSERT INTO rag_entities (id, entity_type, name, name_cn, nsfw, subject_type,
    embed_text, rag_output, embedding, meta_info, popularity)
VALUES (
    'subject_328609',                              -- id: subject_ + Bangumi 作品 ID
    'subject',                                      -- entity_type
    'ぼっち・ざ・ろっく！',                         -- name（原文）
    '孤独摇滚！',                                   -- name_cn
    false,                                          -- nsfw
    2,                                              -- subject_type: 2=动画
    -- embed_text: [名称 + 标签 + 年代 + 评分 + 简介首句]，≤400 字符，仅供向量化
    '孤独摇滚！ ぼっち・ざ・ろっく！ 动画 芳文社 音乐 轻百合 CloverWorks 日常 漫画改 乐队题材 2022 TV 8.4分 作为网络吉他手"吉他英雄"而广受好评的后藤一里',
    -- rag_output: 完整的 JSON 字符串，对齐 API detail 格式 + _source/_next RAG 标记
    '{"id":328609,"name":"ぼっち・ざ・ろっく！","name_cn":"孤独摇滚！","type":"TV","info":"12话 / 2022年10月8日 / 斎藤圭一郎 / はまじあき（芳文社「まんがタイムきららMAX」連載中） / けろりら","date":"2022-10-08","eps":12,"volumes":0,"series":false,"series_entry":0,"nsfw":false,"summary":"作为网络吉他手\"吉他英雄\"而广受好评的后藤一里，在现实中却是个什么都不会的沟通障碍者……","score":8.37,"rank":72,"rating_total":40378,"rating_count":[194,49,48,108,364,1382,5068,14006,12329,6830],"collection":{"想看":4372,"看过":64456,"在看":5175,"搁置":1127,"抛弃":612},"tags":[{"name":"芳文社","count":10817},{"name":"音乐","count":9060},{"name":"轻百合","count":7203},{"name":"CloverWorks","count":7026},{"name":"日常","count":6678}],"infobox":{"导演":"斎藤圭一郎","原作":"はまじあき","动画制作":"CloverWorks"},"_source":"rag","subject_type":2,"_next":"如需口碑数据调 get_subject_opinions(328609)；如需角色列表调 get_subject_characters(328609)"}',
    '[0.0156, ...]'::vector,                        -- embedding: 1024 维向量占位
    -- meta_info: 结构化元数据（JSONB），入库前由 SubjectMeta 校验
    '{"eps":12,"date":"2022-10-08","rank":72,"year":2022,"score":8.37,"platform":"TV","rating_total":40378,"rating_count":[194,49,48,108,364,1382,5068,14006,12329,6830],"collection":{"1":4372,"2":64456,"3":5175,"4":1127,"5":612},"tags":[{"name":"芳文社","count":10817},{"name":"音乐","count":9060},{"name":"轻百合","count":7203}]}'::jsonb,
    40378                                           -- popularity: rating_total（评分人数）
);

-- 3.2 CHARACTER — 牧瀬紅莉栖（entity_type='character', subject_type=NULL）
-- ─────────────────────────────────────────────────
-- Character 的 rag_output 里多了一个 works 字段：列出该角色出场的前 3 部作品。
-- meta_info 里多了 casts：完整的出演列表（含 subject 关联边）。
INSERT INTO rag_entities (id, entity_type, name, name_cn, nsfw, subject_type,
    embed_text, rag_output, embedding, meta_info, popularity)
VALUES (
    'character_12393',                              -- id: character_ + Bangumi 角色 ID
    'character',
    '牧瀬紅莉栖',
    '牧濑红莉栖',
    false,
    NULL,                                           -- subject_type: NULL（角色没有子类型）
    '牧濑红莉栖 牧瀬紅莉栖 命运石之门 18岁即从大学毕业 在美国著名的学术杂志上刊登论文',
    '{"id":12393,"name":"牧瀬紅莉栖","name_cn":"牧濑红莉栖","role":"主角","info":"性别 女 / 生日 1992年7月25日 / 血型 A型 / 身高 160cm","summary":"维克多·孔多利亚大学脑科学研究所的研究员，18岁即从大学毕业……典型的傲娇","infobox":{"常用":"外文书籍","讨厌":"笨蛋，牛排肉","兴趣":"做实验，上@Channel"},"comment":359,"collects":3851,"nsfw":false,"works":[{"subject_name":"命运石之门"},{"subject_name":"命运石之门 0"},{"subject_name":"命运石之门 负荷领域的既视感"}],"_source":"rag"}',
    '[0.0088, ...]'::vector,
    '{"role":1,"collects":3851,"info":"性别 女 / 生日 1992年7月25日","summary":"维克多·孔多利亚大学脑科学研究所的研究员…","casts":[{"subject_id":"subject_10380","subject_name":"命运石之门","person_id":null,"person_name":null,"role_type":1}]}'::jsonb,
    3851                                            -- popularity: collects（收藏数）
);

-- 3.3 PERSON — 花澤香菜（entity_type='person', subject_type=NULL）
-- ─────────────────────────────────────────────────
-- Person 的 rag_output 里多了 career（职业标签）+ works（代表作）。
-- meta_info 用 works 存完整的关联边（含职位信息）。
INSERT INTO rag_entities (id, entity_type, name, name_cn, nsfw, subject_type,
    embed_text, rag_output, embedding, meta_info, popularity)
VALUES (
    'person_4765',                                  -- id: person_ + Bangumi 人物 ID
    'person',
    '花澤香菜',
    '花泽香菜',
    false,
    NULL,                                           -- subject_type: NULL（人物没有子类型）
    '花泽香菜 花澤香菜 artist seiyu 876 PRODUCTION FES おでかけ 日本女性艺人、声优、演员',
    '{"id":4765,"name":"花澤香菜","name_cn":"花泽香菜","type":"个人","career":["artist","seiyu"],"info":"性别 女 / 生日 1989年2月25日 / 血型 AB型 / 身高 156.7cm","summary":"花泽香菜（1989年2月25日－），是一名日本女性艺人、声优、演员。大泽事务所所属……","infobox":{"性格":"有点怕生","趣味":"泡澡","特技":"物体模仿","星座":"双鱼"},"comment":386,"collects":3016,"nsfw":false,"works":[{"subject_name":"876 PRODUCTION FES","role":"艺术家"},{"subject_name":"fade into blue","role":"艺术家"}],"_source":"rag"}',
    '[0.0123, ...]'::vector,
    '{"career":["artist","seiyu"],"type":1,"collects":3016,"info":"性别 女 / 生日 1989年2月25日","summary":"花泽香菜（1989年2月25日－），是一名日本女性艺人、声优、演员…"}'::jsonb,
    3016                                            -- popularity: collects（收藏数）
);


-- ============================================================================
-- 四、端到端数据流（从灌入到检索，一条数据的完整旅程）
-- ============================================================================
--
-- 这张图展示了数据是怎么"流"过整个系统的。
-- 不需要全部理解，看到哪个环节有问题再回来查就行。
--
-- ┌─ Phase 1: ID 发现 ─────────────────────────────────────┐
-- │  python -m rag.cli.discover                             │
-- │  ↓ 从 Bangumi p1 API + HTML 页面采集 Top-N 实体 ID      │
-- │  ↓ 输出: scripts/data/subject_ids_type2.json (1000 IDs) │
-- └────────────────────────────────────────────────────────┘
--         ↓
-- ┌─ Phase 2: API 富化 ────────────────────────────────────┐
-- │  python -m rag.cli.ingest                               │
-- │  ↓ 用 ID 列表调用 Bangumi API 获取详情（评分/标签/简介等）│
-- │  ↓ rag/enricher.py: SubjectCollector / CharacterEnricher │
-- └────────────────────────────────────────────────────────┘
--         ↓
-- ┌─ Phase 3: 向量化 + 灌入 ───────────────────────────────┐
-- │  rag/ingestion.py: RagEntityIngestor                    │
-- │  ↓ 构造 embed_text（关键词密集文本）                     │
-- │  ↓ 调用 智谱 embedding-2 API → 1024 维向量              │
-- │  ↓ 构造 rag_output（预构建 JSON）                       │
-- │  ↓ 构造 meta_info（Pydantic v2 校验）                   │
-- │  ↓ session.merge(entity) → 写入 rag_entities 表         │
-- └────────────────────────────────────────────────────────┘
--         ↓
-- ┌─ Phase 4: 检索（用户问一个问题时触发）─────────────────┐
-- │  rag/retriever.py: RagEntityRetriever.hybrid_search()   │
-- │  ↓ 用户 query 向量化（同一 embedding-2 模型）            │
-- │  ↓ SQL: entity_type 标量前置过滤 + nsfw 安全护栏         │
-- │  ↓ SQL: embedding <=> $qv 余弦距离排序                  │
-- │  ↓ Python: 距离阈值防爆 + 多态阶梯分桶 + MMR 去重       │
-- │  ↓ json.loads(r.rag_output) → 直接返回 dict             │
-- └────────────────────────────────────────────────────────┘
--
--
-- ============================================================================
-- 五、检索示例 SQL（帮你理解搜索是怎么工作的）
-- ============================================================================
--
-- 以下 SQL 展示了 hybrid_search() 在数据库里实际执行的查询。
-- $qv = 用户问题的 1024 维向量（由应用层 embedding-2 生成后传入）

-- 最简单的语义检索：找到和用户问题最像的 10 部动画
-- SELECT id, name, name_cn,
--        embedding <=> $qv AS cos_dist     -- <=> 是 pgvector 的余弦距离操作符
-- FROM rag_entities
-- WHERE nsfw = false                       -- 排除 R18
--   AND entity_type = 'subject'            -- 只看动画条目
--   AND subject_type = 2                   -- 只看动画，不混入书籍
-- ORDER BY embedding <=> $qv               -- 按语义相似度降序
-- LIMIT 10;

-- 带标签过滤的精确检索：找所有芳文社的动画
-- SELECT id, name, name_cn
-- FROM rag_entities
-- WHERE entity_type = 'subject'
--   AND meta_info->'tags' @> '[{"name": "芳文社"}]'   -- JSONB 包含查询
-- ORDER BY popularity DESC
-- LIMIT 20;

-- 年份 + 评分组合过滤
-- SELECT id, name, name_cn, meta_info->>'score' AS score
-- FROM rag_entities
-- WHERE entity_type = 'subject'
--   AND subject_type = 2
--   AND (meta_info->>'score')::float >= 8.0
--   AND meta_info->>'year' = '2023'
-- ORDER BY (meta_info->>'score')::float DESC;

-- 模糊名称搜索（用 trigram 索引加速）
-- SELECT id, name, name_cn
-- FROM rag_entities
-- WHERE name LIKE '%ぼっち%'
--    OR name_cn LIKE '%孤独%';


-- ============================================================================
-- 六、数据规模一览（2026-08-06 灌入数据）
-- ============================================================================
--
--  实体类型       subject_type   头部（热门）  尾部（冷门）  合计   占比
--  ─────────     ────────────   ──────────   ──────────   ────   ────
--  Subject 动画   2（动画）       800          100           900    60%
--  Subject 书籍   1（书籍）       100          30            130    9%
--  Character      NULL           300          50            350    23%
--  Person         NULL           100          20            120    8%
--  ─────────────────────────────────────────────────────────────────
--  总计                                                    1500  100%
--
-- 随机种子 TAIL_SEED=42，保证每次采样可复现。


-- ============================================================================
-- 七、常用维护命令
-- ============================================================================
--
--  操作              命令
--  ────────────────  ──────────────────────────────────────
--  查看表大小        SELECT pg_size_pretty(pg_total_relation_size('rag_entities'));
--  统计各行数量      SELECT entity_type, subject_type, COUNT(*) FROM rag_entities GROUP BY 1,2;
--  查看索引大小      SELECT indexname, pg_size_pretty(pg_relation_size(indexname::regclass))
--                    FROM pg_indexes WHERE tablename='rag_entities';
--  重建全部数据      python -m rag.cli.ingest --clear
--  数据库迁移        python scripts/migrate_subject_type.py
--  RAG 评测          python -m eval.rag_eval --build        → 生成 GT + 标注模板
--                    python -m eval.rag_eval --evaluate     → 跑检索 + 算指标
--                    python -m eval.rag_eval --check        → 回归不变量断言（PASS/FAIL）
