"""Tinyfish Search API retriever for GPT Researcher."""

import logging
import os

import requests


class TinyfishSearch:
    """Retrieve ranked web or news results from the Tinyfish Search API."""

    def __init__(self, query, headers=None, topic="general", query_domains=None):
        self.query = query
        self.request_headers = headers or {}
        self.topic = topic
        self.query_domains = query_domains or []
        self.base_url = os.getenv(
            "TINYFISH_SEARCH_API_URL", "https://api.search.tinyfish.ai"
        )
        self.api_key = self.get_api_key()
        self.logger = logging.getLogger(__name__)

    def get_api_key(self) -> str:
        api_key = self.request_headers.get("tinyfish_api_key") or os.getenv(
            "TINYFISH_API_KEY"
        )
        if not api_key:
            raise Exception(
                "Tinyfish API key not found. Please set the "
                "TINYFISH_API_KEY environment variable."
            )
        return api_key

    def _build_query(self) -> str:
        if not self.query_domains:
            return self.query

        domain_filter = " OR ".join(
            f"site:{domain.strip()}"
            for domain in self.query_domains
            if domain and domain.strip()
        )
        if not domain_filter:
            return self.query
        return f"{self.query} ({domain_filter})"

    def search(self, max_results=10) -> list[dict[str, str]]:
        params = {"query": self._build_query()}

        location = os.getenv("TINYFISH_LOCATION")
        language = os.getenv("TINYFISH_LANGUAGE")
        if location:
            params["location"] = location
        if language:
            params["language"] = language
        if self.topic == "news":
            params["domain_type"] = "news"

        headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        }

        try:
            response = requests.get(
                self.base_url,
                headers=headers,
                params=params,
                timeout=float(os.getenv("TINYFISH_SEARCH_TIMEOUT", "30")),
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        except Exception as exc:
            self.logger.error(
                "Error fetching Tinyfish search results: %s. "
                "Resulting in empty response.",
                exc,
            )
            return []

        search_results = []
        for result in results[:max_results]:
            url = result.get("url")
            if not url:
                continue
            search_results.append(
                {
                    "title": result.get("title", ""),
                    "href": url,
                    "body": result.get("snippet", ""),
                }
            )
        return search_results
