import unittest
from unittest.mock import Mock, patch

import requests

from app.service import embedding_service as embeddings


class QueryEmbeddingTests(unittest.TestCase):
    def setUp(self):
        options = patch.multiple(
            embeddings.settings,
            embedding_query_bge_url="https://example.invalid/embed",
            embedding_provider="sentence_transformers",
            embedding_model="BAAI/bge-m3",
            embedding_dimension=1024,
        )
        options.start()
        self.addCleanup(options.stop)

    def test_configured_bge_service_only_encodes_queries(self):
        vector = [0.5] * 1024
        response = Mock()
        response.json.return_value = {"embeddings": [vector]}
        with patch.object(embeddings._HTTP_SESSION, "post", return_value=response) as post, patch.object(
            embeddings, "_sentence_transformer_embeddings", return_value=[vector],
        ) as local:
            self.assertEqual(embeddings.create_embedding("query"), vector)
            self.assertEqual(embeddings.create_embeddings(["document"]), [vector])
        self.assertEqual(post.call_args.kwargs["json"], {"texts": ["query"], "batch_size": 1})
        local.assert_called_once_with(["document"])

    def test_remote_error_uses_local_bge(self):
        vector = [0.5] * 1024
        with patch.object(embeddings._HTTP_SESSION, "post", side_effect=requests.Timeout), patch.object(
            embeddings, "_sentence_transformer_embeddings", return_value=[vector],
        ) as local:
            self.assertEqual(embeddings.create_embedding("query"), vector)
        local.assert_called_once_with(["query"])

    def test_non_bge_model_keeps_existing_encoder(self):
        vector = [0.5] * 1024
        with patch.object(embeddings.settings, "embedding_model", "another-model"), patch.object(
            embeddings._HTTP_SESSION, "post",
        ) as post, patch.object(embeddings, "_sentence_transformer_embeddings", return_value=[vector]):
            self.assertEqual(embeddings.create_embedding("query"), vector)
        post.assert_not_called()

    def test_wrong_vector_dimensions_are_rejected(self):
        response = Mock()
        response.json.return_value = {"embeddings": [[1.0, 0.0]]}
        with patch.object(embeddings._HTTP_SESSION, "post", return_value=response), self.assertRaises(
            embeddings.EmbeddingServiceError,
        ):
            embeddings._remote_bge_query_embedding("query", "https://example.invalid/embed")
