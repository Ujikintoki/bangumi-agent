CREATE TABLE rag_entities (
    id VARCHAR PRIMARY KEY,
    entity_type VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    name_cn VARCHAR,
    embedding vector(2048),
    meta_info JSONB DEFAULT '{}'::jsonb
);

-- 基础 B-Tree 索引加速标量过滤
CREATE INDEX ix_rag_entities_type ON rag_entities (entity_type);
CREATE INDEX ix_rag_entities_name ON rag_entities (name);

-- 唯一的高性能向量索引，搭配 meta_info 里的评分做你之前设计的“阶梯排序”
CREATE INDEX ix_rag_entities_embedding ON rag_entities USING hnsw (embedding vector_cosine_ops);
