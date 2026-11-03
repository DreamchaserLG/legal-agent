from __future__ import annotations

import unittest
from unittest.mock import patch

from app.service.legal_entity_extraction_service import extract_legal_entities
from app.service.vector_benchmark_service import (
    local_vector_experiment_manifest,
    recall_at_k,
    summarize_backend_trials,
    run_local_vector_benchmark,
    load_canada_queries,
)
from app.service import vector_index_management_service as index_service
from app.service import vector_store_service as vector_store


class VectorRolloutServiceTests(unittest.TestCase):
    def test_recall_metrics_compare_ann_to_exact(self):
        self.assertEqual(recall_at_k([1, 2, 3], [1, 4, 2], 3), 2 / 3)
        summary = summarize_backend_trials([
            {"exact_ids": [1, 2, 3], "candidate_ids": [1, 4, 2], "latency_ms": 12},
            {"exact_ids": [5, 6, 7], "candidate_ids": [5, 6, 8], "latency_ms": 20},
        ])
        self.assertEqual(summary["queries"], 2)
        self.assertGreater(summary["recall"]["recall@5"], 0.6)
        self.assertEqual(summary["latency_ms"]["max"], 20)

    def test_manifest_keeps_migration_paused(self):
        manifest = local_vector_experiment_manifest()
        self.assertFalse(manifest["migration_started"])
        self.assertEqual([row["id"] for row in manifest["experiments"]], ["pgvector_hnsw_sample", "exact_cosine"])

    def test_benchmark_refuses_sequential_scan_when_ann_missing(self):
        with patch("app.service.vector_benchmark_service.get_hnsw_index_state", return_value={"valid": False, "ready": False}):
            result = run_local_vector_benchmark(["Ontario tenancy"])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["migration_started"])

    def test_live_vector_search_refuses_hidden_full_table_scan(self):
        with patch.object(vector_store, "ensure_vector_tables"), patch.object(
            vector_store, "_pgvector_column_available", return_value=True
        ), patch(
            "app.service.vector_index_management_service.get_hnsw_index_state",
            return_value={"valid": False, "ready": False},
        ), patch.object(vector_store, "create_embedding") as create_embedding:
            result = vector_store.vector_search("Ontario sentencing principles")
        self.assertEqual(result["status"], "ann_unavailable")
        create_embedding.assert_not_called()

    def test_hnsw_plan_is_dry_run_and_explicit(self):
        with patch.object(
            index_service, "embedding_metadata", return_value={"provider": "sentence_transformers", "model": "BAAI/bge-m3", "dimension": 1024}
        ):
            plan = index_service.get_hnsw_build_plan(m=16, ef_construction=64)
        self.assertFalse(plan["will_execute"])
        self.assertFalse(plan["migration_started"])
        self.assertIn("USING hnsw", plan["sql"])
        self.assertIn("m = 16", plan["sql"])

    def test_canadian_entity_extraction_preserves_spans(self):
        result = extract_legal_entities(
            "Under the Residential Tenancies Act, 2006, section 20, the landlord must repair. See 2023 ONCA 152."
        )
        types = {item["entity_type"] for item in result["entities"]}
        self.assertIn("Statute", types)
        self.assertIn("Section", types)
        self.assertIn("Case", types)
        self.assertIn("Court", types)
        self.assertTrue(result["source_text_preserved"])

    def test_formal_canada_eval_set_is_cross_domain(self):
        cases = load_canada_queries("data/eval/canada_retrieval_eval.json")
        self.assertGreaterEqual(len(cases), 30)
        self.assertGreaterEqual(len({item["domain"] for item in cases}), 8)


if __name__ == "__main__":
    unittest.main()
