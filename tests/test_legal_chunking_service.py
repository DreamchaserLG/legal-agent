from __future__ import annotations

import unittest

from app.service.legal_chunking_service import INDEX_VERSION, split_legal_document


class LegalChunkingServiceTests(unittest.TestCase):
    def test_short_sections_are_hard_boundaries(self):
        text = """FACTS

[1] Alpha Ltd. employed the applicant.

ISSUES

[2] Was the dismissal for cause?

ANALYSIS

[3] The Court applied the governing employment principles."""
        chunks = split_legal_document(text, doc={"title": "Alpha Ltd. v Smith"}, target_tokens=100, max_tokens=120)
        self.assertEqual([item["section_title"] for item in chunks], ["FACTS", "ISSUES", "ANALYSIS"])
        self.assertEqual([item["paragraph_start"] for item in chunks], [1, 2, 3])
        self.assertTrue(chunks[0]["text"].startswith("FACTS\n"))
        self.assertTrue(chunks[1]["text"].startswith("ISSUES\n"))

    def test_context_prefix_preserves_raw_text_and_resolves_defined_alias(self):
        text = """FACTS

[1] Alpha Holdings Inc. (the \"Company\") entered the lease.

[2] The Company later terminated it."""
        chunks = split_legal_document(
            text,
            doc={"title": "Alpha Holdings Inc. v North", "metadata": {"citation": "2024 ONSC 10", "jurisdiction": "Ontario"}},
            target_tokens=12,
            max_tokens=30,
        )
        self.assertTrue(chunks)
        combined = "\n".join(item["text"] for item in chunks)
        self.assertIn("The Company later terminated it.", combined)
        self.assertTrue(all(item["index_version"] == INDEX_VERSION for item in chunks))
        self.assertTrue(all(item["disambiguation"]["source_text_preserved"] for item in chunks))
        self.assertTrue(any("Alpha Holdings Inc." in item["context_prefix"] for item in chunks))

    def test_multiple_organizations_do_not_guess_company_reference(self):
        text = """FACTS

[1] Alpha Ltd. contracted with Beta Inc.

[2] The company disputed payment."""
        chunks = split_legal_document(text, doc={"title": "Alpha Ltd. v Beta Inc."}, target_tokens=12, max_tokens=30)
        last = chunks[-1]["disambiguation"]
        self.assertIn("the company", [value.lower() for value in last["unresolved_references"]])
        self.assertEqual(last["resolved_references"], [])


if __name__ == "__main__":
    unittest.main()
