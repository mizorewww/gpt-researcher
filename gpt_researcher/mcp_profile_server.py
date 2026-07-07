"""MCP server entry point for the local GPT Researcher profile."""

from __future__ import annotations

import os
import re
import sys
import asyncio
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
EMPTY_REPORT_MARKERS = (
    "i could not gather any source material",
    "no sources were retrieved",
    "not able to produce a reliable, sourced report",
)

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


def _context_text(researcher: Any) -> str:
    context = getattr(researcher, "context", "")
    return "\n".join(context) if isinstance(context, list) else str(context or "")


def _invalid_report_reason(report: str, researcher: Any) -> str | None:
    report_text = (report or "").strip()
    if not _context_text(researcher).strip():
        return "empty research context"
    report_lower = report_text.lower()
    if any(marker in report_lower for marker in EMPTY_REPORT_MARKERS):
        return "empty-source abstention report"
    return None


def _profile(retriever: str | None = None) -> dict[str, Any]:
    return {
        "RETRIEVER": retriever or os.getenv("RETRIEVER"),
        "CODEX_SEARCH_MODE": os.getenv("CODEX_SEARCH_MODE"),
        "CODEX_SEARCH_TIMEOUT": os.getenv("CODEX_SEARCH_TIMEOUT"),
        "CODEX_SEARCH_RETRIEVER_TIMEOUT": os.getenv("CODEX_SEARCH_RETRIEVER_TIMEOUT"),
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": os.getenv("CODEX_SEARCH_RETRIEVER_CONCURRENCY"),
        "CODEX_SEARCH_MODEL": os.getenv("CODEX_SEARCH_MODEL"),
    }


def _set_env_temporarily(key: str, value: str | None):
    previous = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    return previous


def _restore_env(key: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


def _save_report(
    *,
    query: str,
    report: str,
    report_type: str,
    report_source: str,
    tone: str,
    researcher: Any,
) -> tuple[str, str, Path]:
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
    return task_id, final_markdown, path


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
    async def run_once(retriever_override: str | None = None):
        previous_retriever = _set_env_temporarily("RETRIEVER", retriever_override) if retriever_override else None
        try:
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
                return researcher, report
        finally:
            if retriever_override:
                _restore_env("RETRIEVER", previous_retriever)

    async def run_once_with_timeout(retriever_override: str | None = None):
        timeout = float(os.getenv("MCP_RESEARCH_ATTEMPT_TIMEOUT", "900"))
        async with asyncio.timeout(timeout):
            return await run_once(retriever_override)

    attempts: list[dict[str, Any]] = []
    mixed_retriever = os.getenv("RETRIEVER", "tavily,codex")
    mixed_attempts = int(os.getenv("MCP_RESEARCH_MIXED_ATTEMPTS", "2"))

    researcher = None
    report = ""
    fallback_used = False

    for attempt_number in range(1, mixed_attempts + 1):
        try:
            researcher, report = await run_once_with_timeout()
            reason = _invalid_report_reason(report, researcher)
            attempts.append(
                {
                    "attempt": attempt_number,
                    "retriever": mixed_retriever,
                    "status": "invalid" if reason else "ok",
                    "reason": reason,
                    "sources_count": len(getattr(researcher, "visited_urls", []) or []),
                    "context_chars": len(_context_text(researcher)),
                }
            )
            if not reason:
                break
        except Exception as exc:
            attempts.append(
                {
                    "attempt": attempt_number,
                    "retriever": mixed_retriever,
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    else:
        fallback_used = True
        try:
            researcher, report = await run_once_with_timeout("tavily")
            reason = _invalid_report_reason(report, researcher)
            attempts.append(
                {
                    "attempt": 1,
                    "retriever": "tavily",
                    "status": "invalid" if reason else "ok",
                    "reason": reason,
                    "sources_count": len(getattr(researcher, "visited_urls", []) or []),
                    "context_chars": len(_context_text(researcher)),
                }
            )
        except Exception as exc:
            attempts.append(
                {
                    "attempt": 1,
                    "retriever": "tavily",
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            raise

    task_id, final_markdown, path = _save_report(
        query=query,
        report=report,
        report_type=report_type,
        report_source=report_source,
        tone=tone,
        researcher=researcher,
    )

    return {
        "task_id": task_id,
        "path": str(path),
        "report": final_markdown,
        "sources_count": len(getattr(researcher, "visited_urls", []) or []),
        "total_cost_usd": round(researcher.get_costs(), 6),
        "profile": _profile("tavily" if fallback_used else mixed_retriever),
        "fallback_used": fallback_used,
        "attempts": attempts,
    }


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
