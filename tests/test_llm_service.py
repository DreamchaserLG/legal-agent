import unittest
from unittest.mock import Mock, patch

import requests

from app.service import llm_service as llm


class StructuredResponseTests(unittest.TestCase):
    def request(self, provider, *responses, **options):
        with patch.multiple(
            llm.settings,
            llm_provider=provider,
            openai_api_key='example',
            custom_api_key='example',
            custom_model='model',
            custom_base_url='https://example.invalid/v1',
            custom_use_local=False,
            custom_strict_json_mode=True,
        ), patch.object(llm._HTTP_SESSION, 'post', side_effect=responses) as post:
            result = llm.create_structured_response('legal_analysis', {}, 'Return JSON', 'Synthetic input', **options)
            return result, post

    def response(self, content=None, finish_reason='stop'):
        return Mock(
            status_code=200,
            json=Mock(return_value={
                'id': 'response-1',
                'model': 'model',
                'choices': [{'finish_reason': finish_reason, 'message': {'content': content}}],
            }),
        )

    def test_valid_json_keeps_result_metadata(self):
        for provider in ('openai', 'custom'):
            with self.subTest(provider=provider):
                result, _ = self.request(provider, self.response('{"summary":"test"}'))
                self.assertEqual(result['data'], {'summary': 'test'})
                self.assertEqual(result['provider'], provider)
                self.assertEqual(result['response_id'], 'response-1')

    def test_truncated_result_with_complete_nested_object_is_rejected(self):
        for provider in ('openai', 'custom'):
            with self.subTest(provider=provider), self.assertRaisesRegex(llm.LLMServiceError, 'truncated'):
                self.request(provider, self.response('{"analysis":{"valid":true},"citations":[', 'length'))

    def test_filtered_result_is_rejected_even_with_valid_json(self):
        for provider in ('openai', 'custom'):
            with self.subTest(provider=provider), self.assertRaisesRegex(llm.LLMServiceError, 'content filter'):
                self.request(provider, self.response('{"summary":"test"}', 'content_filter'))

    def test_missing_or_non_text_content_is_normalized(self):
        for provider in ('openai', 'custom'):
            for content in (None, [], [{'text': 'example'}], '  '):
                with self.subTest(provider=provider, content=content), self.assertRaisesRegex(llm.LLMServiceError, 'completion content'):
                    self.request(provider, self.response(content))

    def test_non_json_http_success_is_normalized(self):
        for provider in ('openai', 'custom'):
            response = Mock(status_code=200)
            response.json.side_effect = requests.exceptions.JSONDecodeError('Invalid JSON', '<html>', 0)
            with self.subTest(provider=provider), self.assertRaisesRegex(llm.LLMServiceError, 'invalid JSON'):
                self.request(provider, response)

    def test_custom_thinking_option_is_only_sent_when_requested(self):
        _, default_post = self.request("custom", self.response('{"summary":"test"}'))
        self.assertNotIn("enable_thinking", default_post.call_args.kwargs["json"])

        _, post = self.request(
            "custom", self.response('{"summary":"test"}'), model="qwen-flash", enable_thinking=False,
        )
        self.assertEqual(post.call_args.kwargs["json"]["model"], "qwen-flash")
        self.assertIs(post.call_args.kwargs["json"]["enable_thinking"], False)

    def test_custom_keeps_json_mode_compatibility_retry(self):
        rejected = Mock(status_code=400, text='response_format unsupported')
        result, post = self.request('custom', rejected, self.response('{"summary":"test"}'))
        self.assertIn('response_format', post.call_args_list[0].kwargs['json'])
        self.assertNotIn('response_format', post.call_args_list[1].kwargs['json'])
        self.assertEqual(result['http_attempts'], 2)


if __name__ == "__main__":
    unittest.main()
