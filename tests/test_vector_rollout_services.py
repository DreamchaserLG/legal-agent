from __future__ import annotations

import unittest
from unittest.mock import patch

from app.service.legal_entity_extraction_service import extract_legal_entities
from app.service.vector_benchmark_service import (
    local_vector_experiment_manifest,
    recall_at_k,
    summarize_backend_trials,
    run_local_vector_benchmark,
)
from app.service import vector_index_management_service as index_service


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
        self.assertIn("faiss_ivfpq", [row["id"] for row in manifest["experiments"]])

    def test_benchmark_refuses_sequential_scan_when_ann_missing(self):
        with patch("app.service.vector_benchmark_service.get_vector_status", return_value={"ann_index_ready": False}):
            result = run_local_vector_benchmark(["Ontario tenancy"])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["migration_started"])

    def test_ivfflat_plan_is_dry_run_and_explicit(self):
        with patch.object(index_service, "get_vector_status", return_value={"total_embeddings": 6_587_249}), patch.object(
            index_service, "embedding_metadata", return_value={"provider": "sentence_transformers", "model": "BAAI/bge-m3", "dimension": 1024}
        ):
            plan = index_service.get_ivfflat_build_plan(lists=2048)
        self.assertFalse(plan["will_execute"])
        self.assertFalse(plan["migration_started"])
        self.assertIn("USING ivfflat", plan["sql"])
        self.assertIn("lists = 2048", plan["sql"])

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


if __name__ == "__main__":
    unittest.main()
