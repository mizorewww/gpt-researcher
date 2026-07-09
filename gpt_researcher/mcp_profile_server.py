"""MCP server entry point for the local GPT Researcher profile."""

from __future__ import annotations

import os
import re
import sys
import asyncio
import json
import time
from importlib import metadata
from json import loads
from contextlib import redirect_stdout
from datetime import UTC, datetime
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
UNVERIFIED_MARKERS = (
    "合理推断",
    "大概率",
    "缺乏实际",
    "未直接披露",
    "无法核实",
    "cannot verify",
    "unverified",
)
DEFAULT_RESEARCH_TIMEOUT_SECONDS = 1800

mcp = FastMCP(
    "gpt-researcher-codex-long",
    instructions=(
        "Run GPT Researcher using the active environment profile. "
        "For this checkout the default is Tavily + Codex long search."
    ),
)

RESEARCH_JOBS: dict[str, dict[str, Any]] = {}


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
    metrics = _report_metrics(researcher)
    total_cost = round(researcher.get_costs(), 6) if researcher else 0.0
    return (
        "---\n"
        f'task_id: "{task_id}"\n'
        f'title: "{title}"\n'
        f'query: "{query}"\n'
        f'report_type: "{report_type}"\n'
        f'report_source: "{report_source}"\n'
        f'tone: "{tone}"\n'
        f"sources_count: {metrics['sources_count']}\n"
        f"visited_urls_count: {metrics['visited_urls_count']}\n"
        f"context_chars: {metrics['context_chars']}\n"
        f"total_cost_usd: {total_cost}\n"
        "---\n"
    )


def _context_text(researcher: Any) -> str:
    context = getattr(researcher, "context", "")
    return "\n".join(context) if isinstance(context, list) else str(context or "")


def _report_metrics(researcher: Any) -> dict[str, int]:
    context = getattr(researcher, "context", None)
    context_chunks_count = (
        len(context) if isinstance(context, list) else (1 if str(context or "").strip() else 0)
    )
    return {
        "sources_count": context_chunks_count,
        "context_chunks_count": context_chunks_count,
        "context_chars": len(_context_text(researcher)),
        "visited_urls_count": len(getattr(researcher, "visited_urls", []) or []),
    }


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _current_date_context() -> str:
    current_date = datetime.now(UTC).date().isoformat()
    return (
        f"Current date: {current_date} UTC. "
        f"Dates on or before {current_date} are not future dates. "
        "When judging market data, verify source grounding and completeness relative to this date."
    )


def _query_with_current_date(query: str) -> str:
    return f"{_current_date_context()}\n\nUser query:\n{query}"


def _job_timeout_seconds() -> float:
    attempt_timeout = _env_float(
        "MCP_RESEARCH_ATTEMPT_TIMEOUT",
        DEFAULT_RESEARCH_TIMEOUT_SECONDS,
    )
    return _env_float("MCP_RESEARCH_JOB_TIMEOUT", attempt_timeout)


def _invalid_report_reason(report: str, researcher: Any) -> str | None:
    report_text = (report or "").strip()
    if not _context_text(researcher).strip():
        return "empty research context"
    metrics = _report_metrics(researcher)
    min_sources = int(_env_float("MCP_RESEARCH_MIN_VISITED_URLS", 2))
    min_context_chars = int(_env_float("MCP_RESEARCH_MIN_CONTEXT_CHARS", 2000))
    if metrics["visited_urls_count"] < min_sources:
        return f"too few visited URLs: {metrics['visited_urls_count']} < {min_sources}"
    if metrics["context_chars"] < min_context_chars:
        return f"too little research context: {metrics['context_chars']} < {min_context_chars}"
    report_lower = report_text.lower()
    if any(marker in report_lower for marker in EMPTY_REPORT_MARKERS):
        return "empty-source abstention report"
    if any(marker.lower() in report_lower for marker in UNVERIFIED_MARKERS):
        return "report contains explicit unverified/guessed-market-data markers"
    return None


async def _judge_report_quality(query: str, report: str, researcher: Any) -> dict[str, Any]:
    """Use the configured smart LLM to decide whether a report is usable."""
    hard_reason = _invalid_report_reason(report, researcher)
    if hard_reason:
        return {"ok": False, "reason": hard_reason, "confidence": 1.0}

    from gpt_researcher.utils.llm import create_chat_completion

    cfg = researcher.cfg
    context_chars = len(_context_text(researcher))
    sources_count = len(getattr(researcher, "visited_urls", []) or [])
    prompt = (
        "You are a strict quality gate for an autonomous research report. "
        "Return ONLY compact JSON with keys ok (boolean), reason (string), confidence (0-1).\n\n"
        f"{_current_date_context()} "
        "Do not reject a report merely because your model priors think this date is in the future; "
        "judge recency and future-date risk relative to the Current date above.\n\n"
        "Mark ok=false if the report does not directly answer the query, lacks concrete source-grounded facts, "
        "claims it could not gather sources, is mostly generic, omits major requested dimensions, or appears internally inconsistent. "
        "Mark ok=true only when it is a usable sourced research answer.\n\n"
        f"Query:\n{query}\n\n"
        f"Sources count: {sources_count}\n"
        f"Research context characters: {context_chars}\n\n"
        f"Report excerpt:\n{report[:12000]}"
    )
    try:
        response = await create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=cfg.smart_llm_model,
            llm_provider=cfg.smart_llm_provider,
            temperature=0,
            max_tokens=300,
            llm_kwargs=cfg.llm_kwargs,
            cost_callback=researcher.add_costs,
            reasoning_effort=getattr(cfg, "reasoning_effort", None),
        )
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        verdict = json.loads(cleaned)
        return {
            "ok": bool(verdict.get("ok")),
            "reason": str(verdict.get("reason") or "quality gate failed"),
            "confidence": float(verdict.get("confidence", 0.0)),
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"quality judge failed: {type(exc).__name__}: {exc}",
            "confidence": 0.0,
        }


def _profile(retriever: str | None = None) -> dict[str, Any]:
    return {
        "RETRIEVER": retriever or os.getenv("RETRIEVER"),
        "CODEX_SEARCH_MODE": os.getenv("CODEX_SEARCH_MODE"),
        "CODEX_SEARCH_TIMEOUT": os.getenv("CODEX_SEARCH_TIMEOUT"),
        "CODEX_SEARCH_RETRIEVER_TIMEOUT": os.getenv("CODEX_SEARCH_RETRIEVER_TIMEOUT"),
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": os.getenv("CODEX_SEARCH_RETRIEVER_CONCURRENCY"),
        "CODEX_SEARCH_MODEL": os.getenv("CODEX_SEARCH_MODEL"),
        "MCP_RESEARCH_FALLBACK_RETRIEVER": os.getenv("MCP_RESEARCH_FALLBACK_RETRIEVER"),
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


def _save_failure_audit(
    *,
    query: str,
    report_type: str,
    report_source: str,
    tone: str,
    profile: dict[str, Any],
    fallback_used: bool,
    attempts: list[dict[str, Any]],
    researcher: Any,
) -> tuple[str, Path, dict[str, Any]]:
    task_id = str(uuid4())
    title = _safe_filename(query)
    metrics = _report_metrics(researcher)
    total_cost = round(researcher.get_costs(), 6) if researcher else 0.0
    payload = {
        "task_id": task_id,
        "status": "failed",
        "reason": "all research attempts failed quality validation",
        "title": title,
        "query": query,
        "report_type": report_type,
        "report_source": report_source,
        "tone": tone,
        "sources_count": metrics["sources_count"],
        "visited_urls_count": metrics["visited_urls_count"],
        "context_chars": metrics["context_chars"],
        "total_cost_usd": total_cost,
        "profile": profile,
        "fallback_used": fallback_used,
        "attempts": attempts,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{title}.failed.json"
    index = 2
    while path.exists():
        path = OUTPUT_DIR / f"{title}_{index}.failed.json"
        index += 1
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return task_id, path, payload


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
        "MCP_RESEARCH_FALLBACK_RETRIEVER": os.getenv("MCP_RESEARCH_FALLBACK_RETRIEVER"),
        "MCP_RESEARCH_MIN_VISITED_URLS": os.getenv("MCP_RESEARCH_MIN_VISITED_URLS"),
        "MCP_RESEARCH_MIN_CONTEXT_CHARS": os.getenv("MCP_RESEARCH_MIN_CONTEXT_CHARS"),
    }


async def _run_research_report(
    query: str,
    report_type: str = "research_report",
    tone: str = "objective",
    report_source: str = "web",
) -> dict[str, Any]:
    effective_query = _query_with_current_date(query)

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
                    query=effective_query,
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
        timeout = _env_float("MCP_RESEARCH_ATTEMPT_TIMEOUT", DEFAULT_RESEARCH_TIMEOUT_SECONDS)
        async with asyncio.timeout(timeout):
            return await run_once(retriever_override)

    attempts: list[dict[str, Any]] = []
    mixed_retriever = os.getenv("RETRIEVER", "tavily,codex")
    mixed_attempts = int(os.getenv("MCP_RESEARCH_MIXED_ATTEMPTS", "2"))

    researcher = None
    report = ""
    fallback_used = False
    accepted_report = False

    for attempt_number in range(1, mixed_attempts + 1):
        try:
            researcher, report = await run_once_with_timeout()
            quality = await _judge_report_quality(query, report, researcher)
            metrics = _report_metrics(researcher)
            attempts.append(
                {
                    "attempt": attempt_number,
                    "retriever": mixed_retriever,
                    "status": "ok" if quality["ok"] else "invalid",
                    "reason": None if quality["ok"] else quality["reason"],
                    "quality": quality,
                    **metrics,
                }
            )
            if quality["ok"]:
                accepted_report = True
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
        fallback_retriever = os.getenv("MCP_RESEARCH_FALLBACK_RETRIEVER", "").strip()
        if not fallback_retriever:
            fallback_used = False
            active_profile = _profile(mixed_retriever)
            task_id, failure_path, failure_payload = _save_failure_audit(
                query=query,
                report_type=report_type,
                report_source=report_source,
                tone=tone,
                profile=active_profile,
                fallback_used=fallback_used,
                attempts=attempts,
                researcher=researcher,
            )
            failure_payload["path"] = str(failure_path)
            raise RuntimeError(json.dumps(failure_payload, ensure_ascii=False))
        fallback_used = True
        try:
            researcher, report = await run_once_with_timeout(fallback_retriever)
            quality = await _judge_report_quality(query, report, researcher)
            metrics = _report_metrics(researcher)
            attempts.append(
                {
                    "attempt": 1,
                    "retriever": fallback_retriever,
                    "status": "ok" if quality["ok"] else "invalid",
                    "reason": None if quality["ok"] else quality["reason"],
                    "quality": quality,
                    **metrics,
                }
            )
            accepted_report = bool(quality["ok"])
        except Exception as exc:
            attempts.append(
                {
                    "attempt": 1,
                    "retriever": "tavily",
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )

    active_profile = _profile(os.getenv("MCP_RESEARCH_FALLBACK_RETRIEVER") if fallback_used else mixed_retriever)
    if not accepted_report:
        task_id, failure_path, failure_payload = _save_failure_audit(
            query=query,
            report_type=report_type,
            report_source=report_source,
            tone=tone,
            profile=active_profile,
            fallback_used=fallback_used,
            attempts=attempts,
            researcher=researcher,
        )
        failure_payload["path"] = str(failure_path)
        raise RuntimeError(json.dumps(failure_payload, ensure_ascii=False))

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
        **_report_metrics(researcher),
        "total_cost_usd": round(researcher.get_costs(), 6),
        "profile": active_profile,
        "fallback_used": fallback_used,
        "attempts": attempts,
    }


@mcp.tool()
async def research_report(
    query: str,
    report_type: str = "research_report",
    tone: str = "objective",
    report_source: str = "web",
) -> dict[str, Any]:
    """Run a GPT Researcher report synchronously using the active Tavily/Codex profile."""
    timeout = _job_timeout_seconds()
    async with asyncio.timeout(timeout):
        return await _run_research_report(query, report_type, tone, report_source)


@mcp.tool()
async def research_report_start(
    query: str,
    report_type: str = "research_report",
    tone: str = "objective",
    report_source: str = "web",
) -> dict[str, Any]:
    """Start a long GPT Researcher report and return immediately with a job id."""
    job_id = str(uuid4())
    timeout = _job_timeout_seconds()
    task = asyncio.create_task(
        asyncio.wait_for(
            _run_research_report(query, report_type, tone, report_source),
            timeout=timeout,
        )
    )
    RESEARCH_JOBS[job_id] = {
        "task": task,
        "query": query,
        "report_type": report_type,
        "tone": tone,
        "report_source": report_source,
        "created_at": time.time(),
        "timeout_seconds": timeout,
    }
    return {
        "job_id": job_id,
        "status": "running",
        "query": query,
        "message": "Research job started. Poll research_report_status with this job_id.",
    }


@mcp.tool()
async def research_report_status(job_id: str) -> dict[str, Any]:
    """Return status/result for a job created by research_report_start."""
    job = RESEARCH_JOBS.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found", "error": "unknown research job id"}

    task: asyncio.Task = job["task"]
    elapsed_seconds = round(time.time() - float(job["created_at"]), 3)
    if not task.done():
        return {
            "job_id": job_id,
            "status": "running",
            "elapsed_seconds": elapsed_seconds,
            "timeout_seconds": job.get("timeout_seconds"),
            "query": job["query"],
        }

    try:
        result = task.result()
    except RuntimeError as exc:
        error_text = str(exc)
        try:
            failure = json.loads(error_text)
        except json.JSONDecodeError:
            failure = {"status": "failed", "reason": error_text}
        return {
            "job_id": job_id,
            "status": "failed",
            "elapsed_seconds": elapsed_seconds,
            "timeout_seconds": job.get("timeout_seconds"),
            "failure": failure,
        }
    except TimeoutError:
        return {
            "job_id": job_id,
            "status": "timeout",
            "elapsed_seconds": elapsed_seconds,
            "timeout_seconds": job.get("timeout_seconds"),
            "error": "research job exceeded MCP_RESEARCH_JOB_TIMEOUT",
        }
    except Exception as exc:
        return {
            "job_id": job_id,
            "status": "error",
            "elapsed_seconds": elapsed_seconds,
            "timeout_seconds": job.get("timeout_seconds"),
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "job_id": job_id,
        "status": "completed",
        "elapsed_seconds": elapsed_seconds,
        "result": result,
    }


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
