"""MCP server entry point for the local GPT Researcher profile."""

from __future__ import annotations

import os
import re
import sys
import asyncio
import json
from importlib import metadata
from json import loads
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from gpt_researcher.evidence import canonical_http_url
from gpt_researcher.job_manager import (
    JobManager,
    JobQueueFullError,
    atomic_write_json,
    atomic_write_text,
    default_global_slot_root,
    read_json,
)


def _load_profile_env() -> Path:
    """Load `.env` from the configured profile directory."""
    profile_dir = os.getenv("GPT_RESEARCHER_PROFILE_DIR")
    workdir = (
        Path(profile_dir).expanduser().resolve()
        if profile_dir
        else _source_profile_dir()
    )
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
DEFAULT_RESEARCH_TIMEOUT_SECONDS = 2700
DEFAULT_TIMEZONE = "Asia/Singapore"

_HTTP_URL_PATTERN = r"https?://[^\s<>\[\]()|]+"

MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

mcp = FastMCP(
    "gpt-researcher-codex-long",
    instructions=(
        "Use research_report(query) for a complete investigation. The call waits "
        "until the report is finished and returns it directly. Independent calls "
        "may be issued concurrently."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
)

_JOB_MANAGER: JobManager | None = None


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
        f"http_sources_count: {metrics['http_sources_count']}\n"
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
        len(context)
        if isinstance(context, list)
        else (1 if str(context or "").strip() else 0)
    )
    evidence_urls: set[str] = set()
    for item in getattr(researcher, "evidence_items", []) or []:
        if isinstance(item, dict):
            url = item.get("source_url") or item.get("url")
        else:
            url = getattr(item, "source_url", None) or getattr(item, "url", None)
        if isinstance(url, str) and urlparse(url).scheme.lower() in {"http", "https"}:
            evidence_urls.add(url)
    return {
        "sources_count": context_chunks_count,
        "context_chunks_count": context_chunks_count,
        "context_chars": len(_context_text(researcher)),
        "visited_urls_count": len(getattr(researcher, "visited_urls", []) or []),
        "http_sources_count": len(evidence_urls),
    }


def _writer_evidence_catalog(researcher: Any, max_chars: int = 64_000) -> str:
    """Build a domain-neutral, source-addressable evidence catalog."""

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    size = 0
    for raw_item in getattr(researcher, "evidence_items", []) or []:
        if isinstance(raw_item, dict):
            raw = raw_item
        elif hasattr(raw_item, "to_dict"):
            raw = raw_item.to_dict()
        else:
            raw = {
                key: getattr(raw_item, key, None)
                for key in (
                    "claim",
                    "value",
                    "unit",
                    "as_of_date",
                    "source_url",
                    "source_title",
                    "retriever",
                    "retrieved_at",
                    "summary",
                    "checksum",
                )
            }
        source_url = canonical_http_url(str(raw.get("source_url") or ""))
        if source_url is None:
            continue
        record = {
            "claim": str(raw.get("claim") or "").strip(),
            "value": raw.get("value"),
            "unit": str(raw.get("unit") or "").strip() or None,
            "as_of_date": str(raw.get("as_of_date") or "").strip() or None,
            "source_url": source_url,
            "source_title": str(raw.get("source_title") or "").strip(),
            "retriever": str(raw.get("retriever") or "").strip(),
            "retrieved_at": str(raw.get("retrieved_at") or "").strip() or None,
            "summary": str(raw.get("summary") or "").strip(),
            "checksum": str(raw.get("checksum") or "").strip() or None,
        }
        fingerprint = json.dumps(
            record, ensure_ascii=False, sort_keys=True, default=str
        )
        if fingerprint in seen:
            continue
        encoded_size = len(fingerprint.encode("utf-8"))
        if size + encoded_size > max_chars:
            break
        seen.add(fingerprint)
        records.append(record)
        size += encoded_size
    return json.dumps(
        {"evidence": records},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _redact_error(value: object) -> str:
    text = str(value)
    for key, secret in os.environ.items():
        if len(secret) >= 8 and any(
            marker in key.upper()
            for marker in ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
        ):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    return text[:4000]


def _resolve_target_date(
    query: str,
    target_date: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> tuple[str, str]:
    """Resolve relative dates once, when a job is submitted."""
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {timezone}") from exc
    today = datetime.now(zone).date()
    value = (target_date or "").strip().lower()
    if not value:
        lowered_query = query.lower()
        value = (
            "yesterday"
            if re.search(r"(?:\byesterday\b|昨天|昨日)", lowered_query)
            else "today"
        )
    if value in {"yesterday", "昨天", "昨日"}:
        resolved = today - timedelta(days=1)
    elif value in {"today", "今天", "今日"}:
        resolved = today
    else:
        try:
            resolved = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(
                "target_date must be YYYY-MM-DD, today, or yesterday"
            ) from exc
    return resolved.isoformat(), timezone


def _current_date_context(
    *, target_date: str | None = None, timezone: str = DEFAULT_TIMEZONE
) -> str:
    try:
        current_date = datetime.now(ZoneInfo(timezone)).date().isoformat()
    except ZoneInfoNotFoundError:
        current_date = datetime.now(ZoneInfo("UTC")).date().isoformat()
    target_context = f" Target date: {target_date}." if target_date else ""
    return (
        f"Current date: {current_date} in {timezone}.{target_context} "
        f"Dates on or before {current_date} are not future dates. "
        "When judging time-sensitive facts, verify grounding and completeness relative to this date."
    )


def _query_with_current_date(
    query: str, *, target_date: str | None = None, timezone: str = DEFAULT_TIMEZONE
) -> str:
    return (
        f"{_current_date_context(target_date=target_date, timezone=timezone)}\n"
        "Treat the frozen target date above as authoritative for all relative date references.\n\n"
        f"User query:\n{query}"
    )


def _job_timeout_seconds() -> float:
    return _env_float("MCP_RESEARCH_JOB_TIMEOUT", DEFAULT_RESEARCH_TIMEOUT_SECONDS)


def _evidence_source_urls(researcher: Any) -> set[str]:
    urls: set[str] = set()
    for item in getattr(researcher, "evidence_items", []) or []:
        if isinstance(item, dict):
            raw_url = item.get("source_url") or item.get("url")
        else:
            raw_url = getattr(item, "source_url", None) or getattr(item, "url", None)
        canonical = canonical_http_url(str(raw_url or ""))
        if canonical is not None:
            urls.add(canonical)
    return urls


def _invalid_evidence_reason(researcher: Any) -> str | None:
    """Apply only domain-neutral evidence gates to a completed retrieval."""

    if not _context_text(researcher).strip():
        return "empty research context"
    metrics = _report_metrics(researcher)
    min_sources = int(_env_float("MCP_RESEARCH_MIN_HTTP_SOURCES", 2))
    min_context_chars = int(_env_float("MCP_RESEARCH_MIN_CONTEXT_CHARS", 2000))
    if metrics["http_sources_count"] < min_sources:
        return f"too few HTTP evidence sources: {metrics['http_sources_count']} < {min_sources}"
    if metrics["context_chars"] < min_context_chars:
        return f"too little research context: {metrics['context_chars']} < {min_context_chars}"

    evidence_metrics = getattr(researcher, "evidence_metrics", {})
    if isinstance(evidence_metrics, dict) and evidence_metrics:
        minimum_per_item = int(
            evidence_metrics.get("minimum_http_sources_per_work_item", 1) or 1
        )
        per_work_item = evidence_metrics.get("per_work_item_http_sources", {})
        if isinstance(per_work_item, dict):
            insufficient = {
                str(key): int(value or 0)
                for key, value in per_work_item.items()
                if int(value or 0) < minimum_per_item
            }
            if insufficient:
                return (
                    "insufficient HTTP evidence for research work items: "
                    f"{insufficient}; each requires {minimum_per_item}"
                )
    return None


def _invalid_report_reason(
    report: str,
    researcher: Any,
    query: str = "",
    target_date: str | None = None,
) -> str | None:
    """Validate a draft without rewriting domain content."""

    del query, target_date
    evidence_reason = _invalid_evidence_reason(researcher)
    if evidence_reason:
        return evidence_reason
    report_text = (report or "").strip()
    if any(marker in report_text.lower() for marker in EMPTY_REPORT_MARKERS):
        return "empty-source abstention report"
    report_urls = {
        canonical
        for raw_url in re.findall(_HTTP_URL_PATTERN, report_text)
        if (canonical := canonical_http_url(raw_url.rstrip(".,;"))) is not None
    }
    evidence_urls = _evidence_source_urls(researcher)
    if evidence_urls:
        unsupported = sorted(report_urls - evidence_urls)
        if unsupported:
            return (
                f"report cites {len(unsupported)} URL(s) absent from retrieved evidence: "
                + json.dumps(unsupported[:10], ensure_ascii=False)
            )
    return None


def _write_worker_phase(phase: str, **changes: Any) -> None:
    job_dir_value = os.getenv("MCP_RESEARCH_JOB_DIR")
    if not job_dir_value:
        return
    path = Path(job_dir_value) / "worker_status.json"
    status = read_json(path, {})
    if not isinstance(status, dict):
        status = {}
    status.update(changes)
    status["phase"] = phase
    atomic_write_json(path, status)


async def _judge_report_quality(
    query: str,
    report: str,
    researcher: Any,
    *,
    target_date: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """Use the configured smart LLM to decide whether a report is usable."""
    hard_reason = _invalid_report_reason(
        report, researcher, query, target_date=target_date
    )
    if hard_reason:
        return {
            "ok": False,
            "reason": hard_reason,
            "confidence": 1.0,
            "judge_error": False,
        }

    from gpt_researcher.utils.llm import create_chat_completion

    cfg = researcher.cfg
    context_chars = len(_context_text(researcher))
    sources_count = len(getattr(researcher, "visited_urls", []) or [])
    conflicts = getattr(researcher, "evidence_conflicts", []) or []
    prompt = (
        "You are a strict quality gate for an autonomous research report. "
        "Return ONLY compact JSON with keys ok (boolean), reason (string), confidence (0-1).\n\n"
        f"{_current_date_context(target_date=target_date, timezone=timezone)} "
        "Do not reject a report merely because your model priors think this date is in the future; "
        "judge recency and future-date risk relative to the Current date above.\n\n"
        "Mark ok=false if the report does not directly answer the query, lacks concrete source-grounded facts, "
        "claims it could not gather sources, is mostly generic, omits major requested dimensions, or appears internally inconsistent. "
        "Mark ok=true only when it is a usable sourced research answer.\n\n"
        f"Query:\n{query}\n\n"
        f"Sources count: {sources_count}\n"
        f"Research context characters: {context_chars}\n\n"
        f"Structured numeric conflicts requiring explicit resolution or explanation:\n"
        f"{json.dumps(conflicts, ensure_ascii=False)[:5000]}\n\n"
        f"Report excerpt:\n{report[:30000]}"
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
            cleaned = re.sub(
                r"^```(?:json)?\s*|\s*```$",
                "",
                cleaned,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
        verdict = json.loads(cleaned)
        return {
            "ok": verdict.get("ok") is True,
            "reason": str(verdict.get("reason") or "quality gate failed"),
            "confidence": float(verdict.get("confidence", 0.0)),
            "judge_error": False,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"quality judge failed: {type(exc).__name__}: {_redact_error(exc)}",
            "confidence": 0.0,
            "judge_error": True,
        }


def _profile(retriever: str | None = None) -> dict[str, Any]:
    return {
        "RETRIEVER": retriever or os.getenv("RETRIEVER"),
        "CODEX_SEARCH_MODE": os.getenv("CODEX_SEARCH_MODE"),
        "CODEX_SEARCH_TIMEOUT": os.getenv("CODEX_SEARCH_TIMEOUT"),
        "CODEX_SEARCH_RETRIEVER_TIMEOUT": os.getenv("CODEX_SEARCH_RETRIEVER_TIMEOUT"),
        "CODEX_SEARCH_MAX_RESULTS": os.getenv("CODEX_SEARCH_MAX_RESULTS"),
        "CODEX_SEARCH_RETRIEVER_RETRIES": os.getenv("CODEX_SEARCH_RETRIEVER_RETRIES"),
        "CODEX_SEARCH_RETRIEVER_RETRY_DELAY": os.getenv(
            "CODEX_SEARCH_RETRIEVER_RETRY_DELAY"
        ),
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": os.getenv(
            "CODEX_SEARCH_RETRIEVER_CONCURRENCY"
        ),
        "CODEX_SEARCH_GLOBAL_CONCURRENCY": os.getenv("CODEX_SEARCH_GLOBAL_CONCURRENCY"),
        "CODEX_SEARCH_MODEL": os.getenv("CODEX_SEARCH_MODEL"),
        "CODEX_SEARCH_REASONING_EFFORT": os.getenv("CODEX_SEARCH_REASONING_EFFORT"),
        "CODEX_SEARCH_SERVICE_TIER": os.getenv("CODEX_SEARCH_SERVICE_TIER"),
        "SEARCH_RETRIEVER_CONCURRENCY": os.getenv("SEARCH_RETRIEVER_CONCURRENCY"),
        "MAX_SCRAPER_WORKERS": os.getenv("MAX_SCRAPER_WORKERS"),
        "MCP_RESEARCH_FALLBACK_RETRIEVER": os.getenv("MCP_RESEARCH_FALLBACK_RETRIEVER"),
        "MCP_RESEARCH_MIN_HTTP_SOURCES": os.getenv("MCP_RESEARCH_MIN_HTTP_SOURCES"),
        "MCP_RESEARCH_RETRIEVAL_ATTEMPTS": os.getenv("MCP_RESEARCH_RETRIEVAL_ATTEMPTS"),
        "MCP_RESEARCH_WRITER_ATTEMPTS": os.getenv("MCP_RESEARCH_WRITER_ATTEMPTS"),
        "MCP_RESEARCH_JUDGE_ATTEMPTS": os.getenv("MCP_RESEARCH_JUDGE_ATTEMPTS"),
        "MCP_RESEARCH_RETRIEVAL_TIMEOUT": os.getenv("MCP_RESEARCH_RETRIEVAL_TIMEOUT"),
        "MCP_RESEARCH_WRITER_TIMEOUT": os.getenv("MCP_RESEARCH_WRITER_TIMEOUT"),
        "MCP_RESEARCH_JUDGE_TIMEOUT": os.getenv("MCP_RESEARCH_JUDGE_TIMEOUT"),
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
    final_markdown = (
        _frontmatter(
            task_id=task_id,
            title=title,
            query=query,
            report_type=report_type,
            report_source=report_source,
            tone=tone,
            researcher=researcher,
        )
        + report
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{task_id}.md"
    atomic_write_text(path, final_markdown)
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
    evidence_metrics = getattr(researcher, "evidence_metrics", {}) if researcher else {}
    if not isinstance(evidence_metrics, dict):
        evidence_metrics = {}
    evidence_items = (
        [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in (getattr(researcher, "evidence_items", []) or [])
            if hasattr(item, "to_dict") or isinstance(item, dict)
        ]
        if researcher
        else []
    )
    work_items = (
        [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in (getattr(researcher, "research_work_items", []) or [])
            if hasattr(item, "to_dict") or isinstance(item, dict)
        ]
        if researcher
        else []
    )
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
        "http_sources_count": metrics["http_sources_count"],
        "visited_urls_count": metrics["visited_urls_count"],
        "context_chars": metrics["context_chars"],
        "total_cost_usd": total_cost,
        "profile": profile,
        "fallback_used": fallback_used,
        "attempts": attempts,
        "work_item_count": len(work_items),
        "research_work_items": work_items,
        "evidence_metrics": evidence_metrics,
        "evidence_items": evidence_items,
        "evidence_conflicts": list(getattr(researcher, "evidence_conflicts", []) or [])
        if researcher
        else [],
        "source_urls": sorted(
            {
                str(item.get("source_url"))
                for item in evidence_items
                if item.get("source_url")
            }
        ),
        "codex_initial_calls": int(evidence_metrics.get("codex_initial_calls", 0) or 0),
        "codex_total_calls": int(evidence_metrics.get("codex_calls", 0) or 0),
        "active_codex_peak": int(evidence_metrics.get("active_codex_peak", 0) or 0),
        "codex_pids": list(evidence_metrics.get("codex_pids", []) or []),
        "codex_runs": list(evidence_metrics.get("codex_runs", []) or []),
        "quality_gate_passed": False,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{task_id}.failed.json"
    atomic_write_json(path, payload)
    return task_id, path, payload


def profile_info() -> dict[str, Any]:
    """Return the active search profile for internal diagnostics."""
    return {
        "workdir": str(WORKDIR),
        "RETRIEVER": os.getenv("RETRIEVER"),
        "FAST_LLM": os.getenv("FAST_LLM"),
        "SMART_LLM": os.getenv("SMART_LLM"),
        "STRATEGIC_LLM": os.getenv("STRATEGIC_LLM"),
        "EMBEDDING": os.getenv("EMBEDDING"),
        "LANGUAGE": os.getenv("LANGUAGE", "english"),
        "TOTAL_WORDS": os.getenv("TOTAL_WORDS", "1200"),
        "SMART_TOKEN_LIMIT": os.getenv("SMART_TOKEN_LIMIT", "6000"),
        "has_TAVILY_API_KEY": bool(os.getenv("TAVILY_API_KEY")),
        "has_DEEPSEEK_API_KEY": bool(os.getenv("DEEPSEEK_API_KEY")),
        "has_OPENROUTER_API_KEY": bool(os.getenv("OPENROUTER_API_KEY")),
        "has_OPENAI_API_KEY": bool(os.getenv("OPENAI_API_KEY")),
        "CODEX_SEARCH_MODE": os.getenv("CODEX_SEARCH_MODE"),
        "CODEX_SEARCH_TIMEOUT": os.getenv("CODEX_SEARCH_TIMEOUT"),
        "CODEX_SEARCH_RETRIEVER_TIMEOUT": os.getenv("CODEX_SEARCH_RETRIEVER_TIMEOUT"),
        "CODEX_SEARCH_MAX_RESULTS": os.getenv("CODEX_SEARCH_MAX_RESULTS"),
        "CODEX_SEARCH_RETRIEVER_RETRIES": os.getenv("CODEX_SEARCH_RETRIEVER_RETRIES"),
        "CODEX_SEARCH_RETRIEVER_RETRY_DELAY": os.getenv(
            "CODEX_SEARCH_RETRIEVER_RETRY_DELAY"
        ),
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": os.getenv(
            "CODEX_SEARCH_RETRIEVER_CONCURRENCY"
        ),
        "CODEX_SEARCH_GLOBAL_CONCURRENCY": os.getenv(
            "CODEX_SEARCH_GLOBAL_CONCURRENCY", "9"
        ),
        "CODEX_SEARCH_MODEL": os.getenv("CODEX_SEARCH_MODEL"),
        "CODEX_SEARCH_REASONING_EFFORT": os.getenv("CODEX_SEARCH_REASONING_EFFORT"),
        "CODEX_SEARCH_SERVICE_TIER": os.getenv("CODEX_SEARCH_SERVICE_TIER"),
        "CODEX_SEARCH_SUPPORTS_WEBSOCKETS": os.getenv(
            "CODEX_SEARCH_SUPPORTS_WEBSOCKETS"
        ),
        "SEARCH_RETRIEVER_CONCURRENCY": os.getenv("SEARCH_RETRIEVER_CONCURRENCY", "4"),
        "MAX_SCRAPER_WORKERS": os.getenv("MAX_SCRAPER_WORKERS", "5"),
        "MCP_RESEARCH_FALLBACK_RETRIEVER": os.getenv("MCP_RESEARCH_FALLBACK_RETRIEVER"),
        "MCP_RESEARCH_MIN_VISITED_URLS": os.getenv("MCP_RESEARCH_MIN_VISITED_URLS"),
        "MCP_RESEARCH_MIN_CONTEXT_CHARS": os.getenv("MCP_RESEARCH_MIN_CONTEXT_CHARS"),
        "MCP_RESEARCH_MIN_HTTP_SOURCES": os.getenv("MCP_RESEARCH_MIN_HTTP_SOURCES"),
        "MCP_RESEARCH_MAX_CONCURRENT_JOBS": os.getenv(
            "MCP_RESEARCH_MAX_CONCURRENT_JOBS", "3"
        ),
        "MCP_RESEARCH_MAX_QUEUED_JOBS": os.getenv("MCP_RESEARCH_MAX_QUEUED_JOBS", "9"),
        "MCP_RESEARCH_GLOBAL_CONCURRENCY": os.getenv(
            "MCP_RESEARCH_GLOBAL_CONCURRENCY", "3"
        ),
        "MCP_RESEARCH_GLOBAL_SLOT_DIR": os.getenv(
            "MCP_RESEARCH_GLOBAL_SLOT_DIR",
            str(default_global_slot_root() / "reports"),
        ),
        "MCP_RESEARCH_JOB_TIMEOUT": os.getenv("MCP_RESEARCH_JOB_TIMEOUT", "2700"),
        "MCP_RESEARCH_JOB_RETENTION_HOURS": os.getenv(
            "MCP_RESEARCH_JOB_RETENTION_HOURS", "72"
        ),
    }


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= minimum else default


def _get_job_manager() -> JobManager:
    global _JOB_MANAGER
    if _JOB_MANAGER is None:
        jobs_dir = Path(
            os.getenv("MCP_RESEARCH_JOBS_DIR", str(OUTPUT_DIR / "jobs"))
        ).expanduser()
        inherited_pythonpath = os.getenv("PYTHONPATH", "")
        pythonpath = str(WORKDIR)
        if inherited_pythonpath:
            pythonpath = f"{pythonpath}{os.pathsep}{inherited_pythonpath}"
        _JOB_MANAGER = JobManager(
            jobs_dir,
            max_concurrent_jobs=_env_int(
                "MCP_RESEARCH_MAX_CONCURRENT_JOBS", 3, minimum=1
            ),
            max_queued_jobs=_env_int("MCP_RESEARCH_MAX_QUEUED_JOBS", 9),
            timeout_seconds=_job_timeout_seconds(),
            retention_hours=_env_float("MCP_RESEARCH_JOB_RETENTION_HOURS", 72),
            global_slot_dir=Path(
                os.getenv(
                    "MCP_RESEARCH_GLOBAL_SLOT_DIR",
                    str(default_global_slot_root() / "reports"),
                )
            ),
            global_concurrency=_env_int(
                "MCP_RESEARCH_GLOBAL_CONCURRENCY", 3, minimum=1
            ),
            worker_env={
                "GPT_RESEARCHER_PROFILE_DIR": str(WORKDIR),
                "PYTHONPATH": pythonpath,
            },
        )
    return _JOB_MANAGER


async def _run_research_report(
    query: str,
    report_type: str = "research_report",
    tone: str = "objective",
    report_source: str = "web",
    target_date: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    resolved_target_date, resolved_timezone = _resolve_target_date(
        query, target_date, timezone
    )
    effective_query = _query_with_current_date(
        query,
        target_date=resolved_target_date,
        timezone=resolved_timezone,
    )

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

    attempts: list[dict[str, Any]] = []
    primary_retriever = os.getenv("RETRIEVER", "tavily,codex")
    fallback_retriever = os.getenv("MCP_RESEARCH_FALLBACK_RETRIEVER", "").strip()
    retrieval_attempts = min(
        2,
        max(
            1,
            int(
                os.getenv(
                    "MCP_RESEARCH_RETRIEVAL_ATTEMPTS",
                    "2",
                )
            ),
        ),
    )
    writer_attempts = min(
        2, max(1, int(os.getenv("MCP_RESEARCH_WRITER_ATTEMPTS", "2")))
    )
    judge_attempts = min(2, max(1, int(os.getenv("MCP_RESEARCH_JUDGE_ATTEMPTS", "2"))))
    retrieval_timeout = _env_float(
        "MCP_RESEARCH_RETRIEVAL_TIMEOUT",
        750,
    )
    writer_timeout = _env_float("MCP_RESEARCH_WRITER_TIMEOUT", 450)
    judge_timeout = _env_float("MCP_RESEARCH_JUDGE_TIMEOUT", 120)

    researcher = None
    fallback_used = False
    selected_retriever = primary_retriever

    async def conduct_once(retriever_override: str | None):
        previous_retriever = (
            _set_env_temporarily("RETRIEVER", retriever_override)
            if retriever_override
            else None
        )
        try:
            with redirect_stdout(sys.stderr):
                candidate = GPTResearcher(
                    query=effective_query,
                    report_type=report_type,
                    report_source=report_source,
                    tone=tone_map.get(tone, Tone.Objective),
                    verbose=True,
                )
                async with asyncio.timeout(retrieval_timeout):
                    await candidate.conduct_research()
                return candidate
        finally:
            if retriever_override:
                _restore_env("RETRIEVER", previous_retriever)

    retriever_candidates: list[tuple[str, str | None]] = [(primary_retriever, None)]
    if fallback_retriever:
        retriever_candidates.append((fallback_retriever, fallback_retriever))
    for retriever_name, override in retriever_candidates:
        if override is not None:
            fallback_used = True
            selected_retriever = retriever_name
        for attempt_number in range(1, retrieval_attempts + 1):
            _write_worker_phase(
                "retrieval",
                retrieval_attempt=attempt_number,
                retriever=retriever_name,
            )
            try:
                researcher = await conduct_once(override)
            except Exception as exc:
                attempts.append(
                    {
                        "stage": "retrieval",
                        "attempt": attempt_number,
                        "retriever": retriever_name,
                        "status": "error",
                        "reason": f"{type(exc).__name__}: {_redact_error(exc)}",
                    }
                )
                continue
            attempts.append(
                {
                    "stage": "retrieval",
                    "attempt": attempt_number,
                    "retriever": retriever_name,
                    "status": "ok",
                    **_report_metrics(researcher),
                }
            )
            selected_retriever = retriever_name
            break
        if researcher is not None:
            break

    active_profile = _profile(selected_retriever)
    if researcher is None:
        _, failure_path, failure_payload = _save_failure_audit(
            query=query,
            report_type=report_type,
            report_source=report_source,
            tone=tone,
            profile=active_profile,
            fallback_used=fallback_used,
            attempts=attempts,
            researcher=None,
        )
        failure_payload.update(
            {
                "reason": "retrieval stage failed",
                "path": str(failure_path),
                "target_date": resolved_target_date,
                "timezone": resolved_timezone,
            }
        )
        atomic_write_json(failure_path, failure_payload)
        raise RuntimeError(json.dumps(failure_payload, ensure_ascii=False))

    planned_items = getattr(researcher, "research_work_items", []) or []
    serialized_plan = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in planned_items
        if hasattr(item, "to_dict") or isinstance(item, dict)
    ]
    report_generator = getattr(researcher, "report_generator", None)
    research_params = getattr(report_generator, "research_params", None)
    if isinstance(research_params, dict) and serialized_plan:
        evidence_catalog = _writer_evidence_catalog(researcher)
        evidence_catalog_instructions = (
            "The following domain-neutral evidence catalog contains the retrieved claims and "
            "their source URLs. Preserve supported values, dates, units, and caveats; resolve "
            "conflicts explicitly and do not cite a URL absent from this catalog:\n"
            f"{evidence_catalog}"
        )
        conflict_instructions = ""
        planned_conflicts = getattr(researcher, "evidence_conflicts", []) or []
        if planned_conflicts:
            conflict_instructions = (
                "\nThe evidence audit found the following same-date/unit numeric conflicts. "
                "Resolve each with the strongest source or explicitly explain the discrepancy "
                "and chosen figure in the report:\n"
                f"{json.dumps(planned_conflicts, ensure_ascii=False, indent=2)}\n"
            )
        research_params["query"] = (
            f"{effective_query}\n\n"
            "The research planner validated the following work items. The final report "
            "must explicitly satisfy every coverage tag and evidence requirement; do not omit "
            "items merely because the user query summarized them broadly:\n"
            f"{json.dumps(serialized_plan, ensure_ascii=False, indent=2)}"
            f"{conflict_instructions}\n\n"
            f"{evidence_catalog_instructions}"
        )

    evidence_reason = _invalid_evidence_reason(researcher)
    if evidence_reason:
        _, failure_path, failure_payload = _save_failure_audit(
            query=query,
            report_type=report_type,
            report_source=report_source,
            tone=tone,
            profile=active_profile,
            fallback_used=fallback_used,
            attempts=attempts,
            researcher=researcher,
        )
        failure_payload.update(
            {
                "reason": evidence_reason,
                "path": str(failure_path),
                "target_date": resolved_target_date,
                "timezone": resolved_timezone,
            }
        )
        atomic_write_json(failure_path, failure_payload)
        raise RuntimeError(json.dumps(failure_payload, ensure_ascii=False))

    report = ""
    accepted_report = False
    final_quality: dict[str, Any] | None = None
    judge_attempt_count = 0
    for writer_attempt in range(1, writer_attempts + 1):
        _write_worker_phase("writer", writer_attempt=writer_attempt, active_codex=0)
        try:
            with redirect_stdout(sys.stderr):
                async with asyncio.timeout(writer_timeout):
                    report = await researcher.write_report()
        except Exception as exc:
            attempts.append(
                {
                    "stage": "writer",
                    "attempt": writer_attempt,
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {_redact_error(exc)}",
                }
            )
            continue
        candidate_path: str | None = None
        job_dir = os.getenv("MCP_RESEARCH_JOB_DIR")
        if job_dir:
            candidate = Path(job_dir) / f"writer-candidate-{writer_attempt}.md"
            atomic_write_text(candidate, report)
            candidate_path = str(candidate)
        attempts.append(
            {
                "stage": "writer",
                "attempt": writer_attempt,
                "status": "ok",
                "candidate_path": candidate_path,
            }
        )

        for _ in range(judge_attempts):
            if judge_attempt_count >= judge_attempts:
                break
            judge_attempt_count += 1
            judge_attempt = judge_attempt_count
            _write_worker_phase("judge", judge_attempt=judge_attempt, active_codex=0)
            try:
                async with asyncio.timeout(judge_timeout):
                    quality = await _judge_report_quality(
                        query,
                        report,
                        researcher,
                        target_date=resolved_target_date,
                        timezone=resolved_timezone,
                    )
            except Exception as exc:
                quality = {
                    "ok": False,
                    "reason": f"quality judge failed: {type(exc).__name__}: {_redact_error(exc)}",
                    "confidence": 0.0,
                    "judge_error": True,
                }
            final_quality = quality
            attempts.append(
                {
                    "stage": "judge",
                    "attempt": judge_attempt,
                    "writer_attempt": writer_attempt,
                    "status": (
                        "ok"
                        if quality["ok"]
                        else "judge_error"
                        if quality.get("judge_error")
                        else "invalid"
                    ),
                    "reason": None if quality["ok"] else quality["reason"],
                    "quality": quality,
                    **_report_metrics(researcher),
                }
            )
            if quality["ok"]:
                accepted_report = True
                break
            if not quality.get("judge_error"):
                _write_worker_phase(
                    "judge",
                    judge_attempt=judge_attempt,
                    last_quality_reason=str(quality.get("reason") or "")[:4000],
                )
                if (
                    isinstance(research_params, dict)
                    and writer_attempt < writer_attempts
                ):
                    research_params["query"] = (
                        f"{research_params.get('query', effective_query)}\n\n"
                        "The previous draft was rejected by the deterministic/LLM quality gate. "
                        "Rewrite the complete report and explicitly correct every item in this "
                        "failure audit; never substitute guessed values or indirect source labels:\n"
                        f"{str(quality.get('reason') or '')[:5000]}"
                    )
                break
        if accepted_report:
            break
        if judge_attempt_count >= judge_attempts:
            break

    if not accepted_report:
        _, failure_path, failure_payload = _save_failure_audit(
            query=query,
            report_type=report_type,
            report_source=report_source,
            tone=tone,
            profile=active_profile,
            fallback_used=fallback_used,
            attempts=attempts,
            researcher=researcher,
        )
        failure_payload.update(
            {
                "reason": (
                    "judge stage failed"
                    if final_quality and final_quality.get("judge_error")
                    else "report failed evidence or quality validation"
                ),
                "path": str(failure_path),
                "target_date": resolved_target_date,
                "timezone": resolved_timezone,
            }
        )
        atomic_write_json(failure_path, failure_payload)
        raise RuntimeError(json.dumps(failure_payload, ensure_ascii=False))

    task_id, final_markdown, path = _save_report(
        query=query,
        report=report,
        report_type=report_type,
        report_source=report_source,
        tone=tone,
        researcher=researcher,
    )
    _write_worker_phase("completed", active_codex=0)
    evidence_metrics = getattr(researcher, "evidence_metrics", {}) or {}
    if not isinstance(evidence_metrics, dict):
        evidence_metrics = {}
    work_items = getattr(researcher, "research_work_items", []) or []
    evidence_items = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in (getattr(researcher, "evidence_items", []) or [])
        if hasattr(item, "to_dict") or isinstance(item, dict)
    ]
    serialized_work_items = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in work_items
        if hasattr(item, "to_dict") or isinstance(item, dict)
    ]
    quality_gate_passed = bool(
        accepted_report
        and final_quality
        and final_quality.get("ok")
        and serialized_work_items
        and int(evidence_metrics.get("unique_http_sources", 0) or 0)
        >= int(evidence_metrics.get("minimum_http_sources", 2) or 2)
        and bool(evidence_metrics.get("quality_gate_passed"))
    )

    return {
        "task_id": task_id,
        "path": str(path),
        "report": final_markdown,
        "target_date": resolved_target_date,
        "timezone": resolved_timezone,
        **_report_metrics(researcher),
        "work_item_count": len(serialized_work_items),
        "codex_initial_calls": int(evidence_metrics.get("codex_initial_calls", 0) or 0),
        "codex_total_calls": int(evidence_metrics.get("codex_calls", 0) or 0),
        "active_codex_peak": int(evidence_metrics.get("active_codex_peak", 0) or 0),
        "quality_gate_passed": quality_gate_passed,
        "evidence_metrics": evidence_metrics,
        "research_work_items": serialized_work_items,
        "evidence_items": evidence_items,
        "evidence_conflicts": list(getattr(researcher, "evidence_conflicts", []) or []),
        "source_urls": sorted(
            {
                str(item.get("source_url"))
                for item in evidence_items
                if item.get("source_url")
            }
        ),
        "codex_pids": list(evidence_metrics.get("codex_pids", []) or []),
        "codex_runs": list(evidence_metrics.get("codex_runs", []) or []),
        "total_cost_usd": round(researcher.get_costs(), 6),
        "profile": active_profile,
        "fallback_used": fallback_used,
        "attempts": attempts,
    }


@mcp.tool()
async def research_report(query: str) -> dict[str, Any]:
    """Investigate one complete question, wait until finished, and return the report."""
    started = await _submit_research_report(query)
    job_id = str(started["job_id"])
    manager = _get_job_manager()
    try:
        while True:
            status = await manager.wait_status(job_id, 60)
            if status.get("status") in {
                "completed",
                "failed",
                "timed_out",
                "cancelled",
                "interrupted",
            }:
                break
    except asyncio.CancelledError:
        await manager.cancel(job_id)
        raise

    envelope = manager.result(job_id, include_report=True)
    if status.get("status") == "completed" and isinstance(envelope.get("result"), dict):
        return envelope["result"]
    failure = envelope.get("failure")
    if not isinstance(failure, dict):
        failure = {
            "status": status.get("status"),
            "reason": envelope.get("error")
            or status.get("error")
            or "research job failed",
            "job_id": job_id,
        }
    raise RuntimeError(json.dumps(failure, ensure_ascii=False))


async def _submit_research_report(
    query: str,
    report_type: str = "research_report",
    tone: str = "objective",
    report_source: str = "web",
    target_date: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """Persist and enqueue an isolated long-running research worker."""
    resolved_target_date, resolved_timezone = _resolve_target_date(
        query, target_date, timezone
    )
    timeout = _job_timeout_seconds()
    manager = _get_job_manager()
    try:
        initial = await manager.submit(
            {
                "query": query,
                "report_type": report_type,
                "tone": tone,
                "report_source": report_source,
                "target_date": resolved_target_date,
                "timezone": resolved_timezone,
                "timeout_seconds": timeout,
            }
        )
    except JobQueueFullError as exc:
        raise RuntimeError(str(exc)) from exc
    job_id = str(initial["job_id"])
    return {
        "job_id": job_id,
        "status": initial["status"],
        "query": query,
        "target_date": resolved_target_date,
        "timezone": resolved_timezone,
    }


def main() -> None:
    """Run the MCP server using stdio or Streamable HTTP."""
    # Recover durable state and terminate any owned orphan worker before the
    # transport starts accepting calls. Queued jobs are scheduled lazily once
    # FastMCP's event loop handles the first job operation.
    _get_job_manager()
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError(
            "MCP_TRANSPORT must be one of: stdio, sse, streamable-http"
        )
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
