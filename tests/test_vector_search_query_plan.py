from __future__ import annotations

import json
import unittest
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
        original_execute = service._execute_pgvector_ann
        with patch.object(service, "ensure_vector_tables"), patch.object(
            service, "create_embedding", return_value=self.query_vector
        ), patch.object(
            service, "_execute_pgvector_ann", wraps=original_execute
        ) as execute:
            result = service.vector_search(
                "fixed integration query",
                module="canada",
                source_filter="all",
                limit=10,
                filters={"jurisdiction": "__no_such_jurisdiction__"},
            )
        self.assertEqual(result["items"], [])
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(
            [call.kwargs["ann_limit"] for call in execute.call_args_list],
            [40, 80],
        )


if __name__ == "__main__":
    unittest.main()
