from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings:
    def __init__(self):
        self.app_name = os.getenv("APP_NAME", "Foreign-related Legal AI")
        self.app_env = os.getenv("APP_ENV", "development").strip().lower()
        self.app_host = os.getenv("APP_HOST", "127.0.0.1")
        self.app_port = int(os.getenv("APP_PORT", "8000"))
        self.api_docs_enabled = (
            os.getenv(
                "API_DOCS_ENABLED",
                "false" if self.app_env == "production" else "true",
            ).strip().lower()
            in {"1", "true", "yes", "on"}
        )
        default_allowed_hosts = "127.0.0.1,localhost" if self.app_env == "production" else "*"
        self.allowed_hosts = _split_csv(os.getenv("ALLOWED_HOSTS", default_allowed_hosts))

        self.database_url = os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg2://postgres:change_me@127.0.0.1:5432/legal_demo",
        )

        self.request_timeout = int(os.getenv("REQUEST_TIMEOUT", "30"))
        self.llm_timeout = int(os.getenv("LLM_TIMEOUT", "90"))
        self.llm_retry_count = int(os.getenv("LLM_RETRY_COUNT", "2"))
        self.llm_retry_backoff_ms = int(os.getenv("LLM_RETRY_BACKOFF_MS", "800"))
        self.llm_circuit_breaker_seconds = int(os.getenv("LLM_CIRCUIT_BREAKER_SECONDS", "120"))
        self.cache_ttl_seconds = int(os.getenv("CACHE_TTL_SECONDS", "300"))
        self.prediction_use_model_case_comparison = (
            os.getenv("PREDICTION_USE_MODEL_CASE_COMPARISON", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.analysis_local_fast_mode = (
            os.getenv("ANALYSIS_LOCAL_FAST_MODE", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.search_fast_rag_enabled = (
            os.getenv("SEARCH_FAST_RAG_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.analysis_fast_llm_keyword_enabled = (
            os.getenv("ANALYSIS_FAST_LLM_KEYWORD_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.deep_analysis_enabled = (
            os.getenv("DEEP_ANALYSIS_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.deep_analysis_timeout_seconds = int(os.getenv("DEEP_ANALYSIS_TIMEOUT_SECONDS", "120"))
        self.deep_analysis_max_llm_calls = int(os.getenv("DEEP_ANALYSIS_MAX_LLM_CALLS", "5"))
        self.agentic_legal_research_enabled = (
            os.getenv("AGENTIC_LEGAL_RESEARCH_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.database_session_read_only = (
            os.getenv("DATABASE_SESSION_READ_ONLY", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.database_statement_timeout_ms = int(os.getenv("DATABASE_STATEMENT_TIMEOUT_MS", "12000"))
        self.database_lock_timeout_ms = int(os.getenv("DATABASE_LOCK_TIMEOUT_MS", "3000"))
        self.legal_research_max_rounds = int(os.getenv("LEGAL_RESEARCH_MAX_ROUNDS", "3"))
        self.legal_research_queries_per_round = int(os.getenv("LEGAL_RESEARCH_QUERIES_PER_ROUND", "6"))
        self.legal_research_max_evidence = int(os.getenv("LEGAL_RESEARCH_MAX_EVIDENCE", "40"))
        self.legal_research_min_new_evidence = int(os.getenv("LEGAL_RESEARCH_MIN_NEW_EVIDENCE", "2"))
        self.legal_research_timeout_seconds = int(os.getenv("LEGAL_RESEARCH_TIMEOUT_SECONDS", "90"))
        # Vector search remains available for direct/baseline retrieval, but it is
        # deliberately limited during multi-round research until contextual
        # embeddings have been rebuilt.  Lexical and graph channels are much
        # cheaper and provide better bounded latency on the local 6M+ corpus.
        self.legal_research_vector_rounds = int(os.getenv("LEGAL_RESEARCH_VECTOR_ROUNDS", "0"))
        self.analysis_stream_worker_threads = int(os.getenv("ANALYSIS_STREAM_WORKER_THREADS", "2"))
        self.analysis_stream_event_ttl_seconds = int(os.getenv("ANALYSIS_STREAM_EVENT_TTL_SECONDS", "3600"))
        self.quality_gate_citation_traceability_min = float(
            os.getenv("QUALITY_GATE_CITATION_TRACEABILITY_MIN", "0.7")
        )
        self.quality_gate_rule_currency_min = float(
            os.getenv("QUALITY_GATE_RULE_CURRENCY_MIN", "0.8")
        )
        self.quality_gate_evidence_completeness_min = float(
            os.getenv("QUALITY_GATE_EVIDENCE_COMPLETENESS_MIN", "0.6")
        )
        self.quality_gate_low_confidence_ratio_max = float(
            os.getenv("QUALITY_GATE_LOW_CONFIDENCE_RATIO_MAX", "0.4")
        )
        self.quality_gate_manual_review_max = int(
            os.getenv("QUALITY_GATE_MANUAL_REVIEW_MAX", "10")
        )
        self.case_strength_weight_evidence = float(os.getenv("CASE_STRENGTH_WEIGHT_EVIDENCE", "0.25"))
        self.case_strength_weight_legal_basis = float(
            os.getenv("CASE_STRENGTH_WEIGHT_LEGAL_BASIS", "0.25")
        )
        self.case_strength_weight_case_support = float(
            os.getenv("CASE_STRENGTH_WEIGHT_CASE_SUPPORT", "0.20")
        )
        self.case_strength_weight_procedural = float(
            os.getenv("CASE_STRENGTH_WEIGHT_PROCEDURAL", "0.15")
        )
        self.case_strength_weight_opposing = float(
            os.getenv("CASE_STRENGTH_WEIGHT_OPPOSING", "0.15")
        )
        self.prediction_local_fast_mode = (
            os.getenv("PREDICTION_LOCAL_FAST_MODE", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        # 保留旧预测服务，默认关闭一切用户可见和自动触发路径。
        self.direct_prediction_ui_enabled = (
            os.getenv("DIRECT_PREDICTION_UI_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.direct_prediction_auto_enabled = (
            os.getenv("DIRECT_PREDICTION_AUTO_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.hearing_simulation_enabled = (
            os.getenv("HEARING_SIMULATION_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.hearing_final_report_enabled = (
            os.getenv("HEARING_FINAL_REPORT_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.hearing_simulation_probability_enabled = (
            os.getenv("HEARING_SIMULATION_PROBABILITY_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        # 中国民事一审模拟庭审：所有程序上限均由配置注入，编排逻辑不得写死。
        self.civil_trial_user_turn_timeout_seconds = int(
            os.getenv("CIVIL_TRIAL_USER_TURN_TIMEOUT_SECONDS", "300")
        )
        self.civil_trial_warning_before_seconds = int(
            os.getenv("CIVIL_TRIAL_WARNING_BEFORE_SECONDS", "30")
        )
        self.civil_trial_agent_call_timeout_seconds = int(
            os.getenv("CIVIL_TRIAL_AGENT_CALL_TIMEOUT_SECONDS", "30")
        )
        self.civil_trial_agent_retry_count = int(os.getenv("CIVIL_TRIAL_AGENT_RETRY_COUNT", "1"))
        self.civil_trial_opponent_tool_limit = int(os.getenv("CIVIL_TRIAL_OPPONENT_TOOL_LIMIT", "3"))
        self.civil_trial_opponent_max_tokens = int(os.getenv("CIVIL_TRIAL_OPPONENT_MAX_TOKENS", "500"))
        self.civil_trial_procedure_max_tokens = int(os.getenv("CIVIL_TRIAL_PROCEDURE_MAX_TOKENS", "800"))
        self.civil_trial_adjudication_max_tokens = int(
            os.getenv("CIVIL_TRIAL_ADJUDICATION_MAX_TOKENS", "2400")
        )
        self.civil_trial_evidence_exchange_rounds = int(
            os.getenv("CIVIL_TRIAL_EVIDENCE_EXCHANGE_ROUNDS", "2")
        )
        self.civil_trial_issue_summary_rounds = int(
            os.getenv("CIVIL_TRIAL_ISSUE_SUMMARY_ROUNDS", "2")
        )
        self.civil_trial_simple_debate_rounds = int(
            os.getenv("CIVIL_TRIAL_SIMPLE_DEBATE_ROUNDS", "1")
        )
        self.civil_trial_ordinary_debate_rounds = int(
            os.getenv("CIVIL_TRIAL_ORDINARY_DEBATE_ROUNDS", "2")
        )
        self.civil_trial_complex_debate_rounds = int(
            os.getenv("CIVIL_TRIAL_COMPLEX_DEBATE_ROUNDS", "3")
        )
        self.civil_trial_max_user_timeouts = int(os.getenv("CIVIL_TRIAL_MAX_USER_TIMEOUTS", "3"))
        self.civil_trial_max_opponent_fallbacks = int(
            os.getenv("CIVIL_TRIAL_MAX_OPPONENT_FALLBACKS", "5")
        )
        self.civil_trial_max_total_tokens = int(
            os.getenv("CIVIL_TRIAL_MAX_TOTAL_TOKENS", "30000")
        )
        self.civil_trial_max_api_calls = int(os.getenv("CIVIL_TRIAL_MAX_API_CALLS", "80"))
        self.civil_trial_judgment_reserve_calls = int(
            os.getenv("CIVIL_TRIAL_JUDGMENT_RESERVE_CALLS", "2")
        )
        self.civil_trial_judgment_reserve_tokens = int(
            os.getenv("CIVIL_TRIAL_JUDGMENT_RESERVE_TOKENS", "5000")
        )
        self.civil_trial_budget_warning_ratio = float(
            os.getenv("CIVIL_TRIAL_BUDGET_WARNING_RATIO", "0.8")
        )
        self.civil_trial_procedure_model = os.getenv("CIVIL_TRIAL_PROCEDURE_MODEL", "").strip()
        self.civil_trial_opponent_model = os.getenv("CIVIL_TRIAL_OPPONENT_MODEL", "").strip()
        self.civil_trial_adjudication_model = os.getenv("CIVIL_TRIAL_ADJUDICATION_MODEL", "").strip()
        self.agent_collaboration_enabled = (
            os.getenv("AGENT_COLLABORATION_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.mcp_readonly_tools_enabled = (
            os.getenv("MCP_READONLY_TOOLS_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.hearing_score_evidence_weight = float(os.getenv("HEARING_SCORE_EVIDENCE_WEIGHT", "0.25"))
        self.hearing_score_legal_weight = float(os.getenv("HEARING_SCORE_LEGAL_WEIGHT", "0.20"))
        self.hearing_score_fact_weight = float(os.getenv("HEARING_SCORE_FACT_WEIGHT", "0.15"))
        self.hearing_score_procedure_weight = float(os.getenv("HEARING_SCORE_PROCEDURE_WEIGHT", "0.10"))
        self.hearing_score_case_weight = float(os.getenv("HEARING_SCORE_CASE_WEIGHT", "0.15"))
        self.hearing_score_opposition_weight = float(os.getenv("HEARING_SCORE_OPPOSITION_WEIGHT", "0.15"))
        self.reliable_answer_mode = (
            os.getenv("RELIABLE_ANSWER_MODE", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.reliable_min_support_items = int(os.getenv("RELIABLE_MIN_SUPPORT_ITEMS", "1"))
        self.reliable_max_confidence_without_cases = float(os.getenv("RELIABLE_MAX_CONFIDENCE_WITHOUT_CASES", "0.25"))
        self.rag_enabled = (
            os.getenv("RAG_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.rag_chunk_size = int(os.getenv("RAG_CHUNK_SIZE", "1800"))
        self.rag_chunk_overlap = int(os.getenv("RAG_CHUNK_OVERLAP", "180"))
        self.rag_chunk_size_tokens = int(os.getenv("RAG_CHUNK_SIZE_TOKENS", "512"))
        self.rag_chunk_overlap_tokens = int(os.getenv("RAG_CHUNK_OVERLAP_TOKENS", "64"))
        self.rag_max_context_items = int(os.getenv("RAG_MAX_CONTEXT_ITEMS", "8"))
        self.rag_hybrid_enabled = (
            os.getenv("RAG_HYBRID_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.rag_lexical_weight = float(os.getenv("RAG_LEXICAL_WEIGHT", "0.55"))
        self.rag_vector_weight = float(os.getenv("RAG_VECTOR_WEIGHT", "0.45"))
        self.rag_vector_candidate_limit = int(os.getenv("RAG_VECTOR_CANDIDATE_LIMIT", "24"))
        self.rag_lexical_candidate_limit = int(os.getenv("RAG_LEXICAL_CANDIDATE_LIMIT", "24"))
        self.rag_context_window_enabled = (
            os.getenv("RAG_CONTEXT_WINDOW_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.rag_context_window_radius = int(os.getenv("RAG_CONTEXT_WINDOW_RADIUS", "1"))
        self.rag_context_window_seed_limit = int(os.getenv("RAG_CONTEXT_WINDOW_SEED_LIMIT", "4"))
        self.rag_context_window_max_chunks = int(os.getenv("RAG_CONTEXT_WINDOW_MAX_CHUNKS", "12"))
        self.rag_context_window_token_budget = int(os.getenv("RAG_CONTEXT_WINDOW_TOKEN_BUDGET", "8000"))
        self.rag_graph_enabled = (
            os.getenv("RAG_GRAPH_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.rag_graph_weight = float(os.getenv("RAG_GRAPH_WEIGHT", "0.25"))
        self.rag_graph_candidate_limit = int(os.getenv("RAG_GRAPH_CANDIDATE_LIMIT", "20"))
        self.rag_graph_max_hops = int(os.getenv("RAG_GRAPH_MAX_HOPS", "3"))
        self.rag_graph_edge_min_confidence = float(os.getenv("RAG_GRAPH_EDGE_MIN_CONFIDENCE", "0.65"))
        # IVFFlat is the deployable baseline for memory-constrained PostgreSQL.
        # HNSW remains an optional high-recall backend on a larger remote host.
        self.rag_vector_index_type = os.getenv("RAG_VECTOR_INDEX_TYPE", "ivfflat").strip().lower()
        self.rag_ivfflat_lists = int(os.getenv("RAG_IVFFLAT_LISTS", "2048"))
        self.rag_ivfflat_probes = int(os.getenv("RAG_IVFFLAT_PROBES", "48"))
        self.rag_vector_quantization = os.getenv("RAG_VECTOR_QUANTIZATION", "none").strip().lower()
        self.vector_backend = os.getenv("VECTOR_BACKEND", "pgvector_ivfflat").strip().lower()
        self.vector_faiss_index_path = os.getenv("VECTOR_FAISS_INDEX_PATH", "").strip()
        self.vector_remote_hnsw_url = os.getenv("VECTOR_REMOTE_HNSW_URL", "").strip().rstrip("/")
        self.vector_remote_hnsw_token = os.getenv("VECTOR_REMOTE_HNSW_TOKEN", "").strip()
        self.rag_hnsw_m = int(os.getenv("RAG_HNSW_M", "16"))
        self.rag_hnsw_ef_construction = int(os.getenv("RAG_HNSW_EF_CONSTRUCTION", "64"))
        self.rag_hnsw_ef_search = int(os.getenv("RAG_HNSW_EF_SEARCH", "80"))
        self.embedding_provider = os.getenv("EMBEDDING_PROVIDER", "hash").strip().lower()
        self.embedding_model = os.getenv("EMBEDDING_MODEL", "local-hash-embedding").strip()
        self.embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "384"))
        self.embedding_base_url = os.getenv("EMBEDDING_BASE_URL", "").strip().rstrip("/")
        self.embedding_api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
        self.embedding_batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))
        self.embedding_local_model_path = os.getenv("EMBEDDING_LOCAL_MODEL_PATH", "").strip()
        self.embedding_device = os.getenv("EMBEDDING_DEVICE", "auto").strip().lower()
        self.embedding_max_length = int(os.getenv("EMBEDDING_MAX_LENGTH", "8192"))
        self.remote_search_enabled = (
            os.getenv("REMOTE_SEARCH_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.async_hydration_enabled = (
            os.getenv("ASYNC_HYDRATION_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.remote_search_trigger_count = int(os.getenv("REMOTE_SEARCH_TRIGGER_COUNT", "1"))
        self.remote_search_max_items_per_source = int(os.getenv("REMOTE_SEARCH_MAX_ITEMS_PER_SOURCE", "12"))
        self.analysis_new_case_hydration_enabled = (
            os.getenv("ANALYSIS_NEW_CASE_HYDRATION_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.analysis_new_case_target_count = int(os.getenv("ANALYSIS_NEW_CASE_TARGET_COUNT", "0"))
        self.bilingual_result_preview_limit = int(os.getenv("BILINGUAL_RESULT_PREVIEW_LIMIT", "6"))
        self.canlii_bulk_sync_enabled = (
            os.getenv("CANLII_BULK_SYNC_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.ofac_full_snapshot_enabled = (
            os.getenv("OFAC_FULL_SNAPSHOT_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.local_archive_enabled = (
            os.getenv("LOCAL_ARCHIVE_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.archive_bootstrap_enabled = (
            os.getenv("ARCHIVE_BOOTSTRAP_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.startup_canada_sync_mode = os.getenv("STARTUP_CANADA_SYNC_MODE", "background").strip().lower()
        self.local_archive_dir = os.getenv("LOCAL_ARCHIVE_DIR", "data_archive").strip()
        self.local_archive_export_dir = os.getenv("LOCAL_ARCHIVE_EXPORT_DIR", "data_archive/exports").strip()
        self.ingestion_worker_poll_seconds = float(os.getenv("INGESTION_WORKER_POLL_SECONDS", "2"))
        self.ingestion_page_poll_seconds = int(os.getenv("INGESTION_PAGE_POLL_SECONDS", "3000"))
        self.ingestion_task_requeue_cooldown_seconds = int(
            os.getenv("INGESTION_TASK_REQUEUE_COOLDOWN_SECONDS", "900")
        )
        self.ingestion_worker_stale_seconds = int(os.getenv("INGESTION_WORKER_STALE_SECONDS", "600"))
        self.ingestion_worker_name = os.getenv("INGESTION_WORKER_NAME", "").strip()
        self.agent_worker_in_process_enabled = (
            os.getenv("AGENT_WORKER_IN_PROCESS_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.agent_worker_poll_seconds = float(os.getenv("AGENT_WORKER_POLL_SECONDS", "0.8"))
        self.agent_worker_stale_seconds = int(os.getenv("AGENT_WORKER_STALE_SECONDS", "900"))
        self.agent_worker_name = os.getenv("AGENT_WORKER_NAME", "").strip()
        self.default_search_limit = int(os.getenv("DEFAULT_SEARCH_LIMIT", "10"))
        self.session_secret = os.getenv("SESSION_SECRET", "legal-demo-dev-session-secret").strip()
        if self.app_env == "production" and (
            len(self.session_secret) < 32
            or self.session_secret
            in {
                "legal-demo-dev-session-secret",
                "replace-with-a-long-random-string",
                "change_this_to_a_random_string",
            }
        ):
            raise RuntimeError(
                "SESSION_SECRET must be replaced with a random value of at least 32 characters "
                "when APP_ENV=production"
            )
        self.session_max_age_seconds = int(os.getenv("SESSION_MAX_AGE_SECONDS", "604800"))
        self.demo_history_seed_enabled = (
            os.getenv(
                "DEMO_HISTORY_SEED_ENABLED",
                "true" if self.app_env != "production" else "false",
            ).strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.initial_admin_username = os.getenv("INITIAL_ADMIN_USERNAME", "").strip()
        self.initial_admin_password = os.getenv("INITIAL_ADMIN_PASSWORD", "").strip()
        self.initial_admin_email = os.getenv("INITIAL_ADMIN_EMAIL", "").strip()

        self.canlii_database_pages = _split_csv(os.getenv("CANLII_DATABASE_PAGES", ""))
        self.canlii_remote_database_page_limit = int(os.getenv("CANLII_REMOTE_DATABASE_PAGE_LIMIT", "80"))
        self.canlii_database_discovery_ttl_seconds = int(os.getenv("CANLII_DATABASE_DISCOVERY_TTL_SECONDS", "21600"))
        self.canlii_http_proxy = os.getenv("CANLII_HTTP_PROXY", "").strip()
        self.canlii_request_delay_seconds = float(os.getenv("CANLII_REQUEST_DELAY_SECONDS", "2.0"))
        self.canlii_case_text_char_limit = int(os.getenv("CANLII_CASE_TEXT_CHAR_LIMIT", "6000"))
        self.canlii_api_base_url = os.getenv("CANLII_API_BASE_URL", "http://api.canlii.org/v1").strip().rstrip("/")
        self.canlii_api_page_size = int(os.getenv("CANLII_API_PAGE_SIZE", "100"))
        self.canada_federal_sync_max_items = int(os.getenv("CANADA_FEDERAL_SYNC_MAX_ITEMS", "0"))
        self.ontario_legislation_sync_max_items = int(os.getenv("ONTARIO_LEGISLATION_SYNC_MAX_ITEMS", "0"))
        self.ontario_legislation_include_full_text = (
            os.getenv("ONTARIO_LEGISLATION_INCLUDE_FULL_TEXT", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )

        # 数据源配置 - 支持多种数据源
        self.canada_legislation_source = os.getenv("CANADA_LEGISLATION_SOURCE", "demo").strip().lower()
        self.canada_case_source = os.getenv("CANADA_CASE_SOURCE", "demo").strip().lower()
        self.a2aj_api_base_url = os.getenv("A2AJ_API_BASE_URL", "https://api.a2aj.ca").strip().rstrip("/")
        self.a2aj_hf_case_dataset = os.getenv("A2AJ_HF_CASE_DATASET", "a2aj/canadian-case-law").strip()
        self.a2aj_hf_law_dataset = os.getenv("A2AJ_HF_LAW_DATASET", "a2aj/canadian-laws").strip()
        self.a2aj_demo_case_configs = _split_csv(os.getenv("A2AJ_DEMO_CASE_CONFIGS", "default"))
        self.a2aj_demo_law_configs = _split_csv(os.getenv("A2AJ_DEMO_LAW_CONFIGS", "default"))
        self.a2aj_demo_cases_per_config = int(os.getenv("A2AJ_DEMO_CASES_PER_CONFIG", "5"))
        self.a2aj_demo_laws_per_config = int(os.getenv("A2AJ_DEMO_LAWS_PER_CONFIG", "5"))
        self.laws_lois_xml_repo_url = os.getenv(
            "LAWS_LOIS_XML_REPO_URL",
            "https://github.com/justicecanada/laws-lois-xml.git",
        ).strip()
        self.laws_lois_xml_local_dir = os.getenv(
            "LAWS_LOIS_XML_LOCAL_DIR",
            "data/imports/laws-lois-xml",
        ).strip()
        self.laws_lois_xml_demo_limit = int(os.getenv("LAWS_LOIS_XML_DEMO_LIMIT", "10"))
        self.canlii_api_key = os.getenv("CANLII_API_KEY", "").strip()
        self.keyword_extraction_use_llm = (
            os.getenv("KEYWORD_EXTRACTION_USE_LLM", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.canlii_realtime_search_enabled = (
            os.getenv("CANLII_REALTIME_SEARCH_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.canlii_realtime_search_max_items = int(
            os.getenv("CANLII_REALTIME_SEARCH_MAX_ITEMS", "20")
        )
        self.pipeline_search_timeout_seconds = int(
            os.getenv("PIPELINE_SEARCH_TIMEOUT_SECONDS", "30")
        )
        self.canada_open_data_api_url = os.getenv("CANADA_OPEN_DATA_API_URL", "").strip()

        self.ofac_sdn_csv_url = os.getenv("OFAC_SDN_CSV_URL", "").strip()
        self.ofac_add_csv_url = os.getenv("OFAC_ADD_CSV_URL", "").strip()
        self.ofac_alt_csv_url = os.getenv("OFAC_ALT_CSV_URL", "").strip()
        self.ofac_sdn_comments_csv_url = os.getenv("OFAC_SDN_COMMENTS_CSV_URL", "").strip()

        self.llm_provider = os.getenv("LLM_PROVIDER", "spark").strip().lower()

        self.openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.openai_reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "low").strip()

        self.spark_api_password = os.getenv("SPARK_API_PASSWORD", "").strip()
        self.spark_api_key = os.getenv("SPARK_API_KEY", "").strip()
        self.spark_api_secret = os.getenv("SPARK_API_SECRET", "").strip()
        self.spark_app_id = os.getenv("SPARK_APP_ID", "").strip()
        self.spark_model = os.getenv("SPARK_MODEL", "Spark Ultra-32K").strip()
        self.spark_domain = os.getenv("SPARK_DOMAIN", "4.0Ultra").strip()
        self.spark_base_url = os.getenv(
            "SPARK_BASE_URL",
            "wss://spark-api.xf-yun.com/v4.0/chat",
        ).strip()
        self.spark_temperature = float(os.getenv("SPARK_TEMPERATURE", "0.2"))

        # 自定义模型配置 (OpenAI-compatible API)
        self.custom_api_key = os.getenv("CUSTOM_API_KEY", "").strip()
        self.custom_model = os.getenv("CUSTOM_MODEL", "").strip()
        self.custom_base_url = os.getenv("CUSTOM_BASE_URL", "").strip().rstrip("/")
        self.custom_temperature = float(os.getenv("CUSTOM_TEMPERATURE", "0.3"))
        self.custom_max_tokens = int(os.getenv("CUSTOM_MAX_TOKENS", "4096"))
        self.custom_strict_json_mode = (
            os.getenv("CUSTOM_STRICT_JSON_MODE", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        # 本地模型地址 (同一台机器时使用，设为 true 则忽略 CUSTOM_BASE_URL 使用 127.0.0.1)
        self.custom_use_local = (
            os.getenv("CUSTOM_USE_LOCAL", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )


settings = Settings()
