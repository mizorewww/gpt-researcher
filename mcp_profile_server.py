#!/usr/bin/env python3
"""Local MCP server for this repository's GPT Researcher profile.

The server intentionally runs the checked-out code in this repository, not a
published `gpt-researcher` package. It loads `.env`, so the default profile is
the local Tavily + Codex long-search configuration.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = REPO_ROOT / "outputs"

os.chdir(REPO_ROOT)
load_dotenv(REPO_ROOT / ".env")

mcp = FastMCP(
    "gpt-researcher-codex-long",
    instructions=(
        "Run GPT Researcher using this repository's default profile: "
        "Tavily + Codex long search, Codex timeout 300s, HTTPS-only Codex transport."
    ),
)


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "research_report"


def _frontmatter(
    *,
    task_id: str,
    title: str,
    query: str,
    report_type: str,
    report_source: str,
    tone: str,
    researcher: Any,
) -> str:
    sources_count = len(getattr(researcher, "visited_urls", []) or [])
    total_cost = round(researcher.get_costs(), 6) if researcher else 0.0
    return (
        "---\n"
        f'task_id: "{task_id}"\n'
        f'title: "{title}"\n'
        f'query: "{query}"\n'
        f'report_type: "{report_type}"\n'
        f'report_source: "{report_source}"\n'
        f'tone: "{tone}"\n'
        f"sources_count: {sources_count}\n"
        f"total_cost_usd: {total_cost}\n"
        "---\n"
    )


@mcp.tool()
async def research_report(
    query: str,
    report_type: str = "research_report",
    tone: str = "objective",
    report_source: str = "web",
) -> dict[str, Any]:
    """Run a GPT Researcher report using the local Tavily+Codex long-search profile."""
    from gpt_researcher import GPTResearcher
    from gpt_researcher.utils.enum import Tone

    tone_map = {
        "objective": Tone.Objective,
        "formal": Tone.Formal,
        "analytical": Tone.Analytical,
        "persuasive": Tone.Persuasive,
        "informative": Tone.Informative,
        "explanatory": Tone.Explanatory,
        "descriptive": Tone.Descriptive,
        "critical": Tone.Critical,
        "comparative": Tone.Comparative,
        "speculative": Tone.Speculative,
        "reflective": Tone.Reflective,
        "narrative": Tone.Narrative,
        "humorous": Tone.Humorous,
        "optimistic": Tone.Optimistic,
        "pessimistic": Tone.Pessimistic,
    }

    researcher = GPTResearcher(
        query=query,
        report_type=report_type,
        report_source=report_source,
        tone=tone_map.get(tone, Tone.Objective),
        verbose=True,
    )
    await researcher.conduct_research()
    report = await researcher.write_report()

    task_id = str(uuid4())
    title = _safe_filename(query)
    final_markdown = _frontmatter(
        task_id=task_id,
        title=title,
        query=query,
        report_type=report_type,
        report_source=report_source,
        tone=tone,
        researcher=researcher,
    ) + report

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{title}.md"
    index = 2
    while path.exists():
        path = OUTPUT_DIR / f"{title}_{index}.md"
        index += 1
    path.write_text(final_markdown, encoding="utf-8")

    return {
        "task_id": task_id,
        "path": str(path),
        "report": final_markdown,
        "sources_count": len(getattr(researcher, "visited_urls", []) or []),
        "total_cost_usd": round(researcher.get_costs(), 6),
        "profile": {
            "RETRIEVER": os.getenv("RETRIEVER"),
            "CODEX_SEARCH_MODE": os.getenv("CODEX_SEARCH_MODE"),
            "CODEX_SEARCH_TIMEOUT": os.getenv("CODEX_SEARCH_TIMEOUT"),
            "CODEX_SEARCH_RETRIEVER_TIMEOUT": os.getenv("CODEX_SEARCH_RETRIEVER_TIMEOUT"),
            "CODEX_SEARCH_RETRIEVER_CONCURRENCY": os.getenv("CODEX_SEARCH_RETRIEVER_CONCURRENCY"),
            "CODEX_SEARCH_MODEL": os.getenv("CODEX_SEARCH_MODEL"),
        },
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
