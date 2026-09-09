-- BGE-M3 加拿大案例分块语义向量的正式结构。
-- run_migration.py 先建立带 _v2 后缀的暂存表，在质量门禁通过后原子切换为本结构。

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS migration_checkpoint (
    migration_name TEXT NOT NULL,
    source_name TEXT NOT NULL,
    last_processed_id BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (migration_name, source_name)
);

-- 主表：全文与案例元数据，每个案例一行。
CREATE TABLE IF NOT EXISTS cases_metadata (
    id BIGSERIAL PRIMARY KEY,
    case_number TEXT UNIQUE,
    court TEXT,
    decision_date DATE,
    raw_text TEXT NOT NULL
);

-- 向量表：每个案例对应多个 512 token、64 token overlap 的分块。
CREATE TABLE IF NOT EXISTS case_chunks (
    id BIGSERIAL PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES cases_metadata(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    content_tsv TSVECTOR GENERATED ALWAYS AS
        (to_tsvector('english'::regconfig, COALESCE(chunk_text, ''))) STORED,
    UNIQUE (case_id, chunk_index)
);

-- 生产库必须在事务块外执行；迁移脚本会通过独立 autocommit 连接并行创建。
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_embedding
    ON case_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_tsv
    ON case_chunks USING gin (content_tsv);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_case_chunks_case
    ON case_chunks (case_id, chunk_index);

-- RRF 混合检索：向量检索与 PostgreSQL 全文检索各取 4 倍候选后融合。
CREATE OR REPLACE FUNCTION hybrid_search(
    query_text TEXT,
    query_vector VECTOR(1024),
    result_limit INTEGER DEFAULT 10
)
RETURNS TABLE (
    case_id BIGINT,
    case_number TEXT,
    court TEXT,
    decision_date DATE,
    chunk_index INTEGER,
    chunk_text TEXT,
    vector_score DOUBLE PRECISION,
    text_score DOUBLE PRECISION,
    rrf_score DOUBLE PRECISION
)
LANGUAGE sql STABLE AS $$
    WITH params AS (
        SELECT GREATEST(COALESCE(result_limit, 10), 1) AS max_rows
    ),
    vector_ranked AS (
        SELECT cc.id,
               1 - (cc.embedding <=> query_vector) AS vector_score,
               row_number() OVER (ORDER BY cc.embedding <=> query_vector) AS rank_no
        FROM case_chunks cc, params
        WHERE query_vector IS NOT NULL
        ORDER BY cc.embedding <=> query_vector
        LIMIT (SELECT max_rows * 4 FROM params)
    ),
    text_ranked AS (
        SELECT cc.id,
               ts_rank_cd(cc.content_tsv, plainto_tsquery('english', COALESCE(query_text, ''))) AS text_score,
               row_number() OVER (ORDER BY ts_rank_cd(cc.content_tsv, plainto_tsquery('english', COALESCE(query_text, ''))) DESC) AS rank_no
        FROM case_chunks cc, params
        WHERE COALESCE(query_text, '') <> ''
          AND cc.content_tsv @@ plainto_tsquery('english', query_text)
        ORDER BY ts_rank_cd(cc.content_tsv, plainto_tsquery('english', query_text)) DESC
        LIMIT (SELECT max_rows * 4 FROM params)
    ),
    fused AS (
        SELECT COALESCE(v.id, t.id) AS id,
               COALESCE(v.vector_score, 0)::double precision AS vector_score,
               COALESCE(t.text_score, 0)::double precision AS text_score,
               (COALESCE(1.0 / (60 + v.rank_no), 0.0) + COALESCE(1.0 / (60 + t.rank_no), 0.0))::double precision AS rrf_score
        FROM vector_ranked v FULL OUTER JOIN text_ranked t ON t.id = v.id
    )
    SELECT cm.id, cm.case_number, cm.court, cm.decision_date, cc.chunk_index, cc.chunk_text,
           fused.vector_score, fused.text_score, fused.rrf_score
    FROM fused
    JOIN case_chunks cc ON cc.id = fused.id
    JOIN cases_metadata cm ON cm.id = cc.case_id
    ORDER BY fused.rrf_score DESC, fused.vector_score DESC
    LIMIT (SELECT max_rows FROM params)
$$;
