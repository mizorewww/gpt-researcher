"""Codex CLI web-search retriever.

This retriever calls the local ``codex_search/codex_search.py`` helper as a
bounded subprocess. It is designed to be used alongside ordinary retrievers
such as Tavily; failures return an empty result list so the main research flow
can continue.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class CodexSearch:
    """Search retriever backed by local Codex CLI web search."""

    def __init__(self, query: str, query_domains=None, **kwargs: Any):
        self.query = query
        self.query_domains = query_domains or []
        self.repo_root = Path(__file__).resolve().parents[3]
        self.helper_path = Path(
            os.getenv("CODEX_SEARCH_HELPER", self.repo_root / "codex_search" / "codex_search.py")
        )
        self.timeout = int(os.getenv("CODEX_SEARCH_RETRIEVER_TIMEOUT", os.getenv("CODEX_SEARCH_TIMEOUT", "180")))
        self.retries = int(os.getenv("CODEX_SEARCH_RETRIEVER_RETRIES", "1"))
        self.retry_delay = float(os.getenv("CODEX_SEARCH_RETRIEVER_RETRY_DELAY", "2"))
        self.mode = os.getenv("CODEX_SEARCH_RETRIEVER_MODE", os.getenv("CODEX_SEARCH_MODE", "search"))
        self.model = os.getenv("CODEX_SEARCH_MODEL") or os.getenv("CODEX_MODEL")
        self.max_chars = int(os.getenv("CODEX_SEARCH_RETRIEVER_MAX_CHARS", "12000"))
        self.debug = os.getenv("CODEX_SEARCH_RETRIEVER_DEBUG", "").lower() in {"1", "true", "yes"}

    def search(self, max_results: int = 5) -> list[dict[str, str]]:
        """Run Codex search and return GPT Researcher-compatible results."""
        if not self.helper_path.exists():
            return []

        prompt = self._build_query()
        last_error = ""

        for attempt in range(1, self.retries + 2):
            started = time.monotonic()
            cmd = [
                sys.executable,
                str(self.helper_path),
                "--mode",
                self.mode,
                "--timeout",
                str(self.timeout),
                prompt,
            ]
            if self.model:
                cmd.extend(["--model", self.model])
            if self.debug:
                cmd.append("--show-events")

            try:
                completed = subprocess.run(
                    cmd,
                    cwd=str(self.repo_root),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout + 30,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                last_error = f"codex search timed out after {self.timeout}s"
            else:
                if completed.returncode == 0 and completed.stdout.strip():
                    body = completed.stdout.strip()[: self.max_chars]
                    elapsed = time.monotonic() - started
                    if self.debug:
                        print(
                            f"CodexSearch ok: mode={self.mode} elapsed={elapsed:.1f}s "
                            f"chars={len(completed.stdout.strip())} query={self.query[:120]}",
                            file=sys.stderr,
                        )
                    return [
                        {
                            "title": f"Codex web search: {self.query[:80]}",
                            "href": "codex-search://local",
                            "body": f"{body}\n\n[Codex search elapsed: {elapsed:.1f}s]",
                            "raw_content": body,
                        }
                    ][:max_results]
                last_error = (completed.stderr or completed.stdout or "").strip()[:1000]

            if attempt <= self.retries:
                time.sleep(self.retry_delay)

        if self.debug and last_error:
            print(f"CodexSearch failed: mode={self.mode} query={self.query[:120]} error={last_error}", file=sys.stderr)
        return []

    def _build_query(self) -> str:
        domain_hint = ""
        if self.query_domains:
            domain_hint = "\nRestrict or prioritize these domains when useful: " + ", ".join(self.query_domains)

        return (
            "Research this query for GPT Researcher. Return source-backed findings that can be "
            "merged with other retrievers. Include links and caveats. Keep the answer concise."
            f"{domain_hint}\n\nQuery: {self.query}"
        )
