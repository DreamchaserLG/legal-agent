from __future__ import annotations

"""SQLite 数据库初始化"""
import os
from sqlalchemy import text

from app.core.database import engine, is_sqlite


def init_sqlite_tables():
    """初始化 SQLite 表结构"""
    if not is_sqlite():
        return

    statements = [
        # 用户表
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(60) UNIQUE NOT NULL,
            email VARCHAR(120) UNIQUE NOT NULL DEFAULT '',
            password_hash VARCHAR(255) NOT NULL DEFAULT '',
            role VARCHAR(30) NOT NULL DEFAULT 'user',
            status VARCHAR(30) NOT NULL DEFAULT 'active',
            real_name VARCHAR(100) NOT NULL DEFAULT '',
            phone VARCHAR(30) NOT NULL DEFAULT '',
            organization VARCHAR(120) NOT NULL DEFAULT '',
            country_preference VARCHAR(60) NOT NULL DEFAULT '',
            legal_type_preference VARCHAR(60) NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            last_login_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # 案例表
        """
        CREATE TABLE IF NOT EXISTS legal_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            court_name TEXT NOT NULL DEFAULT '',
            court_level TEXT NOT NULL DEFAULT '',
            court_rank INTEGER NOT NULL DEFAULT 0,
            case_type TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            facts TEXT NOT NULL DEFAULT '',
            judgment_result TEXT NOT NULL DEFAULT '',
            judgment_date DATE NULL,
            source_url TEXT NOT NULL DEFAULT '',
            source_site TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '',
            source_item_id BIGINT NULL,
            source_code VARCHAR(50) NOT NULL DEFAULT '',
            external_uid TEXT NOT NULL DEFAULT '',
            normalized_title TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # 法规表
        """
        CREATE TABLE IF NOT EXISTS legal_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            legal_type TEXT NOT NULL DEFAULT '',
            article_no TEXT NOT NULL DEFAULT '',
            article_text TEXT NOT NULL DEFAULT '',
            article_summary TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            source_site TEXT NOT NULL DEFAULT '',
            source_item_id BIGINT NULL,
            canada_law_id BIGINT NULL,
            normalized_title TEXT NOT NULL DEFAULT '',
            slug VARCHAR(240) NOT NULL DEFAULT '',
            rule_level TEXT NOT NULL DEFAULT '',
            citation TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # 案例法规关联表
        """
        CREATE TABLE IF NOT EXISTS case_rule_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id BIGINT NOT NULL,
            rule_id BIGINT NOT NULL,
            relation_type TEXT NOT NULL DEFAULT '',
            match_score REAL NOT NULL DEFAULT 0,
            match_reason TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(case_id, rule_id)
        )
        """,
        # 案例法规关联表 (canada)
        """
        CREATE TABLE IF NOT EXISTS canada_case_law_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_item_id BIGINT NOT NULL,
            law_id BIGINT NOT NULL,
            matched_alias TEXT NOT NULL DEFAULT '',
            match_source TEXT NOT NULL DEFAULT '',
            match_score REAL NOT NULL DEFAULT 0,
            evidence_excerpt TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(case_item_id, law_id)
        )
        """,
        # 案例法规关联表 (canada)
        """
        CREATE TABLE IF NOT EXISTS canada_case_rule_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id BIGINT NOT NULL,
            rule_id BIGINT NOT NULL,
            relation_type TEXT NOT NULL DEFAULT '',
            match_score REAL NOT NULL DEFAULT 0,
            match_reason TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(case_id, rule_id)
        )
        """,
        # Agent 运行表
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            input_text TEXT NOT NULL,
            module_code VARCHAR(60) NOT NULL DEFAULT 'canada',
            source_filter VARCHAR(60) NOT NULL DEFAULT 'all',
            sort_mode VARCHAR(60) NOT NULL DEFAULT 'relevance',
            extracted_keywords JSONB NOT NULL DEFAULT '[]',
            structured_analysis JSONB NOT NULL DEFAULT '{}',
            result_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # Agent 预测表
        """
        CREATE TABLE IF NOT EXISTS agent_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_run_id BIGINT NOT NULL,
            input_text TEXT NOT NULL,
            module_code VARCHAR(60) NOT NULL DEFAULT 'canada',
            predicted_outcome TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            reasoning TEXT NOT NULL DEFAULT '',
            key_factors JSONB NOT NULL DEFAULT '[]',
            supporting_items JSONB NOT NULL DEFAULT '[]',
            model_name VARCHAR(100) NOT NULL DEFAULT '',
            raw_json JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # 用户案例历史表
        """
        CREATE TABLE IF NOT EXISTS user_case_histories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL,
            query_text TEXT NOT NULL,
            query_type VARCHAR(30) NOT NULL DEFAULT 'analysis',
            country VARCHAR(60) NOT NULL DEFAULT '',
            court_level VARCHAR(60) NOT NULL DEFAULT '',
            legal_type VARCHAR(60) NOT NULL DEFAULT '',
            result_snapshot JSONB NOT NULL DEFAULT '{}',
            case_hash VARCHAR(128) NOT NULL DEFAULT '',
            case_title VARCHAR(200) NOT NULL DEFAULT '',
            case_summary TEXT NOT NULL DEFAULT '',
            case_type VARCHAR(60) NOT NULL DEFAULT '',
            key_facts_json JSONB NOT NULL DEFAULT '[]',
            dispute_focus_json JSONB NOT NULL DEFAULT '[]',
            legal_rule_ids_json JSONB NOT NULL DEFAULT '[]',
            legal_rules_json JSONB NOT NULL DEFAULT '[]',
            risk_points_json JSONB NOT NULL DEFAULT '[]',
            prediction_label VARCHAR(100) NOT NULL DEFAULT '',
            prediction_conclusion TEXT NOT NULL DEFAULT '',
            prediction_explanation TEXT NOT NULL DEFAULT '',
            prediction_confidence REAL NOT NULL DEFAULT 0,
            graph_json JSONB NOT NULL DEFAULT '{}',
            view_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_viewed_at TIMESTAMP NULL,
            UNIQUE(user_id, case_hash)
        )
        """,
        # 搜索历史表
        """
        CREATE TABLE IF NOT EXISTS search_histories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT NOT NULL,
            query_text TEXT NOT NULL,
            query_type VARCHAR(30) NOT NULL DEFAULT 'analysis',
            country VARCHAR(60) NOT NULL DEFAULT '',
            court_level VARCHAR(60) NOT NULL DEFAULT '',
            case_hash VARCHAR(128) NOT NULL DEFAULT '',
            case_title VARCHAR(200) NOT NULL DEFAULT '',
            case_type VARCHAR(60) NOT NULL DEFAULT '',
            case_summary TEXT NOT NULL DEFAULT '',
            key_facts_json JSONB NOT NULL DEFAULT '[]',
            dispute_focus_json JSONB NOT NULL DEFAULT '[]',
            legal_rule_ids_json JSONB NOT NULL DEFAULT '[]',
            legal_rules_json JSONB NOT NULL DEFAULT '[]',
            risk_points_json JSONB NOT NULL DEFAULT '[]',
            prediction_label VARCHAR(100) NOT NULL DEFAULT '',
            prediction_conclusion TEXT NOT NULL DEFAULT '',
            prediction_explanation TEXT NOT NULL DEFAULT '',
            prediction_confidence REAL NOT NULL DEFAULT 0,
            graph_json JSONB NOT NULL DEFAULT '{}',
            view_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_viewed_at TIMESTAMP NULL
        )
        """,
        # 案例投票表
        """
        CREATE TABLE IF NOT EXISTS case_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            vote_type VARCHAR(10) NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(case_id, user_id)
        )
        """,
        # 源数据表
        """
        CREATE TABLE IF NOT EXISTS source_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_code VARCHAR(80) NOT NULL,
            source_uid TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            item_url TEXT NOT NULL DEFAULT '',
            published_at TIMESTAMP NULL,
            summary TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL DEFAULT '',
            raw_json JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_code, source_uid)
        )
        """,
        # 关键词表
        """
        CREATE TABLE IF NOT EXISTS item_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id BIGINT NOT NULL,
            keyword TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(item_id, keyword)
        )
        """,
        # RAG 表
        """
        CREATE TABLE IF NOT EXISTS rag_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_kind VARCHAR(40) NOT NULL,
            source_table VARCHAR(80) NOT NULL,
            source_id BIGINT NOT NULL,
            source_code VARCHAR(80) NOT NULL DEFAULT '',
            source_uid TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            published_at TIMESTAMP NULL,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            text_content TEXT NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_table, source_id, chunk_index)
        )
        """,
        # RAG 索引运行表
        """
        CREATE TABLE IF NOT EXISTS rag_index_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_filter VARCHAR(80) NOT NULL DEFAULT 'all',
            status VARCHAR(30) NOT NULL,
            documents_seen INTEGER NOT NULL DEFAULT 0,
            chunks_written INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP NULL
        )
        """,
        # 运行时状态表
        """
        CREATE TABLE IF NOT EXISTS app_runtime_state (
            state_key VARCHAR(120) PRIMARY KEY,
            state_json TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # 导入任务表
        """
        CREATE TABLE IF NOT EXISTS import_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_by BIGINT NULL,
            import_type VARCHAR(30) NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            total_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # 摄入任务表
        """
        CREATE TABLE IF NOT EXISTS ingestion_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type VARCHAR(60) NOT NULL DEFAULT '',
            source_filter VARCHAR(60) NOT NULL DEFAULT 'all',
            status VARCHAR(30) NOT NULL DEFAULT 'queued',
            worker_name VARCHAR(120) NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP NULL,
            finished_at TIMESTAMP NULL
        )
        """,
    ]

    with engine.begin() as conn:
        for statement in statements:
            try:
                conn.execute(text(statement))
            except Exception as e:
                # 忽略已存在的表错误
                if "already exists" not in str(e).lower():
                    print(f"Warning: {e}")
