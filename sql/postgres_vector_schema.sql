-- PostgreSQL + pgvector schema additions for the Legal Agent demo.
-- This file is safe to rerun. The application also initializes these tables at startup.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_chunks (
    id BIGSERIAL PRIMARY KEY,
    source_kind VARCHAR(40) NOT NULL,
    source_table VARCHAR(80) NOT NULL,
    source_id BIGINT NOT NULL,
    source_code VARCHAR(80) NOT NULL DEFAULT '',
    source_uid TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    jurisdiction VARCHAR(120) NOT NULL DEFAULT '',
    document_type VARCHAR(80) NOT NULL DEFAULT '',
    court_level VARCHAR(120) NOT NULL DEFAULT '',
    language VARCHAR(20) NOT NULL DEFAULT '',
    citation TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMP NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    text_content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    search_vector TSVECTOR,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_table, source_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_source
    ON rag_chunks(source_kind, source_code);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_updated
    ON rag_chunks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_structured
    ON rag_chunks(jurisdiction, document_type, court_level, language);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_vector
    ON rag_chunks USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_metadata_gin
    ON rag_chunks USING GIN(metadata_json);

CREATE TABLE IF NOT EXISTS rag_index_runs (
    id BIGSERIAL PRIMARY KEY,
    source_filter VARCHAR(80) NOT NULL DEFAULT 'all',
    status VARCHAR(30) NOT NULL,
    documents_seen INTEGER NOT NULL DEFAULT 0,
    chunks_written INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP NULL
);

CREATE INDEX IF NOT EXISTS idx_rag_index_runs_created
    ON rag_index_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS rag_chunk_embeddings (
    chunk_id BIGINT PRIMARY KEY REFERENCES rag_chunks(id) ON DELETE CASCADE,
    embedding_provider VARCHAR(40) NOT NULL DEFAULT '',
    embedding_model VARCHAR(120) NOT NULL DEFAULT '',
    dimension INTEGER NOT NULL DEFAULT 0,
    content_hash VARCHAR(64) NOT NULL DEFAULT '',
    embedding_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    embedding_vector VECTOR(1024),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rag_chunk_embeddings_model
    ON rag_chunk_embeddings(embedding_model);

CREATE INDEX IF NOT EXISTS idx_rag_chunk_embeddings_vector
    ON rag_chunk_embeddings USING hnsw (embedding_vector vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS risk_assessment_samples (
    id BIGSERIAL PRIMARY KEY,
    agent_run_id BIGINT NULL REFERENCES agent_runs(id) ON DELETE SET NULL,
    agent_prediction_id BIGINT NULL REFERENCES agent_predictions(id) ON DELETE SET NULL,
    module_code VARCHAR(60) NOT NULL DEFAULT 'canada',
    input_text TEXT NOT NULL DEFAULT '',
    risk_level VARCHAR(30) NOT NULL DEFAULT '',
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    evidence_status VARCHAR(40) NOT NULL DEFAULT '',
    evidence_quality_status VARCHAR(40) NOT NULL DEFAULT '',
    jurisdiction TEXT NOT NULL DEFAULT '',
    requested_relief TEXT NOT NULL DEFAULT '',
    disputed_issues_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    retrieved_evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    prediction_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    label_status VARCHAR(30) NOT NULL DEFAULT 'unlabeled',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_risk_samples_module_created
    ON risk_assessment_samples(module_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_risk_samples_level
    ON risk_assessment_samples(risk_level, evidence_status);
CREATE INDEX IF NOT EXISTS idx_risk_samples_prediction_gin
    ON risk_assessment_samples USING GIN(prediction_json);

CREATE TABLE IF NOT EXISTS risk_feedback_labels (
    id BIGSERIAL PRIMARY KEY,
    sample_id BIGINT NOT NULL REFERENCES risk_assessment_samples(id) ON DELETE CASCADE,
    human_risk_level VARCHAR(30) NOT NULL DEFAULT '',
    human_outcome TEXT NOT NULL DEFAULT '',
    human_notes TEXT NOT NULL DEFAULT '',
    label_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_risk_labels_sample
    ON risk_feedback_labels(sample_id, created_at DESC);
