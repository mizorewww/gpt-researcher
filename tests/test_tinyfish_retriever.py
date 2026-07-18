import os
import unittest
from unittest.mock import MagicMock, patch

from gpt_researcher.actions.retriever import get_retriever
from gpt_researcher.retrievers.tinyfish.tinyfish_search import TinyfishSearch


class TestTinyfishSearch(unittest.TestCase):
    def test_missing_api_key_raises_clear_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(Exception, "TINYFISH_API_KEY"):
                TinyfishSearch("test query")

    def test_factory_returns_tinyfish_retriever(self):
        self.assertIs(get_retriever("tinyfish"), TinyfishSearch)

    @patch("gpt_researcher.retrievers.tinyfish.tinyfish_search.requests.get")
    def test_search_normalizes_results_and_applies_options(self, mock_get):
        response = MagicMock()
        response.json.return_value = {
            "results": [
                {
                    "url": "https://example.com/one",
                    "title": "One",
                    "snippet": "First snippet",
                },
                {
                    "url": "https://example.com/two",
                    "title": "Two",
                    "snippet": "Second snippet",
                },
            ]
        }
        mock_get.return_value = response

        env = {
            "TINYFISH_API_KEY": "test-key",
            "TINYFISH_LOCATION": "SG",
            "TINYFISH_LANGUAGE": "zh",
        }
        with patch.dict(os.environ, env, clear=True):
            results = TinyfishSearch(
                "market news",
                topic="news",
                query_domains=["example.com", "reuters.com"],
            ).search(max_results=1)

        mock_get.assert_called_once_with(
            "https://api.search.tinyfish.ai",
            headers={"X-API-Key": "test-key", "Accept": "application/json"},
            params={
                "query": (
                    "market news "
                    "(site:example.com OR site:reuters.com)"
                ),
                "location": "SG",
                "language": "zh",
                "domain_type": "news",
            },
            timeout=30.0,
        )
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(
            results,
            [
                {
                    "href": "https://example.com/one",
                    "title": "One",
                    "body": "First snippet",
                }
            ],
        )

    @patch("gpt_researcher.retrievers.tinyfish.tinyfish_search.requests.get")
    def test_header_api_key_takes_precedence(self, mock_get):
        response = MagicMock()
        response.json.return_value = {"results": []}
        mock_get.return_value = response

        with patch.dict(os.environ, {"TINYFISH_API_KEY": "env-key"}, clear=True):
            TinyfishSearch(
                "query", headers={"tinyfish_api_key": "header-key"}
            ).search()

        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["X-API-Key"], "header-key"
        )


if __name__ == "__main__":
    unittest.main()
