-- ============================================================================
-- rag_entities 表结构 + 真实示例数据
--
-- 单表多态设计：Subject / Character / Person 共用一张表，entity_type 区分。
-- rag_output 存储预构建的 JSON dict（对齐 API detail 工具格式，检索时零转换）。
-- embed_text 存储关键词密集合成文本（仅供 embedding 向量化）。
--
-- 最后更新: 2026-08-06
-- 对应 ORM:   database/rag_tables.py → class RagEntity(SQLModel, table=True)
-- 对应摄入:   rag/ingestion.py → RagEntityIngestor
-- 对应检索:   tools/bgm_tools.py → search_local_bangumi
-- ============================================================================

-- 前置条件
-- CREATE EXTENSION IF NOT EXISTS vector;
-- CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- 1. 建表 DDL（10 列）
-- ============================================================================

DROP TABLE IF EXISTS rag_entities;

CREATE TABLE rag_entities (
    id              VARCHAR PRIMARY KEY,       -- subject_328609 / character_12393 / person_4765
    entity_type     VARCHAR NOT NULL,           -- 'subject' | 'character' | 'person'
    name            VARCHAR NOT NULL,           -- 原文名称（日文/英文）
    name_cn         VARCHAR,                    -- 中文名称（可为空）
    nsfw            BOOLEAN NOT NULL DEFAULT false,  -- R18 安全护栏
    rag_output      VARCHAR NOT NULL,           -- JSON dict（预构建，对齐 API detail 工具 schema）
    embed_text      VARCHAR,                    -- 关键词密集合成文本（仅供 embedding）
    embedding       vector(1024),               -- pgvector (Zhipu embedding-2)
    meta_info       JSONB DEFAULT '{}'::jsonb,  -- 结构化元数据（Pydantic v2 契约校验）
    popularity      INTEGER NOT NULL DEFAULT 0  -- 热度信号（subject→rating_total, 其他→collects）
);

-- ============================================================================
-- 2. 索引（8 个）
-- ============================================================================

CREATE INDEX ix_rag_entities_entity_type ON rag_entities (entity_type);
CREATE INDEX ix_rag_entities_name        ON rag_entities (name);
CREATE INDEX ix_rag_entities_nsfw        ON rag_entities (nsfw);
CREATE INDEX ix_rag_entities_popularity  ON rag_entities (popularity);
CREATE INDEX ix_rag_entities_name_trgm   ON rag_entities USING gin (name gin_trgm_ops);
CREATE INDEX ix_rag_entities_rag_output_trgm ON rag_entities USING gin (rag_output gin_trgm_ops);
CREATE INDEX ix_rag_entities_embedding   ON rag_entities USING hnsw (embedding vector_cosine_ops);

-- ============================================================================
-- 3. 示例数据（2026-08-06 真实灌入，各取 popularity 最高的一条）
-- ============================================================================

-- 3.1 SUBJECT — 孤独摇滚！（对齐 sanitize_subject_detail + _source, _next）
INSERT INTO rag_entities (id, entity_type, name, name_cn, nsfw, embed_text, rag_output, embedding, meta_info, popularity)
VALUES (
    'subject_328609',
    'subject',
    'ぼっち・ざ・ろっく！',
    '孤独摇滚！',
    false,
    '孤独摇滚！ ぼっち・ざ・ろっく！ 芳文社 音乐 轻百合 CloverWorks 日常 漫画改 乐队题材 2022年10月 搞笑 TV 2022 动画 8.4分 作为网络吉他手"吉他英雄"而广受好评的后藤一里，在现实中却是个什么都不会的沟通障碍者',
    '{"id":328609,"name":"ぼっち・ざ・ろっく！","name_cn":"孤独摇滚！","type":"动画","info":"12话 / 2022年10月8日 / 斎藤圭一郎 / はまじあき（芳文社「まんがタイムきららMAX」連載中） / けろりら","date":"2022-10-08","eps":12,"volumes":0,"series":false,"series_entry":0,"nsfw":false,"summary":"作为网络吉他手"吉他英雄"而广受好评的后藤一里，在现实中却是个什么都不会的沟通障碍者。一里有着组建乐队的梦想，但因为不敢向人主动搭话而一直没有成功，直到一天在公园中被伊地知虹夏发现并邀请进入缺少吉他手的"结束乐队"。可是，完全没有和他人合作经历的一里，在人前完全发挥不出原本的实力。为了努力克服沟通障碍，一里与"结束乐队"的成员们一同开始努力……","score":8.37,"rank":72,"rating_total":40378,"rating_count":[194,49,48,108,364,1382,5068,14006,12329,6830],"collection":{"想看":4372,"看过":64456,"在看":5175,"搁置":1127,"抛弃":612},"tags":[{"name":"芳文社","count":10817},{"name":"音乐","count":9060},{"name":"轻百合","count":7203},{"name":"CloverWorks","count":7026},{"name":"日常","count":6678},{"name":"漫画改","count":4684},{"name":"乐队题材","count":4549},{"name":"2022年10月","count":4493},{"name":"搞笑","count":3138},{"name":"TV","count":3103}],"infobox":{"导演":"斎藤圭一郎","原作":"はまじあき","音乐":"菊谷知樹","系列构成":"吉田恵里香","人物设定":"けろりら","动画制作":"CloverWorks","製作":"Aniplex、芳文社"},"_source":"rag","_next":"如需口碑数据调 get_subject_opinions(328609)；如需角色列表调 get_subject_characters(328609)"}',
    '[0.0156, ...]'::vector,
    '{"eps":12,"date":"2022-10-08","rank":72,"year":2022,"score":8.37,"platform":"动画","rating_total":40378,"rating_count":[194,49,48,108,364,1382,5068,14006,12329,6830],"collection":{"1":4372,"2":64456,"3":5175,"4":1127,"5":612},"tags":[{"name":"芳文社","count":10817},{"name":"音乐","count":9060},{"name":"轻百合","count":7203}]}'::jsonb,
    40378
);

-- 3.2 CHARACTER — 牧瀬紅莉栖（对齐 sanitize_character_detail + works, _source）
INSERT INTO rag_entities (id, entity_type, name, name_cn, nsfw, embed_text, rag_output, embedding, meta_info, popularity)
VALUES (
    'character_12393',
    'character',
    '牧瀬紅莉栖',
    '牧濑红莉栖',
    false,
    '牧濑红莉栖 牧瀬紅莉栖 命运石之门 维克多·孔多利亚（原型为哥伦比亚大学）大学脑科学研究所的研究员，18 岁即从大学毕业(因为美国的跳级制度，所以实际年龄跟高三学生相当)，在美国著名的学术杂志上刊登论文而受到瞩目',
    '{"id":12393,"name":"牧瀬紅莉栖","name_cn":"牧濑红莉栖","role":"主角","info":"性别 女 / 生日 1992年7月25日 / 血型 A型 / 身高 160cm / 体重 45kg / BWH B79/W56/H83","summary":"维克多·孔多利亚（原型为哥伦比亚大学）大学脑科学研究所的研究员，18 岁即从大学毕业，在美国著名的学术杂志上刊登论文而受到瞩目。或许是因为饱尝周围人们充满羡慕与嫉妒的目光，面对他人时从来不会露出半点破绽。然而作为研究者，其本质还是个难以掩藏旺盛的好奇心、对感兴趣的事物一头扎进去的女孩。是个典型的傲娇，而且是一旦关系变好后就用情很深的类型。","infobox":{"常用的东西":"外文书籍","讨厌的东西":"笨蛋，牛排肉，蟑螂，环保运动","喜欢的东西":"SF 小说，拉面(包括杯面)，白大褂","兴趣":"做实验，上@Channel","未来道具研究所研究员":"No.004"},"comment":359,"collects":3851,"nsfw":false,"works":[{"subject_name":"命运石之门"},{"subject_name":"命运石之门 0"},{"subject_name":"命运石之门 负荷领域的既视感"}],"_source":"rag"}',
    '[0.0088, ...]'::vector,
    '{"role":1,"collects":3851,"info":"性别 女 / 生日 1992年7月25日 / 血型 A型 / 身高 160cm","summary":"维克多·孔多利亚大学脑科学研究所的研究员...","casts":[{"subject_id":"subject_10380","subject_name":"命运石之门","person_id":null,"person_name":null,"role_type":1}]}'::jsonb,
    3851
);

-- 3.3 PERSON — 花澤香菜（对齐 sanitize_person_detail + works, _source）
INSERT INTO rag_entities (id, entity_type, name, name_cn, nsfw, embed_text, rag_output, embedding, meta_info, popularity)
VALUES (
    'person_4765',
    'person',
    '花澤香菜',
    '花泽香菜',
    false,
    '花泽香菜 花澤香菜 artist seiyu 876 PRODUCTION FES おでかけ 花泽香菜（1989年2月25日－），是一名日本女性艺人、声优、演员',
    '{"id":4765,"name":"花澤香菜","name_cn":"花泽香菜","type":"个人","career":["artist","seiyu"],"info":"性别 女 / 生日 1989年2月25日 / 血型 AB型 / 身高 156.7cm / 体重 45kg","summary":"花泽香菜（1989年2月25日－），是一名日本女性艺人、声优、演员。大泽事务所所属。从幼女到青少年的少女，被多次选拔为主角役。代表角色有《化物语》千石抚子、《Angel Beats!》立华奏、《我的妹妹哪有这么可爱！》黑猫（五更琉璃）、《命运石之门》椎名真由理等。","infobox":{"性格":"有点怕生","趣味":"泡澡","特技":"物体模仿","学历":"文学部日本文学专业大学生","星座":"双鱼","出道时间":"2006"},"comment":386,"collects":3016,"nsfw":false,"works":[{"subject_name":"876 PRODUCTION FES","role":"艺术家"},{"subject_name":"fade into blue","role":"艺术家"},{"subject_name":"おでかけ","role":"艺术家"}],"_source":"rag"}',
    '[0.0123, ...]'::vector,
    '{"career":["artist","seiyu"],"type":1,"collects":3016,"info":"性别 女 / 生日 1989年2月25日 / 血型 AB型 / 身高 156.7cm","summary":"花泽香菜（1989年2月25日－），是一名日本女性艺人、声优、演员..."}'::jsonb,
    3016
);

-- ============================================================================
-- 4. 检索示例
-- ============================================================================

-- 语义检索（query 向量 $qv 由应用层 embedding-2 生成）
-- SELECT id, name, name_cn,
--        embedding <=> $qv AS cos_dist
-- FROM rag_entities
-- WHERE nsfw = false
--   AND entity_type = 'subject'
-- ORDER BY embedding <=> $qv
-- LIMIT 10;

-- rag_output 直接 json 解析返回
-- search_local_bangumi 内部: json.loads(r.rag_output) → 返回 dict
 