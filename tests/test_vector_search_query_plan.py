from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service import vector_store_service as service
from app.service.embedding_service import embedding_metadata
from app.service.vector_index_management_service import (
    HNSW_INDEX_NAME,
    get_hnsw_index_state,
)


def _integration_ready() -> bool:
    try:
        state = get_hnsw_index_state()
        return not is_sqlite() and bool(state.get("valid") and state.get("ready"))
    except Exception:
        return False


def _plan_nodes(value):
    nodes = []
    if isinstance(value, dict):
        if value.get("Node Type"):
            nodes.append(value)
        for child in value.values():
            nodes.extend(_plan_nodes(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_plan_nodes(child))
    return nodes


@unittest.skipUnless(_integration_ready(), "requires the local valid pgvector HNSW index")
class VectorSearchQueryPlanIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with engine.connect() as conn:
            raw = conn.execute(
                text(
                    "SELECT embedding_vector::text FROM rag_chunk_embeddings "
                    "WHERE embedding_vector IS NOT NULL "
                    "AND embedding_provider=:provider AND embedding_model=:model "
                    "ORDER BY chunk_id LIMIT 1"
                ),
                {
                    "provider": embedding_metadata()["provider"],
                    "model": embedding_metadata()["model"],
                },
            ).scalar_one()
        cls.query_vector = [float(value) for value in raw.strip("[]").split(",")]
        cls.law_query_vector = service.create_embedding(
            "Ontario landlord tenant eviction remedy"
        )

    def test_ann_first_plan_uses_hnsw_without_large_sequential_plan(self):
        meta = embedding_metadata()
        sql, params = service._build_pgvector_ann_query(
            self.query_vector,
            meta=meta,
            module="canada",
            source_filter="all",
            filters=None,
            ann_limit=96,
        )
        with engine.begin() as conn:
            service._configure_hnsw_search(conn, ann_limit=96)
            raw_plan = conn.execute(text("EXPLAIN (FORMAT JSON) " + sql), params).scalar()
        plan = raw_plan if isinstance(raw_plan, list) else json.loads(raw_plan)
        nodes = _plan_nodes(plan)
        node_types = {str(node.get("Node Type")) for node in nodes}
        used_indexes = {str(node.get("Index Name")) for node in nodes if node.get("Index Name")}
        self.assertIn(HNSW_INDEX_NAME, used_indexes)
        self.assertNotIn("Seq Scan", node_types)
        self.assertNotIn("Parallel Seq Scan", node_types)
        self.assertNotIn("Hash Join", node_types)

    def test_filtered_expansion_plan_still_uses_hnsw(self):
        sql, params = service._build_pgvector_ann_query(
            self.law_query_vector,
            meta=embedding_metadata(),
            module="canada",
            source_filter="law",
            filters=None,
            ann_limit=20000,
            outer_limit=192,
        )
        with engine.begin() as conn:
            effective_ef = service._configure_hnsw_search(conn, ann_limit=20000)
            raw_plan = conn.execute(text("EXPLAIN (FORMAT JSON) " + sql), params).scalar()
        plan = raw_plan if isinstance(raw_plan, list) else json.loads(raw_plan)
        nodes = _plan_nodes(plan)
        node_types = {str(node.get("Node Type")) for node in nodes}
        used_indexes = {str(node.get("Index Name")) for node in nodes if node.get("Index Name")}
        self.assertEqual(effective_ef, 1000)
        self.assertIn(HNSW_INDEX_NAME, used_indexes)
        self.assertNotIn("Seq Scan", node_types)
        self.assertNotIn("Parallel Seq Scan", node_types)
        self.assertNotIn("Hash Join", node_types)

    def test_limit_40_returns_at_least_40_fresh_deduplicated_items(self):
        with patch.object(service, "ensure_vector_tables"), patch.object(
            service, "create_embedding", return_value=self.query_vector
        ):
            result = service.vector_search(
                "fixed integration query", module="canada", source_filter="all", limit=40,
            )
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(len(result["items"]), 40)

    def test_filtered_shortfall_performs_only_one_bounded_retry(self):
        with patch.object(service, "ensure_vector_tables"), patch.object(
            service, "create_embedding", return_value=self.query_vector
        ):
            result = service.vector_search(
                "fixed integration query",
                module="canada",
                source_filter="all",
                limit=10,
                filters={"jurisdiction": "__no_such_jurisdiction__"},
            )
        self.assertEqual(result["items"], [])
        self.assertEqual(result["retrieval_stats"]["ann_query_count"], 2)
        self.assertEqual(
            [attempt["ann_limit"] for attempt in result["retrieval_stats"]["ann_attempts"]],
            [40, 20000],
        )

    def test_law_partition_expands_once_and_returns_semantic_results(self):
        with patch.object(service, "ensure_vector_tables"), patch.object(
            service, "create_embedding", return_value=self.law_query_vector
        ):
            result = service.vector_search(
                "Ontario landlord tenant eviction remedy",
                module="canada",
                source_filter="law",
                limit=24,
            )
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(len(result["items"]), 24)
        self.assertLessEqual(result["retrieval_stats"]["ann_query_count"], 2)
        self.assertEqual(
            result["retrieval_stats"]["ann_attempts"][-1]["ann_limit"], 20000
        )
        self.assertTrue(all(item.get("source_kind") == "law" for item in result["items"]))


class VectorSchemaInitializationTests(unittest.TestCase):
    def test_concurrent_initialization_executes_ddl_once(self):
        calls = 0
        calls_lock = threading.Lock()

        def initialize():
            nonlocal calls
            with calls_lock:
                calls += 1

        with patch.object(service, "_VECTOR_SCHEMA_READY", False), patch.object(
            service, "_ensure_vector_tables_impl", side_effect=initialize
        ):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: service.ensure_vector_tables(), range(24)))
            self.assertEqual(calls, 1)
            self.assertTrue(service._VECTOR_SCHEMA_READY)

    def test_failed_initialization_is_not_cached(self):
        with patch.object(service, "_VECTOR_SCHEMA_READY", False), patch.object(
            service,
            "_ensure_vector_tables_impl",
            side_effect=[RuntimeError("ddl failed"), None],
        ) as initialize:
            with self.assertRaisesRegex(RuntimeError, "ddl failed"):
                service.ensure_vector_tables()
            service.ensure_vector_tables()
            self.assertEqual(initialize.call_count, 2)
            self.assertTrue(service._VECTOR_SCHEMA_READY)


if __name__ == "__main__":
    unittest.main()
