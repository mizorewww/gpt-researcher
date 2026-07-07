"""MCP server entry point for the local GPT Researcher profile."""

from __future__ import annotations

import os
import re
import sys
from importlib import metadata
from json import loads
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP


def _load_profile_env() -> Path:
    """Load `.env` from the configured profile directory."""
    profile_dir = os.getenv("GPT_RESEARCHER_PROFILE_DIR")
    workdir = Path(profile_dir).expanduser().resolve() if profile_dir else _source_profile_dir()
    load_dotenv(workdir / ".env")
    return workdir


def _source_profile_dir() -> Path:
    """Resolve the source checkout used by `uvx --from /path gpt-researcher`."""
    try:
        dist = metadata.distribution("gpt-researcher")
        direct_url_file = next(
            file for file in (dist.files or []) if str(file).endswith("direct_url.json")
        )
        direct_url = loads(dist.locate_file(direct_url_file).read_text())
        parsed = urlparse(direct_url.get("url", ""))
        if parsed.scheme == "file":
            source = Path(unquote(parsed.path)).expanduser().resolve()
            if (source / ".env").exists():
                return source
    except Exception:
        pass
    return Path.cwd()


WORKDIR = _load_profile_env()
OUTPUT_DIR = WORKDIR / "outputs"

mcp = FastMCP(
    "gpt-researcher-codex-long",
    instructions=(
        "Run GPT Researcher using the active environment profile. "
        "For this checkout the default is Tavily + Codex long search."
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
def profile_info() -> dict[str, Any]:
    """Return the active GPT Researcher search profile without running research."""
    return {
        "workdir": str(WORKDIR),
        "RETRIEVER": os.getenv("RETRIEVER"),
        "FAST_LLM": os.getenv("FAST_LLM"),
        "SMART_LLM": os.getenv("SMART_LLM"),
        "STRATEGIC_LLM": os.getenv("STRATEGIC_LLM"),
        "EMBEDDING": os.getenv("EMBEDDING"),
        "has_TAVILY_API_KEY": bool(os.getenv("TAVILY_API_KEY")),
        "has_DEEPSEEK_API_KEY": bool(os.getenv("DEEPSEEK_API_KEY")),
        "has_OPENROUTER_API_KEY": bool(os.getenv("OPENROUTER_API_KEY")),
        "has_OPENAI_API_KEY": bool(os.getenv("OPENAI_API_KEY")),
        "CODEX_SEARCH_MODE": os.getenv("CODEX_SEARCH_MODE"),
        "CODEX_SEARCH_TIMEOUT": os.getenv("CODEX_SEARCH_TIMEOUT"),
        "CODEX_SEARCH_RETRIEVER_TIMEOUT": os.getenv("CODEX_SEARCH_RETRIEVER_TIMEOUT"),
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": os.getenv("CODEX_SEARCH_RETRIEVER_CONCURRENCY"),
        "CODEX_SEARCH_MODEL": os.getenv("CODEX_SEARCH_MODEL"),
        "CODEX_SEARCH_REASONING_EFFORT": os.getenv("CODEX_SEARCH_REASONING_EFFORT"),
        "CODEX_SEARCH_SERVICE_TIER": os.getenv("CODEX_SEARCH_SERVICE_TIER"),
        "CODEX_SEARCH_SUPPORTS_WEBSOCKETS": os.getenv("CODEX_SEARCH_SUPPORTS_WEBSOCKETS"),
    }


@mcp.tool()
async def research_report(
    query: str,
    report_type: str = "research_report",
    tone: str = "objective",
    report_source: str = "web",
) -> dict[str, Any]:
    """Run a GPT Researcher report using the active Tavily/Codex profile."""
    with redirect_stdout(sys.stderr):
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


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
