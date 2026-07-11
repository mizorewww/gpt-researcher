"""MCP server entry point for the local GPT Researcher profile."""

from __future__ import annotations

import os
import re
import sys
import asyncio
import json
import math
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
DEFAULT_RESEARCH_TIMEOUT_SECONDS = 2700
DEFAULT_TIMEZONE = "Asia/Singapore"

_MARKET_INDEX_ALIASES = {
    "S&P 500": ("s&p 500", "标普500", "标普 500"),
    "Dow": ("dow", "dow jones", "djia", "道琼斯"),
    "Nasdaq": (
        "nasdaq",
        "nasdaq composite",
        "纳斯达克",
        "纳斯达克综合",
        "纳指",
    ),
    "Russell 2000": ("russell 2000", "罗素2000", "罗素 2000"),
    "Nikkei 225": ("nikkei 225", "日经225", "日经 225"),
    "TOPIX": ("topix", "东证股价", "东证指数"),
    "KOSPI": ("kospi", "韩国综合股价"),
    "KOSDAQ": ("kosdaq", "韩国创业板"),
    "Hang Seng": ("hang seng", "hang seng index", "恒生指数"),
    "Hang Seng TECH": ("hang seng tech", "恒生科技"),
}
_COMMODITY_ALIASES = {
    "WTI": ("wti", "西德州", "西德克萨斯"),
    "Brent": ("brent", "布伦特"),
    "Gold": ("gold", "黄金"),
    "Copper": ("copper", "铜"),
}
_STOCK_MARKET_ALIASES = {
    "US": {"us", "u.s.", "u.s", "美国", "美股", "美"},
    "Japan": {"japan", "日本", "日股", "日"},
    "Korea": {"korea", "south korea", "韩国", "韩股", "韩"},
    "Hong Kong": {"hong kong", "hk", "香港", "港股", "港"},
}
_HTTP_URL_PATTERN = r"https?://[^\s<>\[\]()|]+"
_LOW_VALUE_EVIDENCE_MARKERS = (
    "not supported",
    "could not be verified",
    "could not verify",
    "not provide",
    "no direct",
    "not found",
    "unavailable",
    "n/a",
    "\u672a\u83b7\u53d6",
    "\u672a\u6838\u5b9e",
    "\u65e0\u6cd5\u6838\u5b9e",
)
_WRITER_COMMON_ENTITY_TERMS = {
    "sp500": ("s&p 500", "\u6807\u666e500", "\u6807\u666e 500"),
    "dow": ("dow jones", "djia", "\u9053\u743c\u65af"),
    "nasdaq": ("nasdaq composite", "\u7eb3\u65af\u8fbe\u514b\u7efc\u5408", "\u7eb3\u6307"),
    "russell2000": ("russell 2000", "\u7f57\u7d202000", "\u7f57\u7d20 2000"),
    "nikkei225": ("nikkei 225", "\u65e5\u7ecf225", "\u65e5\u7ecf 225"),
    "topix": ("topix", "\u4e1c\u8bc1\u80a1\u4ef7"),
    "kospi": ("kospi", "\u97e9\u56fd\u7efc\u5408\u80a1\u4ef7"),
    "kosdaq": ("kosdaq", "\u97e9\u56fd\u521b\u4e1a\u677f"),
    "hang_seng": ("hang seng index", "\u6052\u751f\u6307\u6570"),
    "hang_seng_tech": ("hang seng tech", "hstech", "\u6052\u751f\u79d1\u6280"),
    "wti": ("wti", "west texas intermediate", "\u897f\u5fb7\u5dde"),
    "brent": ("brent", "\u5e03\u4f26\u7279"),
    "gold": ("gold", "\u9ec4\u91d1"),
    "copper": ("copper", "\u94dc"),
}
_WRITER_SPECIAL_SOURCE_TERMS = {
    "nikkei_movers": ("market-movers/nikkei_225", "nikkei 225 market movers"),
    "tencent_0700": ("download 700 data", "/stock/700/download-data", "tencent 0700"),
}
_INDEX_LEDGER_SPECS = (
    ("S&P 500", "^GSPC", _MARKET_INDEX_ALIASES["S&P 500"]),
    ("Dow", "^DJI", _MARKET_INDEX_ALIASES["Dow"]),
    ("Nasdaq", "^IXIC", _MARKET_INDEX_ALIASES["Nasdaq"]),
    ("Russell 2000", "^RUT", _MARKET_INDEX_ALIASES["Russell 2000"]),
    ("Nikkei 225", "^N225", _MARKET_INDEX_ALIASES["Nikkei 225"]),
    ("TOPIX", "998405.T", _MARKET_INDEX_ALIASES["TOPIX"]),
    ("KOSPI", "^KS11", _MARKET_INDEX_ALIASES["KOSPI"]),
    ("KOSDAQ", "^KQ11", _MARKET_INDEX_ALIASES["KOSDAQ"]),
    ("Hang Seng", "^HSI", _MARKET_INDEX_ALIASES["Hang Seng"]),
    ("Hang Seng TECH", "HSTECH", _MARKET_INDEX_ALIASES["Hang Seng TECH"]),
)
_COMMODITY_LEDGER_SPECS = (
    (
        "WTI",
        "CL=F",
        _COMMODITY_ALIASES["WTI"],
        "USD/barrel",
        "Yahoo Finance continuous front-month WTI futures (CL=F)",
    ),
    (
        "Brent",
        "BZ=F",
        _COMMODITY_ALIASES["Brent"],
        "USD/barrel",
        "Yahoo Finance continuous front-month Brent futures (BZ=F)",
    ),
    (
        "Gold",
        "GC=F",
        _COMMODITY_ALIASES["Gold"],
        "USD/troy ounce",
        "Yahoo Finance continuous front-month COMEX gold futures (GC=F)",
    ),
    (
        "Copper",
        "HG=F",
        _COMMODITY_ALIASES["Copper"],
        "USD/pound",
        "Yahoo Finance continuous front-month COMEX copper futures (HG=F)",
    ),
)
_STOCK_LEDGER_LEADERS = {
    "US": ("AAPL", "MSFT"),
    "Japan": ("7203.T", "9984.T"),
    "Korea": ("005930.KS", "000660.KS"),
    "Hong Kong": ("0700.HK", "9988.HK"),
}

mcp = FastMCP(
    "gpt-researcher-codex-long",
    instructions=(
        "Run GPT Researcher using the active environment profile. "
        "For this checkout the default is Tavily + Codex long search."
    ),
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
        len(context) if isinstance(context, list) else (1 if str(context or "").strip() else 0)
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


def _writer_reservation_keys(text: str) -> set[str]:
    folded = text.casefold()
    keys = {
        key
        for key, terms in {
            **_WRITER_COMMON_ENTITY_TERMS,
            **_WRITER_SPECIAL_SOURCE_TERMS,
        }.items()
        if any(term in folded for term in terms)
    }
    ticker_matches = re.findall(
        r"\bticker\s*:\s*([a-z0-9.]{2,10})\b",
        folded,
    )
    ticker_matches.extend(
        re.findall(
            r"\|\s*([a-z0-9.]{2,10})\s*\|\s*(?:nasdaq|nyse|"
            r"tokyo stock exchange|tse|korea exchange|krx|hkex)",
            folded,
        )
    )
    keys.update(f"stock:{ticker.upper()}" for ticker in ticker_matches)
    return keys


def _structured_writer_score(record: dict[str, Any], target_date: str) -> int:
    text = " ".join(
        str(record.get(key) or "")
        for key in ("claim", "value", "unit", "as_of_date", "summary")
    ).casefold()
    as_of_date = str(record.get("as_of_date") or "").strip()
    target_hit = bool(target_date) and (
        as_of_date == target_date
        or any(needle in text for needle in _target_date_needles(target_date))
    )
    flat_stock_row = (
        "ticker:" in text
        and "close:" in text
        and ("daily change:" in text or "daily move:" in text)
    ) or (
        text.count("|") >= 6
        and any(exchange in text for exchange in ("nasdaq", "nyse", "tse", "krx", "hkex"))
    )
    numeric = record.get("value") is not None or bool(
        re.search(r"(?:\d[\d,.]*\s*%|[+\-]\d[\d,.]*)", text)
    )
    score = (
        (50 if target_hit else 0)
        + (35 if flat_stock_row else 0)
        + (18 if numeric else 0)
        + (8 if "row" in text or "table" in text else 0)
        + min(8, len(record.get("source_urls", ())) * 4)
    )
    if as_of_date and target_date and as_of_date != target_date:
        score -= 12
    if any(marker in text for marker in _LOW_VALUE_EVIDENCE_MARKERS):
        score -= 120
    return score


def _structured_evidence_digest(researcher: Any, max_chars: int = 48_000) -> str:
    """Deduplicate Codex claims into a writer-visible, source-addressable digest."""

    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for raw_item in getattr(researcher, "evidence_items", []) or []:
        if isinstance(raw_item, dict):
            retriever = str(raw_item.get("retriever") or "")
            claim = str(raw_item.get("claim") or "").strip()
            value = raw_item.get("value")
            unit = str(raw_item.get("unit") or "").strip()
            as_of_date = str(raw_item.get("as_of_date") or "").strip()
            summary = str(raw_item.get("summary") or "").strip()
            raw_url = str(raw_item.get("source_url") or raw_item.get("url") or "")
        else:
            retriever = str(getattr(raw_item, "retriever", "") or "")
            claim = str(getattr(raw_item, "claim", "") or "").strip()
            value = getattr(raw_item, "value", None)
            unit = str(getattr(raw_item, "unit", "") or "").strip()
            as_of_date = str(getattr(raw_item, "as_of_date", "") or "").strip()
            summary = str(getattr(raw_item, "summary", "") or "").strip()
            raw_url = str(
                getattr(raw_item, "source_url", "")
                or getattr(raw_item, "url", "")
                or ""
            )
        if retriever.casefold() != "codexsearch" or not claim:
            continue
        source_url = canonical_http_url(raw_url)
        if source_url is None:
            continue
        try:
            value_key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value_key = str(value)
        # Codex may use a generic table header as the claim and put each row in
        # ``summary``. Keep those rows distinct while still merging the same row
        # when it is backed by several URLs.
        summary_key = summary if value is None else ""
        key = (claim, value_key, unit, as_of_date, summary_key)
        record = grouped.setdefault(
            key,
            {
                "claim": claim,
                "value": value,
                "unit": unit or None,
                "as_of_date": as_of_date or None,
                "summary": summary if value is None else "",
                "source_urls": set(),
            },
        )
        record["source_urls"].add(source_url)

    target_date = os.getenv("MCP_RESEARCH_TARGET_DATE", "").strip()
    ranked: list[dict[str, Any]] = []
    for record in grouped.values():
        candidate = {**record, "source_urls": sorted(record["source_urls"])}
        searchable = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        candidate["_score"] = _structured_writer_score(candidate, target_date)
        candidate["_reservation_keys"] = sorted(_writer_reservation_keys(searchable))
        ranked.append(candidate)
    ranked.sort(
        key=lambda item: (item["_score"], len(item["_reservation_keys"])),
        reverse=True,
    )

    ordered: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    reservation_keys = sorted(
        {
            key
            for candidate in ranked
            if candidate["_score"] >= 0
            for key in candidate["_reservation_keys"]
        }
    )
    for key in reservation_keys:
        candidate = next(
            (
                item
                for item in ranked
                if item["_score"] >= 0
                and key in item["_reservation_keys"]
                and id(item) not in selected_ids
            ),
            None,
        )
        if candidate is not None:
            ordered.append(candidate)
            selected_ids.add(id(candidate))
    ordered.extend(
        candidate
        for candidate in ranked
        if candidate["_score"] >= 0 and id(candidate) not in selected_ids
    )

    serialized: list[dict[str, Any]] = []
    current_size = 2
    for candidate in ordered:
        public_candidate = {
            key: value for key, value in candidate.items() if not key.startswith("_")
        }
        encoded = json.dumps(public_candidate, ensure_ascii=False, separators=(",", ":"))
        if current_size + len(encoded) + 1 > max_chars:
            continue
        serialized.append(public_candidate)
        current_size += len(encoded) + 1
    return json.dumps(serialized, ensure_ascii=False, separators=(",", ":"))


_WRITER_EVIDENCE_CATEGORY_TERMS = {
    "us_market": (
        "s&p",
        "nasdaq",
        "dow jones",
        "russell 2000",
        "nyse",
        "nvidia",
        "apple",
        "microsoft",
        "tesla",
    ),
    "japan": (
        "nikkei",
        "topix",
        "tokyo stock exchange",
        "tse",
        "toyota",
        "softbank",
        "sony",
        "advantest",
        "日本",
        "日经",
    ),
    "korea": (
        "kospi",
        "kosdaq",
        "korea exchange",
        "krx",
        "samsung",
        "sk hynix",
        "韩国",
    ),
    "hong_kong": (
        "hang seng",
        "hstech",
        "hkex",
        "hong kong",
        "tencent",
        "alibaba",
        "meituan",
        "xiaomi",
        "香港",
        "恒生",
    ),
    "commodities": (
        "wti",
        "brent",
        "crude oil",
        "gold",
        "copper",
        "nymex",
        "comex",
        "lme",
        "原油",
        "黄金",
        "铜",
    ),
    "macro": (
        "federal reserve",
        "bank of japan",
        "bank of korea",
        "people's bank of china",
        "inflation",
        "interest rate",
        "yield",
        "央行",
        "通胀",
        "利率",
    ),
}


def _target_date_needles(target_date: str) -> tuple[str, ...]:
    needles = [target_date.strip()]
    try:
        parsed = datetime.strptime(target_date.strip(), "%Y-%m-%d")
    except ValueError:
        return tuple(needle.casefold() for needle in needles if needle)
    needles.extend(
        (
            parsed.strftime("%Y/%m/%d"),
            parsed.strftime("%d/%m/%Y"),
            parsed.strftime("%b %d, %Y"),
            parsed.strftime("%b %d %Y"),
            parsed.strftime("%B %d, %Y"),
            parsed.strftime("%b %d"),
        )
    )
    # Financial tables commonly omit the leading zero ("Jul 9").
    needles.extend(needle.replace(" 0", " ") for needle in tuple(needles))
    return tuple(dict.fromkeys(needle.casefold() for needle in needles if needle))


def _dated_evidence_excerpt(text: str, target_date: str, max_chars: int = 900) -> str:
    """Keep the source window nearest the frozen date instead of a generic page prefix."""

    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    folded = compact.casefold()
    positions = [
        position
        for needle in _target_date_needles(target_date)
        if (position := folded.find(needle)) >= 0
    ]
    if not positions:
        return compact[:max_chars].rstrip() + "…"
    position = min(positions)
    before = min(240, position)
    start = position - before
    excerpt = compact[start : start + max_chars]
    return ("…" if start else "") + excerpt.rstrip() + (
        "…" if start + max_chars < len(compact) else ""
    )


def _writer_evidence_catalog(researcher: Any, max_chars: int = 64_000) -> str:
    """Build a bounded, market-balanced catalog visible to the report writer.

    Codex claims remain the preferred structured layer.  Web retriever excerpts
    are included as source material so an individual Codex outage does not hide
    exact historical rows that were successfully retrieved by Tavily.
    """

    target_date = os.getenv("MCP_RESEARCH_TARGET_DATE", "").strip()
    codex_budget = min(32_000, max(2_000, max_chars // 2))
    structured_codex = json.loads(
        _structured_evidence_digest(researcher, max_chars=codex_budget)
    )
    candidates_by_url: dict[str, dict[str, Any]] = {}

    for raw_item in getattr(researcher, "evidence_items", []) or []:
        if isinstance(raw_item, dict):
            retriever = str(raw_item.get("retriever") or "")
            claim = str(raw_item.get("claim") or "").strip()
            summary = str(raw_item.get("summary") or "").strip()
            title = str(raw_item.get("source_title") or "").strip()
            raw_url = str(raw_item.get("source_url") or raw_item.get("url") or "")
        else:
            retriever = str(getattr(raw_item, "retriever", "") or "")
            claim = str(getattr(raw_item, "claim", "") or "").strip()
            summary = str(getattr(raw_item, "summary", "") or "").strip()
            title = str(getattr(raw_item, "source_title", "") or "").strip()
            raw_url = str(
                getattr(raw_item, "source_url", "")
                or getattr(raw_item, "url", "")
                or ""
            )
        if retriever.casefold() == "codexsearch":
            continue
        source_url = canonical_http_url(raw_url)
        if source_url is None:
            continue
        body = summary or claim
        if not body:
            continue
        searchable = " ".join((title, claim, summary)).casefold()
        categories = [
            category
            for category, terms in _WRITER_EVIDENCE_CATEGORY_TERMS.items()
            if any(term in searchable for term in terms)
        ]
        date_hit = any(needle in searchable for needle in _target_date_needles(target_date))
        flat_rows = searchable.count("|") >= 6
        low_value = any(marker in searchable for marker in _LOW_VALUE_EVIDENCE_MARKERS)
        historical_surface = bool(
            re.search(r"(?:historical|download(?:-data)?|price data)", f"{title} {source_url}", re.I)
        )
        score = (
            (40 if date_hit else 0)
            + (12 if re.search(r"(?:\d[\d,.]*\s*%|[+-]\d[\d,.]*)", searchable) else 0)
            + (12 if flat_rows else 0)
            + (10 if historical_surface else 0)
            + (8 if re.search(r"\b(?:close|closed|settle|settlement|historical)\b", searchable) else 0)
            + min(6, len(categories) * 2)
            - (100 if low_value else 0)
        )
        reservation_searchable = " ".join((title, source_url, searchable[:2000]))
        candidate = {
            "source_url": source_url,
            "source_title": title or None,
            "retriever": retriever or "web",
            "categories": categories or ["other"],
            "excerpt": _dated_evidence_excerpt(body, target_date),
            "_score": score,
            "_reservation_keys": sorted(
                _writer_reservation_keys(reservation_searchable)
            ),
        }
        previous = candidates_by_url.get(source_url)
        if previous is None or candidate["_score"] > previous["_score"]:
            candidates_by_url[source_url] = candidate

    ranked = sorted(
        candidates_by_url.values(),
        key=lambda item: (item["_score"], len(item["excerpt"])),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    selected_urls: set[str] = set()
    # The contract requires two direct sources for every index and commodity,
    # so reserve two distinct candidates per common entity before breadth/fill
    # selection. Known truncation-prone stock surfaces need one each.
    for reservation_key in _WRITER_COMMON_ENTITY_TERMS:
        for _ in range(2):
            candidate = next(
                (
                    item
                    for item in ranked
                    if item["_score"] >= 0
                    and reservation_key in item["_reservation_keys"]
                    and item["source_url"] not in selected_urls
                ),
                None,
            )
            if candidate is None:
                break
            selected.append(candidate)
            selected_urls.add(candidate["source_url"])
    for reservation_key in _WRITER_SPECIAL_SOURCE_TERMS:
        candidate = next(
            (
                item
                for item in ranked
                if item["_score"] >= 0
                and reservation_key in item["_reservation_keys"]
                and item["source_url"] not in selected_urls
            ),
            None,
        )
        if candidate is not None:
            selected.append(candidate)
            selected_urls.add(candidate["source_url"])
    # Reserve breadth first so a dense US result set cannot crowd out Korea or
    # Hong Kong; then fill remaining space with the strongest sources overall.
    for category in _WRITER_EVIDENCE_CATEGORY_TERMS:
        category_count = 0
        for candidate in ranked:
            if candidate["source_url"] in selected_urls or category not in candidate["categories"]:
                continue
            selected.append(candidate)
            selected_urls.add(candidate["source_url"])
            category_count += 1
            if category_count >= 4:
                break
    for candidate in ranked:
        if len(selected) >= 48:
            break
        if candidate["source_url"] not in selected_urls:
            selected.append(candidate)
            selected_urls.add(candidate["source_url"])

    payload: dict[str, Any] = {
        "structured_codex_claims": structured_codex,
        "web_source_excerpts": [],
    }
    for candidate in selected:
        public_candidate = {
            key: value for key, value in candidate.items() if not key.startswith("_")
        }
        payload["web_source_excerpts"].append(public_candidate)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > max_chars:
            payload["web_source_excerpts"].pop()
            break
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _evidence_record(raw_item: Any) -> dict[str, Any]:
    def get(name: str, fallback: str | None = None) -> Any:
        if isinstance(raw_item, dict):
            value = raw_item.get(name)
            return value if value is not None and value != "" else raw_item.get(fallback or "")
        value = getattr(raw_item, name, None)
        return (
            value
            if value is not None and value != ""
            else getattr(raw_item, fallback or "", None)
        )

    return {
        "retriever": str(get("retriever") or ""),
        "claim": str(get("claim") or ""),
        "value": get("value"),
        "unit": str(get("unit") or ""),
        "as_of_date": str(get("as_of_date") or ""),
        "summary": str(get("summary") or ""),
        "source_title": str(get("source_title") or ""),
        "source_url": canonical_http_url(str(get("source_url", "url") or "")),
    }


def _term_in_text(term: str, text: str) -> bool:
    folded_term = term.casefold()
    folded_text = text.casefold()
    if re.fullmatch(r"[a-z0-9]+", folded_term):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(folded_term)}(?![a-z0-9])",
            folded_text,
        ) is not None
    return folded_term in folded_text


def _named_required_market_entities(text: str) -> set[str]:
    decoded = unquote(text)
    named = {
        name
        for name, ticker, aliases in _INDEX_LEDGER_SPECS
        if _term_in_text(ticker, decoded)
        or any(_term_in_text(alias, decoded) for alias in aliases)
    }
    named.update(
        f"commodity:{name}"
        for name, aliases in _COMMODITY_ALIASES.items()
        if any(_term_in_text(alias, decoded) for alias in aliases)
    )
    return named


def _record_entity_match(record: dict[str, Any], entity_name: str) -> int:
    primary_text = f"{record['claim']} {record['source_title']}"
    primary_entities = _named_required_market_entities(primary_text)
    if entity_name in primary_entities:
        return 2
    # A Gold/AP item whose summary happens to mention the Dow must never become
    # Dow corroboration. Summary fallback is allowed only for generic metadata.
    if primary_entities:
        return 0
    summary_entities = _named_required_market_entities(record["summary"])
    return 1 if entity_name in summary_entities else 0


def _source_family(url: str) -> str:
    host = urlparse(url).netloc.casefold()
    if "yahoo." in host or host.endswith("yahoo.com"):
        return "yahoo"
    if "investing.com" in host:
        return "investing"
    return host


def _ledger_value(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return format(value, ".15g")
    normalized = str(value).strip()
    return normalized or None


def _index_source_pair_ledger(
    researcher: Any, target_date: str, max_chars: int = 12_000
) -> str:
    """Build exact, deterministic two-source index rows from retrieved evidence."""

    records = [
        record
        for raw_item in (getattr(researcher, "evidence_items", []) or [])
        if (record := _evidence_record(raw_item))["source_url"] is not None
    ]
    entries: list[dict[str, Any]] = []
    for entity_name, ticker, _aliases in _INDEX_LEDGER_SPECS:
        grouped: dict[str, dict[str, Any]] = {}
        for record in records:
            if record["retriever"].casefold() not in {"yahoochart", "indexhtml"}:
                continue
            if record["as_of_date"] != target_date:
                continue
            if _record_entity_match(record, entity_name) == 0:
                continue
            claim = record["claim"].casefold()
            role = (
                "change"
                if "daily percentage change" in claim
                else "close"
                if "target-date close" in claim
                else None
            )
            if role is None or _ledger_value(record["value"]) is None:
                continue
            group = grouped.setdefault(
                str(record["source_url"]),
                {"source_url": record["source_url"], "records": []},
            )
            group[role] = record
            group["records"].append(record)

        complete_groups = [
            group for group in grouped.values() if "close" in group and "change" in group
        ]
        if not complete_groups:
            continue

        def primary_priority(group: dict[str, Any]) -> tuple[int, str]:
            url = str(group["source_url"])
            retriever = str(group["close"]["retriever"]).casefold()
            if ticker == "998405.T":
                priority = 500 if "finance.yahoo.co.jp" in url else 400 if "investing.com" in url else 0
            elif ticker == "HSTECH":
                priority = 500 if "investing.com" in url else 400
            else:
                priority = 500 if retriever == "yahoochart" else 300
            return (-priority, url)

        primary = sorted(complete_groups, key=primary_priority)[0]
        source_1 = str(primary["source_url"])
        source_1_family = _source_family(source_1)
        candidates: dict[str, tuple[int, dict[str, Any]]] = {}
        for record in records:
            source_2 = str(record["source_url"])
            if source_2 == source_1:
                continue
            parsed = urlparse(source_2)
            if parsed.path in {"", "/"}:
                continue
            if _source_family(source_2) == source_1_family:
                continue
            if ticker == "^N225" and "investing.com" in parsed.netloc.casefold():
                continue
            match_strength = _record_entity_match(record, entity_name)
            if match_strength == 0:
                continue
            retriever = record["retriever"].casefold()
            host = parsed.netloc.casefold()
            record_text = " ".join(
                (record["claim"], record["source_title"], record["summary"])
            ).casefold()
            target_hit = record["as_of_date"] == target_date or any(
                needle in record_text for needle in _target_date_needles(target_date)
            )
            score = (
                (500 if retriever in {"yahoochart", "indexhtml"} else 0)
                + match_strength * 100
                + (80 if target_hit else 0)
                + (70 if "marketwatch.com" in host else 0)
                + (65 if "indexes.nikkei" in host else 0)
                + (60 if "fred.stlouisfed.org" in host else 0)
                + (55 if any(term in host for term in ("jpx", "krx", "hangseng")) else 0)
                + (40 if "investing.com" in host else 0)
                + (20 if re.search(r"historical|history|close", record["source_title"], re.I) else 0)
            )
            previous = candidates.get(source_2)
            if previous is None or score > previous[0]:
                candidates[source_2] = (score, record)
        if not candidates:
            continue
        source_2 = sorted(candidates, key=lambda url: (-candidates[url][0], url))[0]
        close = _ledger_value(primary["close"]["value"])
        change = _ledger_value(primary["change"]["value"])
        if close is None or change is None:
            continue
        entry = {
            "index": entity_name,
            "ticker": ticker,
            "close": close,
            "unit": primary["close"]["unit"] or "index points",
            "daily_move": f"{'+' if not change.startswith(('-', '+')) else ''}{change}%",
            "data_date": target_date,
            "source_1": source_1,
            "source_2": source_2,
        }
        trial = {
            "target_date": target_date,
            "entries": [*entries, entry],
        }
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) <= max_chars:
            entries.append(entry)
    return json.dumps(
        {"target_date": target_date, "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _commodity_source_pair_ledger(
    researcher: Any, target_date: str, max_chars: int = 8_000
) -> str:
    """Build stable commodity rows from target-date Yahoo futures closes."""

    records = [
        record
        for raw_item in (getattr(researcher, "evidence_items", []) or [])
        if (record := _evidence_record(raw_item))["source_url"] is not None
    ]
    entries: list[dict[str, Any]] = []
    for name, ticker, _aliases, unit, contract_basis in _COMMODITY_LEDGER_SPECS:
        entity_name = f"commodity:{name}"
        grouped: dict[str, dict[str, Any]] = {}
        for record in records:
            if (
                record["retriever"].casefold() != "yahoochart"
                or record["as_of_date"] != target_date
                or _record_entity_match(record, entity_name) == 0
            ):
                continue
            claim = record["claim"].casefold()
            role = (
                "change"
                if "daily percentage change" in claim
                else "close"
                if "target-date close" in claim
                else None
            )
            if role is None or _ledger_value(record["value"]) is None:
                continue
            group = grouped.setdefault(str(record["source_url"]), {})
            group[role] = record
        complete = [
            group for group in grouped.values() if "close" in group and "change" in group
        ]
        if not complete:
            continue
        primary = sorted(complete, key=lambda group: str(group["close"]["source_url"]))[0]
        source_1 = str(primary["close"]["source_url"])
        candidates: dict[str, int] = {}
        for record in records:
            source_2 = str(record["source_url"])
            parsed = urlparse(source_2)
            if (
                source_2 == source_1
                or parsed.path in {"", "/"}
                or _source_family(source_2) == _source_family(source_1)
            ):
                continue
            match_strength = _record_entity_match(record, entity_name)
            if match_strength == 0:
                continue
            record_text = " ".join(
                (record["claim"], record["source_title"], record["summary"])
            ).casefold()
            target_hit = record["as_of_date"] == target_date or any(
                needle in record_text for needle in _target_date_needles(target_date)
            )
            if not target_hit:
                continue
            host = parsed.netloc.casefold()
            score = (
                match_strength * 100
                + (100 if any(term in host for term in ("cmegroup", "theice", "lme")) else 0)
                + (80 if any(term in host for term in ("wsj.com", "barrons.com")) else 0)
                + (70 if "marketwatch.com" in host else 0)
                + (60 if "investing.com" in host else 0)
                + (50 if "tradingeconomics.com" in host else 0)
            )
            candidates[source_2] = max(score, candidates.get(source_2, 0))
        if not candidates:
            continue
        source_2 = sorted(candidates, key=lambda url: (-candidates[url], url))[0]
        price = _ledger_value(primary["close"]["value"])
        change = _ledger_value(primary["change"]["value"])
        if price is None or change is None:
            continue
        entry = {
            "commodity": name,
            "ticker": ticker,
            "price": price,
            "currency_unit": unit,
            "contract_basis": contract_basis,
            "daily_move": f"{'+' if not change.startswith(('-', '+')) else ''}{change}%",
            "data_date": target_date,
            "source_1": source_1,
            "source_2": source_2,
        }
        trial = {"target_date": target_date, "entries": [*entries, entry]}
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) <= max_chars:
            entries.append(entry)
    return json.dumps(
        {"target_date": target_date, "entries": entries},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _stock_row_ledger(
    researcher: Any, target_date: str, max_chars: int = 16_000
) -> str:
    """Select exactly two leaders and two largest absolute movers per market."""

    index_tickers = {ticker for _name, ticker, _aliases in _INDEX_LEDGER_SPECS}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_item in getattr(researcher, "evidence_items", []) or []:
        record = _evidence_record(raw_item)
        if (
            record["retriever"].casefold() != "yahoochart"
            or record["as_of_date"] != target_date
            or record["source_url"] is None
        ):
            continue
        match = re.match(
            r"^(.+?)\s+target-date\s+(close|daily percentage change)(?:\s|$)",
            record["claim"],
            re.I,
        )
        if match is None:
            continue
        ticker = match.group(1).strip().upper()
        if ticker in index_tickers:
            continue
        role = "close" if match.group(2).casefold() == "close" else "change"
        groups.setdefault((ticker, str(record["source_url"])), {})[role] = record

    candidates_by_market: dict[str, dict[str, dict[str, Any]]] = {
        market: {} for market in _STOCK_LEDGER_LEADERS
    }
    for (ticker, source_url), group in sorted(groups.items()):
        if "close" not in group or "change" not in group:
            continue
        close_record = group["close"]
        change_record = group["change"]
        summary = close_record["summary"] or change_record["summary"]
        market_match = re.search(r"Market:\s*([^|]+)", summary, re.I)
        company_match = re.search(r"Company/Index:\s*([^|]+)", summary, re.I)
        exchange_match = re.search(r"Exchange:\s*([^|]+)", summary, re.I)
        if market_match is None or exchange_match is None:
            continue
        raw_market = market_match.group(1).strip().casefold().replace(".", "")
        market = next(
            (
                canonical
                for canonical, aliases in {
                    "US": {"us", "united states", "america"},
                    "Japan": {"japan"},
                    "Korea": {"korea", "south korea"},
                    "Hong Kong": {"hong kong", "hk"},
                }.items()
                if raw_market in aliases
            ),
            None,
        )
        if market is None:
            continue
        company = company_match.group(1).strip() if company_match else ""
        if not company:
            title_match = re.search(
                r"Yahoo Finance chart:\s*(.+?)\s*\([^()]+\)\s*$",
                close_record["source_title"],
                re.I,
            )
            company = title_match.group(1).strip() if title_match else ""
        exchange = exchange_match.group(1).strip()
        close = _ledger_value(close_record["value"])
        change = _ledger_value(change_record["value"])
        unit = close_record["unit"].strip()
        if not all((company, exchange, close, change, unit)):
            continue
        required_text = " ".join((company, ticker, exchange, close, change, unit)).casefold()
        if "?" in required_text or any(
            marker in required_text
            for marker in (
                *_LOW_VALUE_EVIDENCE_MARKERS,
                "unknown",
                "estimated",
                "estimate",
                "approx",
                "missing",
            )
        ) or any(value.strip() in {"-", "--", "—"} for value in (company, ticker, exchange, close, change, unit)):
            continue
        try:
            numeric_change = float(change.replace(",", "").removesuffix("%"))
        except ValueError:
            continue
        if not math.isfinite(numeric_change):
            continue
        parsed_source = urlparse(source_url)
        if parsed_source.path in {"", "/"}:
            continue
        candidate = {
            "market": market,
            "company": company,
            "ticker": ticker,
            "exchange": exchange,
            "close": f"{close} {unit}",
            "daily_move": (
                f"{'+' if numeric_change > 0 else ''}"
                f"{change.removesuffix('%').lstrip('+')}%"
            ),
            "data_date": target_date,
            "direct_source": source_url,
            "_abs_change": abs(numeric_change),
        }
        previous = candidates_by_market[market].get(ticker)
        if previous is None or source_url < previous["direct_source"]:
            candidates_by_market[market][ticker] = candidate

    entries: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for market, leaders in _STOCK_LEDGER_LEADERS.items():
        pool = candidates_by_market[market]
        missing_leaders = [ticker for ticker in leaders if ticker not in pool]
        movers = sorted(
            (candidate for ticker, candidate in pool.items() if ticker not in leaders),
            key=lambda item: (-item["_abs_change"], item["ticker"]),
        )
        if missing_leaders or len(movers) < 2:
            gaps.append(
                {
                    "market": market,
                    "missing_liquid_leaders": missing_leaders,
                    "event_movers_available": len(movers),
                    "event_movers_required": 2,
                }
            )
            continue
        selected = [
            {**pool[ticker], "selection_type": "liquid leader"}
            for ticker in leaders
        ] + [
            {**candidate, "selection_type": "event mover"}
            for candidate in movers[:2]
        ]
        for candidate in selected:
            entries.append(
                {
                    "market": candidate["market"],
                    "company": candidate["company"],
                    "ticker": candidate["ticker"],
                    "exchange": candidate["exchange"],
                    "close": candidate["close"],
                    "daily_move": candidate["daily_move"],
                    "selection_type": candidate["selection_type"],
                    "data_date": candidate["data_date"],
                    "direct_source": candidate["direct_source"],
                }
            )
    payload = {
        "target_date": target_date,
        "complete": not gaps and len(entries) == 16,
        "required_per_market": 4,
        "gaps": gaps,
        "entries": entries,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > max_chars:
        payload.update(
            {
                "complete": False,
                "gaps": [*gaps, {"reason": "ledger_size_exceeded"}],
                "entries": [],
            }
        )
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return encoded


def _market_writer_final_constraints(researcher: Any, target_date: str) -> str:
    index_ledger = _index_source_pair_ledger(researcher, target_date)
    commodity_ledger = _commodity_source_pair_ledger(researcher, target_date)
    stock_ledger = _stock_row_ledger(researcher, target_date)
    return (
        "\n\n================ DETERMINISTIC INDEX SOURCE-PAIR LEDGER ================\n"
        "For every ledger entry, copy close, daily_move, data_date, source_1 and "
        "source_2 VERBATIM into that index row. Do not substitute, round, shorten, "
        "or replace either deep URL. Entries are emitted only when two distinct "
        "retrieved sources exist:\n"
        f"{index_ledger}\n"
        "================ END INDEX SOURCE-PAIR LEDGER ================"
        "\n\n================ DETERMINISTIC COMMODITY SOURCE-PAIR LEDGER ================\n"
        "For every ledger entry, copy price, currency_unit, contract_basis, "
        "daily_move, data_date, source_1 and source_2 VERBATIM into that "
        "commodity row. Entries use one exact target-date Yahoo futures close "
        "and a distinct retrieved corroborating provider:\n"
        f"{commodity_ledger}\n"
        "================ END COMMODITY SOURCE-PAIR LEDGER ================"
        "\n\n================ DETERMINISTIC STOCK ROW LEDGER ================\n"
        "When complete=true, write EXACTLY one 11-column stock row for every "
        "ledger item and no alternate stocks. Copy market, company, ticker, "
        "exchange, close, daily_move, selection_type (the first seven columns) "
        "VERBATIM; add Catalyst, Recent fundamentals and Risks in their own "
        "columns; copy direct_source VERBATIM as the LAST column. Every row is "
        "for data_date. If complete=false, NEVER invent replacements: preserve "
        "the gaps and fail closed rather than adding unsupported stocks.\n"
        f"{stock_ledger}\n"
        "================ END STOCK ROW LEDGER ================"
        f"{_market_writer_table_contract(target_date)}"
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
        value = "yesterday" if re.search(r"(?:\byesterday\b|昨天|昨日)", lowered_query) else "today"
    if value in {"yesterday", "昨天", "昨日"}:
        resolved = today - timedelta(days=1)
    elif value in {"today", "今天", "今日"}:
        resolved = today
    else:
        try:
            resolved = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("target_date must be YYYY-MM-DD, today, or yesterday") from exc
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
        "When judging market data, verify source grounding and completeness relative to this date."
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


def _is_market_daily_query(query: str) -> bool:
    lowered = query.casefold()
    return any(marker in lowered for marker in ("股票市场", "市场大盘", "stock market"))


def _market_writer_table_contract(target_date: str) -> str:
    return f"""

================ NON-NEGOTIABLE FINAL TABLE CONTRACT ================
This is the FINAL and HIGHEST-PRIORITY formatting requirement. The report is
invalid unless every rule below is satisfied using exact {target_date} data:
1. INDEX TABLE: one horizontal row for each of all 10 required indices. Use
   Index | Close | Daily move | Data date | Driver | Sources. The final Sources
   cell MUST contain at least TWO distinct direct HTTP(S) URLs (or split those
   URLs into Source 1 and Source 2 columns). Never put a range in Close.
2. COMMODITY TABLE: one horizontal row for WTI, Brent, Gold and Copper. Use
   Commodity | Price | Currency/Unit | Contract or spot basis | Daily move |
   Data date | Driver | Sources. The final Sources cell MUST contain at least
   TWO distinct direct HTTP(S) URLs (or use separate Source 1/Source 2 columns).
   Copy every available deterministic commodity-ledger field verbatim.
3. STOCK TABLE: when the deterministic stock ledger says complete=true, use
   EXACTLY its 16 stocks (four per market) and no alternates. EVERY ledger stock
   MUST be exactly one horizontal row with:
   Market | Company | Ticker | Exchange | Close | Daily move | Selection type |
   Catalyst | Recent fundamentals | Risks | Direct source. Direct source MUST be
   the LAST column and contain the ledger's exact direct HTTP(S) URL. When the
   ledger says complete=false, do not invent replacements; fail closed.
4. FORBIDDEN: vertical "Item | Detail" / "项目 | 详情" mini-tables, one
   table per stock, N/A, "not supported", "未获取", estimates, missing tickers,
   missing closes/moves, source labels without URLs, or any stock absent from a
   complete stock ledger.
5. URL FIDELITY: copy the exact deep HTTP(S) URL from the evidence catalog.
   Never shorten a retrieved URL to a domain homepage and never invent a root
   URL such as finance.yahoo.com/, tradingeconomics.com/, or marketwatch.com/.
================ END NON-NEGOTIABLE CONTRACT ================"""


def _market_report_coverage(
    query: str, report: str, target_date: str | None = None
) -> dict[str, Any]:
    """Deterministically validate the acceptance-critical market tables."""

    if not _is_market_daily_query(query):
        return {"applicable": False, "passed": True}
    lowered = report.casefold()
    lines = [line.strip() for line in report.splitlines() if line.strip()]
    report_http_sources = {
        canonical
        for raw_url in re.findall(_HTTP_URL_PATTERN, report)
        if (canonical := canonical_http_url(raw_url.rstrip(".,;"))) is not None
    }
    minimum_report_sources = max(
        1, int(_env_float("MCP_RESEARCH_MIN_HTTP_SOURCES", 25))
    )

    def matching_lines(aliases: tuple[str, ...]) -> list[str]:
        return [
            line
            for line in lines
            if any(_term_in_text(alias, line) for alias in aliases)
        ]

    def matching_index_rows(name: str, aliases: tuple[str, ...]) -> list[str]:
        """Return table rows whose first cell names this index, not a sibling.

        The deterministic ledger intentionally uses short canonical labels such
        as ``Dow``, ``Nasdaq`` and ``Hang Seng``.  Validate those labels while
        keeping the Hang Seng cash index distinct from Hang Seng TECH.
        """

        candidates: list[str] = []
        for line in lines:
            if not line.startswith("|"):
                continue
            cells = row_cells(line)
            if not cells:
                continue
            label = cells[0]
            if name == "Hang Seng" and any(
                _term_in_text(marker, label)
                for marker in ("tech", "hstech", "科技")
            ):
                continue
            if any(_term_in_text(alias, label) for alias in aliases):
                candidates.append(line)
        return candidates

    unverified_markers = (
        "n/a",
        "unknown",
        "unverified",
        "estimated",
        "estimate",
        "approx.",
        "approximately",
        "未即时获取",
        "未获取",
        "未核实",
        "待核实",
        "待补充",
        "估值",
        "综合推断",
        "via terminal data",
    )

    def is_unverified(value: str) -> bool:
        normalized = value.casefold().strip()
        return (
            normalized in {"-", "--", "—", "na"}
            or any(marker in normalized for marker in unverified_markers)
        )

    def row_cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    def row_urls(line: str) -> list[str]:
        return re.findall(_HTTP_URL_PATTERN, line)

    def is_direct_http_url(url: str) -> bool:
        parsed = urlparse(url.rstrip(".,;"))
        return parsed.scheme in {"http", "https"} and parsed.netloc and parsed.path not in {"", "/"}

    missing_indices = [
        name
        for name, aliases in _MARKET_INDEX_ALIASES.items()
        if not matching_index_rows(name, aliases)
    ]
    missing_index_double_sources = [
        name
        for name, aliases in _MARKET_INDEX_ALIASES.items()
        if not any(
            len(
                {
                    url.rstrip(".,;")
                    for url in re.findall(_HTTP_URL_PATTERN, line)
                    if is_direct_http_url(url)
                }
            )
            >= 2
            for line in matching_index_rows(name, aliases)
        )
    ]
    invalid_index_rows = []
    for name, aliases in _MARKET_INDEX_ALIASES.items():
        candidates = [
            line
            for line in matching_index_rows(name, aliases)
            if len(row_cells(line)) >= 6
        ]
        valid = False
        for line in candidates:
            cells = row_cells(line)
            if (
                any(alias in cells[0].casefold() for alias in aliases)
                and re.search(r"\d", cells[1])
                and re.search(r"[-+]?\d", cells[2])
                and re.search(r"20\d{2}-\d{2}-\d{2}", cells[3])
                and (not target_date or target_date in cells[3])
                and len(set(row_urls(line))) >= 2
                and all(is_direct_http_url(url) for url in row_urls(line))
                and not any(
                    is_unverified(value)
                    for value in (cells[1], cells[2], cells[3])
                )
            ):
                valid = True
                break
        if not valid:
            invalid_index_rows.append(name)
    missing_commodities = [
        name
        for name, aliases in _COMMODITY_ALIASES.items()
        if not any(alias in lowered for alias in aliases)
    ]
    missing_commodity_double_sources = [
        name
        for name, aliases in _COMMODITY_ALIASES.items()
        if not any(
            len(
                {
                    url.rstrip(".,;")
                    for url in re.findall(_HTTP_URL_PATTERN, line)
                    if is_direct_http_url(url)
                }
            )
            >= 2
            for line in matching_lines(aliases)
        )
    ]
    invalid_commodity_rows = []
    for name, aliases in _COMMODITY_ALIASES.items():
        candidates = [
            line
            for line in matching_lines(aliases)
            if line.startswith("|") and len(row_cells(line)) >= 8
        ]
        valid = False
        for line in candidates:
            cells = row_cells(line)
            if (
                any(alias in cells[0].casefold() for alias in aliases)
                and re.search(r"\d", cells[1])
                and cells[2]
                and cells[3]
                and re.search(r"[-+]?\d", cells[4])
                and re.search(r"20\d{2}-\d{2}-\d{2}", cells[5])
                and (not target_date or target_date in cells[5])
                and len(set(row_urls(line))) >= 2
                and all(is_direct_http_url(url) for url in row_urls(line))
                and not any(
                    is_unverified(value)
                    for value in (
                        cells[1],
                        cells[2],
                        cells[3],
                        cells[4],
                        cells[5],
                    )
                )
            ):
                valid = True
                break
        if not valid:
            invalid_commodity_rows.append(name)

    market_rows: dict[str, list[tuple[str, str]]] = {
        market: [] for market in _STOCK_MARKET_ALIASES
    }
    incomplete_stock_rows: list[str] = []
    seen_stocks: set[tuple[str, str]] = set()
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = row_cells(line)
        if not cells or set("".join(cells)) <= {"-", ":", " "}:
            continue
        market_cell = re.sub(r"[*_`\s]", "", cells[0].casefold())
        market_tokens = {
            token
            for token in re.split(r"[()（）/]+", market_cell)
            if token
        }
        market_tokens.add(market_cell)
        market = next(
            (
                name
                for name, aliases in _STOCK_MARKET_ALIASES.items()
                if market_tokens.intersection(
                    {
                        re.sub(r"[*_`\s]", "", alias.casefold())
                        for alias in aliases
                    }
                )
            ),
            None,
        )
        if market is None:
            continue
        # Allow fundamentals and risks to share one substantive column; both
        # still remain mandatory content and the LLM judge checks the prose.
        if len(cells) < 10:
            incomplete_stock_rows.append(line[:240])
            continue
        source_index = 10 if len(cells) >= 11 else 9
        required = cells[: source_index + 1]
        if any(not value for value in required) or any(
            is_unverified(value)
            for value in (cells[2], cells[4], cells[5], cells[source_index])
        ):
            incomplete_stock_rows.append(line[:240])
            continue
        source_urls = row_urls(cells[source_index])
        if not source_urls or not any(is_direct_http_url(url) for url in source_urls):
            incomplete_stock_rows.append(line[:240])
            continue
        if not re.search(r"\d", cells[4]) or "%" not in cells[5]:
            incomplete_stock_rows.append(line[:240])
            continue
        ticker = re.sub(r"[*_`\s]", "", cells[2]).upper()
        if not ticker or (market, ticker) in seen_stocks:
            continue
        seen_stocks.add((market, ticker))
        market_rows[market].append((ticker, cells[6].casefold()))

    stock_counts = {market: len(rows) for market, rows in market_rows.items()}
    leader_markers = ("liquid", "leader", "权重", "高流动", "龙头")
    event_markers = ("event", "mover", "事件", "异动", "异常")
    selection_counts = {
        market: {
            "leaders": sum(
                any(marker in selection for marker in leader_markers)
                or not any(marker in selection for marker in event_markers)
                for _, selection in rows
            ),
            "event_movers": sum(
                any(marker in selection for marker in event_markers)
                for _, selection in rows
            ),
        }
        for market, rows in market_rows.items()
    }
    deficient_markets = {
        market: count for market, count in stock_counts.items() if count < 4
    }
    deficient_selection_mix = {
        market: counts
        for market, counts in selection_counts.items()
        if counts["leaders"] < 2 or counts["event_movers"] < 2
    }
    passed = not any(
        (
            missing_indices,
            missing_index_double_sources,
            invalid_index_rows,
            missing_commodities,
            missing_commodity_double_sources,
            invalid_commodity_rows,
            deficient_markets,
            deficient_selection_mix,
            incomplete_stock_rows,
            len(report_http_sources) < minimum_report_sources,
        )
    ) and len(seen_stocks) >= 16
    return {
        "applicable": True,
        "passed": passed,
        "expected_target_date": target_date,
        "missing_indices": missing_indices,
        "indices_without_two_direct_sources": missing_index_double_sources,
        "invalid_or_unverified_index_rows": invalid_index_rows,
        "missing_commodities": missing_commodities,
        "commodities_without_two_direct_sources": missing_commodity_double_sources,
        "invalid_or_unverified_commodity_rows": invalid_commodity_rows,
        "distinct_stocks": len(seen_stocks),
        "stock_counts_by_market": stock_counts,
        "selection_mix_by_market": selection_counts,
        "deficient_markets": deficient_markets,
        "deficient_selection_mix": deficient_selection_mix,
        "incomplete_stock_rows": incomplete_stock_rows[:20],
        "report_http_sources_count": len(report_http_sources),
        "minimum_report_http_sources": minimum_report_sources,
    }


def _market_ledger_fidelity(
    researcher: Any, report: str, target_date: str
) -> dict[str, Any]:
    """Verify final table rows against all deterministic ledger fields."""

    rows = []
    for raw_line in report.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells:
            rows.append((line, cells))

    def numeric(value: str) -> float | None:
        match = re.search(r"[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)", value)
        if match is None:
            return None
        try:
            parsed = float(match.group(0).replace(",", ""))
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None

    def close_enough(observed: str, expected: str, tolerance: float = 1e-6) -> bool:
        left = numeric(observed)
        right = numeric(expected)
        return (
            left is not None
            and right is not None
            and math.isclose(left, right, rel_tol=0.0, abs_tol=tolerance)
        )

    def urls(line: str) -> set[str]:
        return {
            canonical
            for raw_url in re.findall(_HTTP_URL_PATTERN, line)
            if (canonical := canonical_http_url(raw_url.rstrip(".,;"))) is not None
        }

    def index_label_matches(name: str, aliases: tuple[str, ...], label: str) -> bool:
        if name == "Hang Seng" and any(
            _term_in_text(marker, label) for marker in ("tech", "hstech", "科技")
        ):
            return False
        return any(_term_in_text(alias, label) for alias in aliases)

    index_payload = json.loads(_index_source_pair_ledger(researcher, target_date))
    commodity_payload = json.loads(
        _commodity_source_pair_ledger(researcher, target_date)
    )
    stock_payload = json.loads(_stock_row_ledger(researcher, target_date))
    index_entries = index_payload.get("entries", [])
    commodity_entries = commodity_payload.get("entries", [])
    stock_entries = stock_payload.get("entries", [])
    index_mismatches: list[dict[str, str]] = []
    commodity_mismatches: list[dict[str, str]] = []
    stock_mismatches: list[dict[str, str]] = []

    aliases_by_index = {
        name: aliases for name, _ticker, aliases in _INDEX_LEDGER_SPECS
    }
    for entry in index_entries:
        name = str(entry["index"])
        candidates = [
            (line, cells)
            for line, cells in rows
            if len(cells) >= 7
            and index_label_matches(name, aliases_by_index[name], cells[0])
        ]
        expected_urls = {
            canonical_http_url(str(entry["source_1"])),
            canonical_http_url(str(entry["source_2"])),
        }
        valid = any(
            close_enough(cells[1], str(entry["close"]))
            and close_enough(cells[2], str(entry["daily_move"]), tolerance=0.01)
            and target_date in cells[3]
            and expected_urls.issubset(urls(line))
            for line, cells in candidates
        )
        if not valid:
            index_mismatches.append({"index": name, "reason": "value/date/source mismatch"})

    aliases_by_commodity = {
        name: aliases
        for name, _ticker, aliases, _unit, _basis in _COMMODITY_LEDGER_SPECS
    }
    for entry in commodity_entries:
        name = str(entry["commodity"])
        candidates = [
            (line, cells)
            for line, cells in rows
            if len(cells) >= 9
            and any(
                _term_in_text(alias, cells[0])
                for alias in aliases_by_commodity[name]
            )
        ]
        expected_urls = {
            canonical_http_url(str(entry["source_1"])),
            canonical_http_url(str(entry["source_2"])),
        }
        ticker = str(entry["ticker"]).casefold()
        valid = any(
            close_enough(cells[1], str(entry["price"]))
            and close_enough(cells[4], str(entry["daily_move"]), tolerance=0.01)
            and ticker in cells[3].casefold()
            and target_date in cells[5]
            and expected_urls.issubset(urls(line))
            for line, cells in candidates
        )
        if not valid:
            commodity_mismatches.append(
                {"commodity": name, "reason": "value/basis/date/source mismatch"}
            )

    for entry in stock_entries:
        ticker = str(entry["ticker"]).upper()
        candidates = [
            (line, cells)
            for line, cells in rows
            if len(cells) >= 11
            and re.sub(r"[*_`\s]", "", cells[2]).upper() == ticker
        ]
        expected_url = canonical_http_url(str(entry["direct_source"]))
        valid = any(
            close_enough(cells[4], str(entry["close"]))
            and close_enough(cells[5], str(entry["daily_move"]), tolerance=0.01)
            and expected_url in urls(line)
            for line, cells in candidates
        )
        if not valid:
            stock_mismatches.append(
                {"ticker": ticker, "reason": "value/move/source mismatch"}
            )

    expected_counts = {"indices": 10, "commodities": 4, "stocks": 16}
    actual_counts = {
        "indices": len(index_entries),
        "commodities": len(commodity_entries),
        "stocks": len(stock_entries),
    }
    complete_ledgers = (
        actual_counts == expected_counts and stock_payload.get("complete") is True
    )
    return {
        "passed": complete_ledgers
        and not index_mismatches
        and not commodity_mismatches
        and not stock_mismatches,
        "target_date": target_date,
        "expected_counts": expected_counts,
        "actual_counts": actual_counts,
        "index_mismatches": index_mismatches,
        "commodity_mismatches": commodity_mismatches,
        "stock_mismatches": stock_mismatches,
        "stock_ledger_complete": stock_payload.get("complete") is True,
    }


def _invalid_evidence_reason(researcher: Any) -> str | None:
    if not _context_text(researcher).strip():
        return "empty research context"
    metrics = _report_metrics(researcher)
    min_sources = int(_env_float("MCP_RESEARCH_MIN_HTTP_SOURCES", 25))
    min_context_chars = int(_env_float("MCP_RESEARCH_MIN_CONTEXT_CHARS", 2000))
    if metrics["http_sources_count"] < min_sources:
        return f"too few HTTP evidence sources: {metrics['http_sources_count']} < {min_sources}"
    if metrics["context_chars"] < min_context_chars:
        return f"too little research context: {metrics['context_chars']} < {min_context_chars}"
    work_items = getattr(researcher, "research_work_items", None)
    if work_items is not None and len(work_items) != 3:
        return f"research planner produced {len(work_items)} work items instead of 3"
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
        if int(evidence_metrics.get("codex_initial_calls", 0) or 0) != 3:
            return "the three initial work items did not each attempt Codex retrieval"
        if int(evidence_metrics.get("active_codex_peak", 0) or 0) != 3:
            return "the three initial Codex calls did not overlap"
    return None


def _invalid_report_reason(
    report: str,
    researcher: Any,
    query: str = "",
    target_date: str | None = None,
) -> str | None:
    evidence_reason = _invalid_evidence_reason(researcher)
    if evidence_reason:
        return evidence_reason
    report_text = (report or "").strip()
    report_lower = report_text.lower()
    if any(marker in report_lower for marker in EMPTY_REPORT_MARKERS):
        return "empty-source abstention report"
    coverage = _market_report_coverage(query, report_text, target_date=target_date)
    reasons: list[str] = []
    if not coverage["passed"]:
        compact_coverage = coverage.copy()
        compact_coverage["incomplete_stock_rows"] = [
            str(row)[:160]
            for row in (coverage.get("incomplete_stock_rows") or [])[:10]
        ]
        reasons.append(
            "market report coverage gate failed: "
            + json.dumps(
                compact_coverage,
                ensure_ascii=False,
                separators=(",", ":"),
            )[:3500]
        )
    if coverage.get("applicable"):
        if target_date:
            ledger_fidelity = _market_ledger_fidelity(
                researcher, report_text, target_date
            )
            if not ledger_fidelity["passed"]:
                reasons.append(
                    "deterministic market-ledger fidelity gate failed: "
                    + json.dumps(
                        ledger_fidelity,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )[:3500]
                )
        report_urls = {
            canonical
            for raw_url in re.findall(_HTTP_URL_PATTERN, report_text)
            if (canonical := canonical_http_url(raw_url.rstrip(".,;"))) is not None
        }
        evidence_urls = {
            canonical
            for item in (getattr(researcher, "evidence_items", None) or [])
            if (
                canonical := canonical_http_url(
                    str(getattr(item, "source_url", "") or "")
                )
            )
            is not None
        }
        if evidence_urls:
            unsupported = sorted(report_urls - evidence_urls)
            if unsupported:
                reasons.append(
                    f"report cites {len(unsupported)} URL(s) absent from retrieved evidence; "
                    "rewrite using only audited evidence URLs: "
                    + json.dumps(unsupported[:10], ensure_ascii=False)
                )
    return "\n".join(reasons) if reasons else None


def _url_equivalence_key(url: str) -> tuple[str, str, str, str] | None:
    """Return a conservative key for harmless URL spelling differences."""

    canonical = canonical_http_url(url)
    if canonical is None:
        return None
    parsed = urlparse(canonical)
    path = parsed.path
    if parsed.netloc in {"query1.finance.yahoo.com", "query2.finance.yahoo.com"}:
        if path.casefold().startswith("/v8/finance/chart/"):
            # Yahoo ticker symbols are case-insensitive, while an LLM may alter
            # 285A.T to 285a.t despite being told to copy the ledger verbatim.
            path = path.casefold()
    return parsed.scheme, parsed.netloc, path, parsed.query


def _sanitize_report_urls(
    report: str, researcher: Any
) -> tuple[str, dict[str, list[dict[str, str]]]]:
    """Enforce the retrieved-evidence URL allow-list before judging a draft.

    Exact or conservatively equivalent URLs are rewritten to the canonical URL
    stored in ``EvidenceItem``. Unsupported Markdown links keep their visible
    label but lose the invented target; unsupported bare URLs are removed. The
    quality gate runs afterwards, so removing a required row source still makes
    the report fail closed.
    """

    evidence_urls = sorted(
        {
            canonical
            for item in (getattr(researcher, "evidence_items", None) or [])
            if (
                canonical := canonical_http_url(
                    str(getattr(item, "source_url", "") or "")
                )
            )
            is not None
        }
    )
    if not evidence_urls:
        return report, {"normalized": [], "removed": []}

    exact = set(evidence_urls)
    equivalent: dict[tuple[str, str, str, str], str] = {}
    for url in evidence_urls:
        key = _url_equivalence_key(url)
        if key is not None:
            equivalent.setdefault(key, url)

    normalized: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []

    def resolve(raw_url: str) -> str | None:
        canonical = canonical_http_url(raw_url)
        if canonical is None:
            return None
        if canonical in exact:
            return canonical
        key = _url_equivalence_key(canonical)
        return equivalent.get(key) if key is not None else None

    markdown_pattern = re.compile(rf"\[([^\]]*)\]\(({_HTTP_URL_PATTERN})\)")

    protected_links: list[str] = []

    def replace_markdown(match: re.Match[str]) -> str:
        label, raw_url = match.group(1), match.group(2)
        resolved = resolve(raw_url)
        if resolved is None:
            removed.append({"url": raw_url, "location": "markdown"})
            return label
        if resolved != raw_url:
            normalized.append({"from": raw_url, "to": resolved})
        display_label = label.strip() or urlparse(resolved).netloc
        token = f"\x00REPORTLINK{len(protected_links)}\x00"
        protected_links.append(f"[{display_label}]({resolved})")
        return token

    sanitized = markdown_pattern.sub(replace_markdown, report)

    def replace_bare(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        resolved = resolve(raw_url)
        if resolved is None:
            removed.append({"url": raw_url, "location": "bare"})
            return ""
        if resolved != raw_url:
            normalized.append({"from": raw_url, "to": resolved})
        return resolved

    sanitized = re.sub(_HTTP_URL_PATTERN, replace_bare, sanitized)
    for position, link in enumerate(protected_links):
        sanitized = sanitized.replace(f"\x00REPORTLINK{position}\x00", link)
    return sanitized, {
        "normalized": normalized[:100],
        "removed": removed[:100],
    }


def _repair_writer_output(report: str) -> tuple[str, list[dict[str, Any]]]:
    """Remove a duplicated full-report restart without inventing content."""

    text = str(report or "")
    first_heading = re.search(r"(?m)^#\s+[^\n]+", text)
    if first_heading is None:
        return text, []
    heading = first_heading.group(0)
    restart = text.find(heading, first_heading.end())
    if restart < 0:
        return text, []
    restarted = text[restart:]
    if len(restarted) < len(text) * 0.45 or restarted.count("\n## ") < 2:
        return text, []
    return restarted, [
        {
            "repair": "duplicate_report_restart",
            "removed_prefix_chars": restart,
            "heading": heading[:200],
        }
    ]


def _enforce_market_ledger_rows(
    report: str, researcher: Any, target_date: str
) -> tuple[str, list[dict[str, str]]]:
    """Replace only acceptance fields in existing rows with ledger truth."""

    index_entries = json.loads(
        _index_source_pair_ledger(researcher, target_date)
    ).get("entries", [])
    commodity_entries = json.loads(
        _commodity_source_pair_ledger(researcher, target_date)
    ).get("entries", [])
    stock_entries = json.loads(_stock_row_ledger(researcher, target_date)).get(
        "entries", []
    )
    index_by_name = {str(entry["index"]): entry for entry in index_entries}
    commodity_by_name = {
        str(entry["commodity"]): entry for entry in commodity_entries
    }
    stock_by_ticker = {str(entry["ticker"]).upper(): entry for entry in stock_entries}
    aliases_by_index = {
        name: aliases for name, _ticker, aliases in _INDEX_LEDGER_SPECS
    }
    aliases_by_commodity = {
        name: aliases
        for name, _ticker, aliases, _unit, _basis in _COMMODITY_LEDGER_SPECS
    }
    changes: list[dict[str, str]] = []
    output: list[str] = []

    def index_matches(name: str, label: str) -> bool:
        if name == "Hang Seng" and any(
            _term_in_text(marker, label) for marker in ("tech", "hstech", "科技")
        ):
            return False
        return any(
            _term_in_text(alias, label) for alias in aliases_by_index[name]
        )

    for raw_line in report.splitlines(keepends=True):
        ending = "\n" if raw_line.endswith("\n") else ""
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        if not stripped.startswith("|"):
            output.append(raw_line)
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        replacement: list[str] | None = None
        entity = ""

        if len(cells) >= 6:
            for name, entry in index_by_name.items():
                if not index_matches(name, cells[0]):
                    continue
                replacement = [
                    cells[0],
                    str(entry["close"]),
                    str(entry["daily_move"]),
                    str(entry["data_date"]),
                    cells[4],
                    f"[Source 1]({entry['source_1']})",
                    f"[Source 2]({entry['source_2']})",
                ]
                entity = f"index:{name}"
                break

        if replacement is None and len(cells) >= 8:
            for name, entry in commodity_by_name.items():
                if not any(
                    _term_in_text(alias, cells[0])
                    for alias in aliases_by_commodity[name]
                ):
                    continue
                replacement = [
                    cells[0],
                    str(entry["price"]),
                    str(entry["currency_unit"]),
                    str(entry["contract_basis"]),
                    str(entry["daily_move"]),
                    str(entry["data_date"]),
                    cells[6],
                    f"[Source 1]({entry['source_1']})",
                    f"[Source 2]({entry['source_2']})",
                ]
                entity = f"commodity:{name}"
                break

        if replacement is None and len(cells) >= 11:
            ticker = re.sub(r"[*_`\s]", "", cells[2]).upper()
            entry = stock_by_ticker.get(ticker)
            if entry is not None:
                replacement = [
                    str(entry["market"]),
                    str(entry["company"]),
                    str(entry["ticker"]),
                    str(entry["exchange"]),
                    str(entry["close"]),
                    str(entry["daily_move"]),
                    str(entry["selection_type"]),
                    cells[7],
                    cells[8],
                    cells[9],
                    f"[Direct source]({entry['direct_source']})",
                ]
                entity = f"stock:{ticker}"

        if replacement is None:
            output.append(raw_line)
            continue
        rebuilt = "| " + " | ".join(replacement) + " |"
        output.append(rebuilt + ending)
        if rebuilt != stripped:
            changes.append({"entity": entity, "repair": "ledger_row_fields"})

    return "".join(output), changes[:100]


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
        return {"ok": False, "reason": hard_reason, "confidence": 1.0, "judge_error": False}

    from gpt_researcher.utils.llm import create_chat_completion

    cfg = researcher.cfg
    context_chars = len(_context_text(researcher))
    sources_count = len(getattr(researcher, "visited_urls", []) or [])
    conflicts = getattr(researcher, "evidence_conflicts", []) or []
    market_requirements = ""
    lowered_query = query.casefold()
    if any(marker in lowered_query for marker in ("股票市场", "市场大盘", "stock market")):
        market_requirements = (
            "\nFor this market daily, ok=true additionally requires all ten requested indices; "
            "WTI, Brent, gold and copper with price/unit/basis/change/date; at least 16 distinct "
            "stocks with at least four each in the U.S., Japan, South Korea and Hong Kong; and "
            "for every stock ticker, exchange, close, move, catalyst, recent fundamentals, risk "
            "and a direct source. Each market must contain two liquid/index leaders and two "
            "event-driven or abnormal movers. Material index and commodity figures must be "
            "corroborated twice. Reject omissions rather than assuming them.\n"
        )
    prompt = (
        "You are a strict quality gate for an autonomous research report. "
        "Return ONLY compact JSON with keys ok (boolean), reason (string), confidence (0-1).\n\n"
        f"{_current_date_context(target_date=target_date, timezone=timezone)} "
        "Do not reject a report merely because your model priors think this date is in the future; "
        "judge recency and future-date risk relative to the Current date above.\n\n"
        "Mark ok=false if the report does not directly answer the query, lacks concrete source-grounded facts, "
        "claims it could not gather sources, is mostly generic, omits major requested dimensions, or appears internally inconsistent. "
        "Mark ok=true only when it is a usable sourced research answer.\n\n"
        f"{market_requirements}"
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
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
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
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": os.getenv("CODEX_SEARCH_RETRIEVER_CONCURRENCY"),
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
    evidence_items = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in (getattr(researcher, "evidence_items", []) or [])
        if hasattr(item, "to_dict") or isinstance(item, dict)
    ] if researcher else []
    work_items = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in (getattr(researcher, "research_work_items", []) or [])
        if hasattr(item, "to_dict") or isinstance(item, dict)
    ] if researcher else []
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
        "evidence_conflicts": list(
            getattr(researcher, "evidence_conflicts", []) or []
        ) if researcher else [],
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
    path = OUTPUT_DIR / f"{title}.failed.json"
    index = 2
    while path.exists():
        path = OUTPUT_DIR / f"{title}_{index}.failed.json"
        index += 1
    atomic_write_json(path, payload)
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
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": os.getenv("CODEX_SEARCH_RETRIEVER_CONCURRENCY"),
        "CODEX_SEARCH_GLOBAL_CONCURRENCY": os.getenv("CODEX_SEARCH_GLOBAL_CONCURRENCY", "9"),
        "CODEX_SEARCH_MODEL": os.getenv("CODEX_SEARCH_MODEL"),
        "CODEX_SEARCH_REASONING_EFFORT": os.getenv("CODEX_SEARCH_REASONING_EFFORT"),
        "CODEX_SEARCH_SERVICE_TIER": os.getenv("CODEX_SEARCH_SERVICE_TIER"),
        "CODEX_SEARCH_SUPPORTS_WEBSOCKETS": os.getenv("CODEX_SEARCH_SUPPORTS_WEBSOCKETS"),
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
    judge_attempts = min(
        2, max(1, int(os.getenv("MCP_RESEARCH_JUDGE_ATTEMPTS", "2")))
    )
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
    market_final_constraints = (
        _market_writer_final_constraints(researcher, resolved_target_date)
        if _is_market_daily_query(query)
        else ""
    )
    if isinstance(research_params, dict) and serialized_plan:
        evidence_catalog = _writer_evidence_catalog(researcher)
        evidence_catalog_instructions = (
            "The following source-addressable evidence catalog is authoritative for report "
            "construction. Prefer structured_codex_claims for exact fields. The bounded "
            "web_source_excerpts preserve target-date windows from successfully retrieved "
            "pages and may be used when a Codex lane failed, but verify their date, unit, "
            "contract, and row labels exactly. Reuse supported values and URLs; never replace "
            "a supported value with an estimate, cite a URL absent from retrieved evidence, "
            "or turn a caveat into a numeric claim:\n"
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
            "The research planner validated the following three work items. The final report "
            "must explicitly satisfy every coverage tag and evidence requirement; do not omit "
            "items merely because the user query summarized them broadly:\n"
            f"{json.dumps(serialized_plan, ensure_ascii=False, indent=2)}"
            f"{conflict_instructions}\n\n"
            f"{evidence_catalog_instructions}"
            f"{market_final_constraints}"
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
        report, writer_output_repairs = _repair_writer_output(report)
        ledger_row_repairs: list[dict[str, str]] = []
        if _is_market_daily_query(query):
            report, ledger_row_repairs = _enforce_market_ledger_rows(
                report, researcher, resolved_target_date
            )
        report, url_sanitization = _sanitize_report_urls(report, researcher)
        evidence_metrics = getattr(researcher, "evidence_metrics", None)
        if isinstance(evidence_metrics, dict):
            evidence_metrics.setdefault("writer_output_repairs", []).append(
                {
                    "writer_attempt": writer_attempt,
                    "repairs": writer_output_repairs,
                }
            )
            evidence_metrics.setdefault("ledger_row_repairs", []).append(
                {
                    "writer_attempt": writer_attempt,
                    "repairs": ledger_row_repairs,
                }
            )
            evidence_metrics.setdefault("report_url_sanitization", []).append(
                {
                    "writer_attempt": writer_attempt,
                    **url_sanitization,
                }
            )
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
                        f"{market_final_constraints}"
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
        and len(serialized_work_items) == 3
        and int(evidence_metrics.get("unique_http_sources", 0) or 0)
        >= int(evidence_metrics.get("minimum_http_sources", 25) or 25)
        and int(evidence_metrics.get("codex_initial_calls", 0) or 0) == 3
        and int(evidence_metrics.get("active_codex_peak", 0) or 0) == 3
        and bool(evidence_metrics.get("quality_gate_passed"))
    )
    coverage_audit = _market_report_coverage(
        query, report, target_date=resolved_target_date
    )
    coverage_audit["ledger_fidelity"] = _market_ledger_fidelity(
        researcher, report, resolved_target_date
    )
    quality_gate_passed = (
        quality_gate_passed
        and bool(coverage_audit.get("passed"))
        and bool(coverage_audit["ledger_fidelity"].get("passed"))
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
        "coverage_audit": coverage_audit,
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
async def research_report(
    query: str,
    report_type: str = "research_report",
    tone: str = "objective",
    report_source: str = "web",
    target_date: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """Run a short report through an isolated worker and wait for its result."""
    started = await research_report_start(
        query,
        report_type,
        tone,
        report_source,
        target_date,
        timezone,
    )
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
            "reason": envelope.get("error") or status.get("error") or "research job failed",
            "job_id": job_id,
        }
    raise RuntimeError(json.dumps(failure, ensure_ascii=False))


@mcp.tool()
async def research_report_start(
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
        "message": "Research job queued. Poll research_report_status with this job_id.",
    }


@mcp.tool()
async def research_report_status(
    job_id: str, wait_seconds: float = 0
) -> dict[str, Any]:
    """Return compact durable status, optionally waiting for one state change."""
    return await _get_job_manager().wait_status(job_id, wait_seconds)


@mcp.tool()
async def research_reports_status(
    job_ids: list[str], wait_seconds: float = 20
) -> dict[str, Any]:
    """Long-poll compact status for multiple research jobs in one call."""
    statuses = await _get_job_manager().wait_many(job_ids, wait_seconds)
    return {"jobs": statuses}


@mcp.tool()
def research_report_result(
    job_id: str, include_report: bool = False
) -> dict[str, Any]:
    """Return a terminal result; omit the potentially large report by default."""
    return _get_job_manager().result(job_id, include_report=include_report)


@mcp.tool()
async def research_report_cancel(job_id: str) -> dict[str, Any]:
    """Cancel a queued job or terminate a running worker process group."""
    return await _get_job_manager().cancel(job_id)


def main() -> None:
    """Run the MCP server over stdio."""
    # Recover durable state and terminate any owned orphan worker before the
    # transport starts accepting calls. Queued jobs are scheduled lazily once
    # FastMCP's event loop handles the first job operation.
    _get_job_manager()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
