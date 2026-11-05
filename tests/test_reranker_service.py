from __future__ import annotations

import unittest

from app.service.reranker_service import rerank_legal_evidence


class LegalRerankerTests(unittest.TestCase):
    def test_ontario_authority_is_boosted_and_law_partition_is_preserved(self):
        plan = {
            "jurisdictions": ["Ontario"],
            "preferred_sources": ["case", "law"],
            "meaningful_terms": ["landlord", "tenant", "eviction"],
            "domains": ["residential tenancy"],
            "issues": ["eviction"],
        }
        items = [
            {
                "title": "Reference re Residential Tenancies Act",
                "citation": "1994 NSCA 124",
                "source_kind": "case",
                "search_channels": ["lexical", "vector"],
                "hybrid_score": 0.08,
                "vector_score": 0.7,
            },
            {
                "title": "Ontario landlord and tenant appeal",
                "citation": "2023 ONCA 789",
                "source_kind": "case",
                "search_channels": ["vector"],
                "hybrid_score": 0.07,
                "vector_score": 0.65,
            },
            {
                "title": "Residential Tenancies Act, 2006",
                "citation": "S.O. 2006, c. 17",
                "source_code": "on_statute",
                "source_kind": "law",
                "search_channels": ["lexical"],
                "hybrid_score": 0.03,
                "vector_score": 0.0,
            },
        ]

        result = rerank_legal_evidence(
            "Ontario landlord tenant eviction", items, plan=plan, limit=2
        )

        self.assertEqual(result[0]["citation"], "2023 ONCA 789")
        self.assertTrue(result[0]["rerank_signals"]["jurisdiction_match"])
        self.assertEqual({item["source_kind"] for item in result}, {"case", "law"})


if __name__ == "__main__":
    unittest.main()
