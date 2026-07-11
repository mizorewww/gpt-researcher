"""Research conductor skill for GPT Researcher.

This module provides the ResearchConductor class that manages and
coordinates the research process including query planning, web searching,
and context gathering.
"""

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..actions.agent_creator import choose_agent
from ..actions.query_processing import plan_research_outline
from ..actions.utils import stream_output
from ..document import DocumentLoader, LangChainDocumentLoader, OnlineDocumentLoader
from ..evidence import EvidenceItem, canonical_http_url, deduplicate_evidence
from ..market_data import (
    fetch_index_html_supplement,
    fetch_yahoo_chart,
    index_html_supplements_for_initial_market_lane,
    index_html_supplements_for_regional_gap,
    target_date_for_regional_gap,
    yahoo_instruments_for_initial_commodities_lane,
    yahoo_instruments_for_initial_equities_lane,
    yahoo_instruments_for_initial_market_lane,
    yahoo_instruments_for_regional_gap,
)
from ..utils.enum import ReportSource
from ..utils.logging_config import get_json_handler


@dataclass(frozen=True, slots=True)
class ResearchWorkItem:
    """A bounded research unit with explicit coverage and evidence goals."""

    query: str
    coverage_tags: tuple[str, ...]
    evidence_requirements: tuple[str, ...]
    priority: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "coverage_tags": list(self.coverage_tags),
            "evidence_requirements": list(self.evidence_requirements),
            "priority": self.priority,
        }


class ResearchConductor:
    """Manages and coordinates the research process.

    This class handles the main research workflow including planning
    research queries, conducting web searches, managing MCP retrievers,
    and gathering context from various sources.

    Attributes:
        researcher: The parent GPTResearcher instance.
        logger: Logger for research events.
        json_handler: Handler for JSON logging.
    """

    def __init__(self, researcher):
        """Initialize the ResearchConductor.

        Args:
            researcher: The GPTResearcher instance that owns this conductor.
        """
        self.researcher = researcher
        self.logger = logging.getLogger('research')
        self.json_handler = get_json_handler()
        # Add cache for MCP results to avoid redundant calls
        self._mcp_results_cache = None
        # Track MCP query count for balanced mode
        self._mcp_query_count = 0
        self._retriever_semaphores = {}
        self.research_work_items: list[ResearchWorkItem] = []
        self.gap_followup_queries: list[str] = []
        self.evidence_conflicts: list[dict[str, Any]] = []
        self._evidence_by_checksum: dict[str, EvidenceItem] = {}
        self._evidence_by_query: dict[str, list[EvidenceItem]] = {}
        self._query_http_source_counts: dict[str, int] = {}
        self._gap_followup_rounds = 0
        self._research_progress_completed = 0
        self._active_codex = 0
        self._active_codex_peak = 0
        self._codex_calls = 0
        self._codex_initial_calls = 0
        self._codex_run_metadata: list[dict[str, Any]] = []

    @property
    def evidence_items(self) -> list[EvidenceItem]:
        """Deduplicated evidence accumulated during this report."""

        return list(self._evidence_by_checksum.values())

    @property
    def evidence_metrics(self) -> dict[str, Any]:
        source_urls = {item.source_url for item in self.evidence_items}
        minimum_total = self._minimum_total_http_sources()
        per_work_item_sources: dict[str, int] = {}
        minimum_per_item = max(
            1, int(os.getenv("RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM", "2"))
        )
        for work_item in self.research_work_items:
            urls: set[str] = set()
            prefix = f"{work_item.query}\n"
            for evidence_query, evidence in self._evidence_by_query.items():
                regional_stock_followup = (
                    self._is_market_stock_work_item(work_item)
                    and "market-daily regional evidence gap"
                    in evidence_query.casefold()
                )
                if (
                    evidence_query == work_item.query
                    or evidence_query.startswith(prefix)
                    or regional_stock_followup
                ):
                    urls.update(item.source_url for item in evidence)
            per_work_item_sources[str(work_item.priority)] = len(urls)
        execution_peak = self._codex_execution_peak()
        return {
            "work_item_count": len(self.research_work_items),
            "unique_http_sources": len(source_urls),
            "minimum_http_sources": minimum_total,
            "meets_minimum_http_sources": len(source_urls) >= minimum_total,
            "per_query_http_sources": self._query_http_source_counts.copy(),
            "per_work_item_http_sources": per_work_item_sources,
            "minimum_http_sources_per_work_item": minimum_per_item,
            "evidence_items": len(self.evidence_items),
            "conflicts": len(self.evidence_conflicts),
            "codex_calls": self._codex_calls,
            "codex_initial_calls": self._codex_initial_calls,
            "active_codex_peak": execution_peak or self._active_codex_peak,
            "scheduled_codex_peak": self._active_codex_peak,
            "codex_execution_peak": execution_peak,
            "codex_run_count": len(self._codex_run_metadata),
            "codex_pids": sorted(
                {
                    run.get("codex_pid") or run.get("pid")
                    for run in self._codex_run_metadata
                    if isinstance(run.get("codex_pid") or run.get("pid"), int)
                }
            ),
            "codex_runs": [run.copy() for run in self._codex_run_metadata],
            "quality_gate_passed": (
                len(self.research_work_items) == 3
                and len(source_urls) >= minimum_total
                and all(
                    count >= minimum_per_item
                    for count in per_work_item_sources.values()
                )
            ),
        }

    def _codex_execution_peak(self) -> int:
        events: list[tuple[datetime, int]] = []
        for run in self._codex_run_metadata:
            started = run.get("codex_started_at") or run.get("slot_acquired_at")
            finished = run.get("codex_finished_at") or run.get("slot_released_at")
            if not isinstance(started, str) or not isinstance(finished, str):
                continue
            try:
                events.append((datetime.fromisoformat(started.replace("Z", "+00:00")), 1))
                events.append((datetime.fromisoformat(finished.replace("Z", "+00:00")), -1))
            except ValueError:
                continue
        active = 0
        peak = 0
        for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
            active += delta
            peak = max(peak, active)
        return peak

    async def plan_research(self, query, query_domains=None):
        """Gets the sub-queries from the query
        Args:
            query: original query
        Returns:
            List of queries
        """
        await stream_output(
            "logs",
            "planning_research",
            f"🌐 Browsing the web to learn more about the task: {query}...",
            self.researcher.websocket,
        )

        # Planning uses lightweight retrievers only. Running Codex here would add
        # a hidden serial call before the three deliberate research work items.
        search_results = await self._get_search_results_from_all_retrievers(
            query,
            query_domains,
            exclude_retriever_names={"codexsearch"},
            record_evidence=False,
        )
        self.logger.info(f"Initial search results obtained: {len(search_results)} results")

        await stream_output(
            "logs",
            "planning_research",
            "🤔 Planning the research strategy and subtasks...",
            self.researcher.websocket,
        )

        retriever_names = [
            r.__name__
            for r in self.researcher.retrievers
            if r.__name__.lower() != "codexsearch"
        ]
        # Remove duplicate logging - this will be logged once in conduct_research instead

        outline = await plan_research_outline(
            query=query,
            search_results=search_results,
            agent_role_prompt=self.researcher.role,
            cfg=self.researcher.cfg,
            parent_query=self.researcher.parent_query,
            report_type=self.researcher.report_type,
            cost_callback=self.researcher.add_costs,
            retriever_names=retriever_names,  # Pass retriever names for MCP optimization
            **self.researcher.kwargs
        )
        self.research_work_items = self._normalize_work_items(outline, query)
        self.logger.info(
            "Research outline normalized to three work items: %s",
            [item.to_dict() for item in self.research_work_items],
        )
        # Keep the longstanding list[str] return contract for external callers.
        return [item.query for item in self.research_work_items]

    def _normalize_work_items(self, outline: Any, original_query: str) -> list[ResearchWorkItem]:
        """Convert arbitrary planner output into exactly three distinct work items."""

        market_items = self._market_daily_work_items(original_query)
        if market_items is not None:
            return market_items

        if isinstance(outline, dict):
            candidates = outline.get("work_items") or outline.get("queries") or []
        elif isinstance(outline, (list, tuple)):
            candidates = list(outline)
        elif outline:
            candidates = [outline]
        else:
            candidates = []

        fallback_specs = [
            (
                f"{original_query}\nFocus on baseline facts, chronology, exact metrics, and primary sources.",
                ("baseline", "timeline", "metrics"),
                ("primary sources", "exact dates and values"),
            ),
            (
                f"{original_query}\nFocus on actors, causal drivers, comparisons, and independent corroboration.",
                ("drivers", "stakeholders", "comparison"),
                ("independent corroboration", "direct source URLs"),
            ),
            (
                f"{original_query}\nFocus on risks, counterevidence, uncertainty, and the forward outlook.",
                ("risks", "counterevidence", "outlook"),
                ("contrary evidence", "freshness caveats"),
            ),
        ]

        normalized: list[ResearchWorkItem] = []
        seen_queries: set[str] = set()
        for candidate in candidates:
            if isinstance(candidate, dict):
                query = str(candidate.get("query") or candidate.get("task") or "").strip()
                tags = self._string_tuple(candidate.get("coverage_tags") or candidate.get("tags"))
                requirements = self._string_tuple(
                    candidate.get("evidence_requirements") or candidate.get("evidence")
                )
            else:
                query = str(candidate).strip()
                tags = ()
                requirements = ()
            key = self._query_key(query)
            if not query or key in seen_queries:
                continue
            fallback_index = min(len(normalized), 2)
            _, fallback_tags, fallback_requirements = fallback_specs[fallback_index]
            normalized.append(
                ResearchWorkItem(
                    query=query,
                    coverage_tags=tags or fallback_tags,
                    evidence_requirements=requirements or fallback_requirements,
                    priority=len(normalized) + 1,
                )
            )
            seen_queries.add(key)
            if len(normalized) == 3:
                break

        for query, tags, requirements in fallback_specs:
            if len(normalized) == 3:
                break
            key = self._query_key(query)
            if key in seen_queries:
                continue
            normalized.append(
                ResearchWorkItem(
                    query=query,
                    coverage_tags=tags,
                    evidence_requirements=requirements,
                    priority=len(normalized) + 1,
                )
            )
            seen_queries.add(key)

        # The fallbacks are deliberately distinct, so reaching anything other
        # than three indicates a programming error rather than planner quality.
        if len(normalized) != 3:
            raise RuntimeError("Research planning must yield exactly three work items")
        return normalized

    def _market_daily_work_items(self, query: str) -> list[ResearchWorkItem] | None:
        """Lock the acceptance-critical market daily request into three complete lanes."""

        lowered = query.casefold()
        market_signal = any(
            marker in lowered
            for marker in ("股票市场", "市场大盘", "stock market", "market daily", "市场日报")
        )
        regions_present = all(
            any(marker in lowered for marker in markers)
            for markers in (
                ("美", "美国", "u.s.", " us "),
                ("日", "日本", "japan"),
                ("韩", "韩国", "korea"),
                ("港", "香港", "hong kong"),
            )
        )
        breadth_signal = any(marker in lowered for marker in ("大宗", "commodity", "宏观"))
        if not (market_signal and regions_present and breadth_signal):
            return None

        target_date = os.getenv("MCP_RESEARCH_TARGET_DATE", "the requested trading day")
        timezone = os.getenv("MCP_RESEARCH_TIMEZONE", "Asia/Singapore")
        frozen = f"Target trading date: {target_date}; interpretation timezone: {timezone}."
        common = (
            "Use current live research extending far enough before the target date to explain "
            "drivers. Record exact as-of dates, units, and direct HTTP(S) sources; prefer "
            "exchanges, central banks, official statistics, and company IR. "
        )
        return [
            ResearchWorkItem(
                query=(
                    f"{frozen} {common}Research lane 1 — market indices and macro expectations. "
                    "Cover all ten benchmarks without omission: S&P 500, Dow Jones Industrial "
                    "Average, Nasdaq Composite, Russell 2000, Nikkei 225, TOPIX, KOSPI, KOSDAQ, "
                    "Hang Seng Index, and Hang Seng TECH Index. For each give close, daily move, "
                    "as-of date and the session driver. Explain market expectations for growth, "
                    "inflation, central-bank policy/rates and relevant FX across the U.S., Japan, "
                    "South Korea and Hong Kong/China. Cross-check material index values with two "
                    "independent sources. In the final report use one index table row per benchmark "
                    "with columns Index | Close | Daily move | Data date | Driver | Source 1 | "
                    "Source 2, and put both direct HTTP links in that same row."
                ),
                coverage_tags=(
                    "S&P 500",
                    "Dow",
                    "Nasdaq",
                    "Russell 2000",
                    "Nikkei 225",
                    "TOPIX",
                    "KOSPI",
                    "KOSDAQ",
                    "Hang Seng",
                    "Hang Seng TECH",
                    "macro expectations",
                    "rates",
                    "FX",
                ),
                evidence_requirements=(
                    "close, daily move, and as-of date for every index",
                    "two-source checks for material index figures",
                    "official macro and central-bank sources",
                ),
                priority=1,
            ),
            ResearchWorkItem(
                query=(
                    f"{frozen} {common}Research lane 2 — commodities and cross-asset hot topics. "
                    "Cover WTI crude, Brent crude, gold, and copper without omission. For every "
                    "commodity report price, currency/unit, exact futures contract or spot basis, "
                    "daily change and data date. Explain supply/demand, inventories, geopolitics, "
                    "rates/USD and other current catalysts, link them to equity-market themes, and "
                    "cross-check each material price with two independent sources. In the final "
                    "report use one row per commodity with columns Commodity | Price | Currency/Unit "
                    "| Contract or spot basis | Daily move | Data date | Driver | Source 1 | "
                    "Source 2, with two direct HTTP links in the row."
                ),
                coverage_tags=("WTI", "Brent", "gold", "copper", "cross-asset themes"),
                evidence_requirements=(
                    "price, unit, contract basis, daily move, and date for all four commodities",
                    "two-source checks for each material commodity price",
                    "primary market or official data where available",
                ),
                priority=2,
            ),
            ResearchWorkItem(
                query=(
                    f"{frozen} {common}Research lane 3 — important equities in depth. Dynamically "
                    "select at least 16 distinct stocks: at least four each from the U.S., Japan, "
                    "South Korea and Hong Kong. Within every market include at least two index-"
                    "weight/high-liquidity names and at least two target-day event-driven or "
                    "unusually moving names. For every stock provide company, ticker, exchange, "
                    "closing price, daily percentage move, target-day catalyst, recent fundamental "
                    "background, principal risks and a direct source. Investigate each stock rather "
                    "than grouping names into a superficial list; prioritize exchange filings and IR. "
                    "The final report must use exactly one row per company with columns Market | "
                    "Company | Ticker | Exchange | Close | Daily move | Selection type (liquid leader "
                    "or event mover) | Catalyst | Recent fundamentals | Risks | Direct source."
                ),
                coverage_tags=(
                    "US stocks >=4",
                    "Japan stocks >=4",
                    "Korea stocks >=4",
                    "Hong Kong stocks >=4",
                    "liquid leaders",
                    "event movers",
                ),
                evidence_requirements=(
                    "at least 16 distinct stocks with four per market",
                    "ticker, exchange, close, move, catalyst, fundamentals, risks, direct source",
                    "two liquid leaders and two event movers per market",
                ),
                priority=3,
            ),
        ]

    @staticmethod
    def _string_tuple(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value.strip(),) if value.strip() else ()
        if not isinstance(value, (list, tuple, set)):
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())

    @staticmethod
    def _query_key(query: str) -> str:
        return re.sub(r"\s+", " ", str(query)).strip().casefold()

    @staticmethod
    def _compact_web_retriever_query(query: str) -> str:
        """Keep lightweight search APIs below their practical query limits."""

        normalized = re.sub(r"\s+", " ", str(query)).strip()
        if len(normalized) <= 380:
            return normalized
        date_match = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", normalized)
        target_date = date_match.group(0) if date_match else "requested trading date"
        lowered = normalized.casefold()
        additional = " additional independent primary sources" if "follow-up" in lowered else ""
        if "market-daily regional evidence gap" in lowered:
            if "— japan" in lowered:
                region, indices, candidates = (
                    "Japan",
                    "Nikkei 225 TOPIX",
                    "Toyota 7203 SoftBank 9984 MUFG 8306 Tokyo Electron 8035 Sony 6758",
                )
            elif "— korea" in lowered:
                region, indices, candidates = (
                    "South Korea",
                    "KOSPI KOSDAQ",
                    "Samsung 005930 SK Hynix 000660 Hyundai 005380 Naver 035420 Kakao 035720",
                )
            elif "— hong kong" in lowered:
                region, indices, candidates = (
                    "Hong Kong",
                    "Hang Seng HSI Hang Seng Tech HSTECH",
                    "Tencent 0700 Alibaba 9988 Meituan 3690 BYD 1211 Xiaomi 1810 CNOOC 0883",
                )
            else:
                region, indices, candidates = (
                    "U.S.",
                    "S&P 500 Nasdaq Composite",
                    "NVDA AAPL MSFT MU PEP MARA DELL SNDK",
                )
            return (
                f"{target_date} {region} at least four stocks two liquid leaders two event movers "
                "ticker exchange exact close daily change catalyst fundamentals risk direct source "
                f"{indices} exact close change two direct URLs each index company IR exchange "
                f"candidate pool {candidates} MarketWatch historical"
            )
        if "research lane 1" in lowered:
            return (
                f"{target_date} official market close daily change drivers S&P 500 Dow Nasdaq "
                "Russell 2000 Nikkei 225 TOPIX KOSPI KOSDAQ Hang Seng Hang Seng Tech macro "
                f"rates FX central banks{additional}"
            )
        if "research lane 2" in lowered:
            return (
                f"{target_date} WTI Brent gold copper price unit futures contract daily change "
                f"official market data drivers{additional}"
            )
        if "research lane 3" in lowered:
            return (
                f"{target_date} US Japan South Korea Hong Kong stocks close daily movers "
                "catalysts company IR exchange filings liquid leaders unusual movers "
                f"four stocks each market{additional}"
            )
        return normalized[:380]

    def _lightweight_web_retriever_queries(self, query: str) -> list[str]:
        """Split a regional gap into precise, bounded Tavily-sized searches."""

        compact = self._compact_web_retriever_query(query)
        lowered = str(query).casefold()
        date_match = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", str(query))
        target_date = date_match.group(0) if date_match else "target trading date"
        if "research lane 1" in lowered:
            return [
                f"{target_date} S&P 500 Dow Nasdaq Russell 2000 exact close daily change two sources",
                f"{target_date} Nikkei TOPIX KOSPI KOSDAQ Hang Seng HSI HSTECH exact close change",
                f"{target_date} Fed BOJ BOK PBOC growth inflation rates FX market expectations official",
            ]
        if "research lane 2" in lowered:
            return [
                f"{target_date} WTI Brent exact settlement daily change contract USD barrel EIA ICE NYMEX",
                f"{target_date} gold copper exact settlement daily change contract unit COMEX LME historical",
                f"{target_date} oil gold copper drivers inventories geopolitics rates USD official data",
            ]
        if "research lane 3" in lowered:
            return [
                f"{target_date} US Japan stocks exact close percent movers ticker exchange company IR",
                f"{target_date} South Korea Hong Kong stocks exact close percent movers ticker exchange",
                f"{target_date} global stock biggest gainers losers catalyst fundamentals risk direct source",
            ]
        if "market-daily regional evidence gap" not in lowered:
            return [compact]
        if "— japan" in lowered:
            region = "Japan"
            index_query = (
                f"{target_date} Japan Nikkei 225 TOPIX exact close daily change historical "
                "finance.yahoo.co.jp/quote/998405.T/history "
                "investing.com/indices/topix-historical-data two direct sources"
            )
            price_groups = (
                "Toyota 7203.T SoftBank 9984.T MUFG 8306.T",
                "Tokyo Electron 8035.T Sony 6758.T Kioxia 285A.T",
            )
            mover_surface = "JPX Tokyo Stock Exchange gainers losers"
            catalyst_names = "Toyota SoftBank MUFG Tokyo Electron Sony Kioxia"
        elif "— korea" in lowered:
            region = "South Korea"
            index_query = (
                f"{target_date} South Korea KOSPI KOSDAQ exact close daily change historical "
                "Yahoo ^KS11 ^KQ11 MarketWatch Investing two direct sources"
            )
            price_groups = (
                "Samsung Electronics 005930.KS SK Hynix 000660.KS Hyundai Motor 005380.KS",
                "Naver 035420.KS Kakao 035720.KS Krafton 259960.KS",
            )
            mover_surface = "KRX KOSPI KOSDAQ gainers losers"
            catalyst_names = "Samsung Electronics SK Hynix Hyundai Naver Kakao Krafton"
        elif "— hong kong" in lowered:
            region = "Hong Kong"
            index_query = (
                f"{target_date} Hong Kong Hang Seng HSI Hang Seng Tech HSTECH exact close "
                "daily change historical investing.com/indices/hang-seng-tech-historical-data "
                "cbbc.hangseng.com/en-hk/market/stock/code/hstech two direct sources"
            )
            price_groups = (
                "Tencent 0700.HK Alibaba 9988.HK Meituan 3690.HK",
                "BYD 1211.HK Xiaomi 1810.HK HKEX 0388.HK CNOOC 0883.HK",
            )
            mover_surface = "HKEX Hong Kong gainers losers unusual movers"
            catalyst_names = "Tencent Alibaba Meituan BYD Xiaomi HKEX CNOOC"
        else:
            region = "U.S."
            index_query = (
                f"{target_date} U.S. S&P 500 Nasdaq Composite exact close daily change "
                "historical data AP MarketWatch two direct sources"
            )
            price_groups = (
                "NVDA AAPL MSFT MU",
                "PEP MARA DELL SNDK",
            )
            mover_surface = "NYSE Nasdaq gainers losers unusual movers"
            catalyst_names = "NVIDIA Apple Microsoft Micron PepsiCo MARA Dell SanDisk"
        return [
            index_query,
            (
                f"{target_date} {region} {price_groups[0]} exact historical close daily percent "
                "change ticker exchange direct quote MarketWatch Investing official"
            ),
            (
                f"{target_date} {region} {price_groups[1]} exact historical close daily percent "
                "change ticker exchange direct quote MarketWatch Investing official"
            ),
            (
                f"{target_date} {region} {mover_surface} exact stock close daily percent change "
                "ticker exchange event mover direct quote source"
            ),
            (
                f"{target_date} {region} {catalyst_names} target-date catalyst recent fundamental "
                "background principal risk company IR exchange filing direct source"
            ),
        ]

    def _write_worker_status(self, **extra: Any) -> None:
        """Atomically expose retrieval progress to the isolated job coordinator."""

        job_dir = os.getenv("MCP_RESEARCH_JOB_DIR")
        if not job_dir:
            return
        path = Path(job_dir) / "worker_status.json"
        payload: dict[str, Any] = {
            "phase": "retrieval",
            "progress": {
                "completed": self._research_progress_completed,
                "total": 3,
            },
            "active_codex": self._active_codex,
            "active_codex_peak": self._active_codex_peak,
            "codex_calls": self._codex_calls,
            "codex_initial_calls": self._codex_initial_calls,
        }
        payload.update(extra)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            self.logger.warning("Unable to write worker retrieval status: %s", exc)
            temporary.unlink(missing_ok=True)

    async def conduct_research(self):
        """Runs the GPT Researcher to conduct research"""
        if self.json_handler:
            self.json_handler.update_content("query", self.researcher.query)
        
        self.logger.info(f"Starting research for query: {self.researcher.query}")
        
        # Log active retrievers once at the start of research
        retriever_names = [r.__name__ for r in self.researcher.retrievers]
        self.logger.info(f"Active retrievers: {retriever_names}")
        
        # Reset visited_urls and source_urls at the start of each research task
        self.researcher.visited_urls.clear()
        self.research_work_items = []
        self.gap_followup_queries = []
        self.evidence_conflicts = []
        self._evidence_by_checksum.clear()
        self._evidence_by_query.clear()
        self._query_http_source_counts.clear()
        self._gap_followup_rounds = 0
        self._research_progress_completed = 0
        self._active_codex = 0
        self._active_codex_peak = 0
        self._codex_calls = 0
        self._codex_initial_calls = 0
        self._codex_run_metadata = []
        self._write_worker_status()
        research_data = []

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "starting_research",
                f"🔍 Starting the research task for '{self.researcher.query}'...",
                self.researcher.websocket,
            )
            await stream_output(
                "logs",
                "agent_generated",
                self.researcher.agent,
                self.researcher.websocket
            )

        # Choose agent and role if not already defined
        if not (self.researcher.agent and self.researcher.role):
            self.researcher.agent, self.researcher.role = await choose_agent(
                query=self.researcher.query,
                cfg=self.researcher.cfg,
                parent_query=self.researcher.parent_query,
                cost_callback=self.researcher.add_costs,
                headers=self.researcher.headers,
                prompt_family=self.researcher.prompt_family
            )
                
        # Check if MCP retrievers are configured
        has_mcp_retriever = any("mcpretriever" in r.__name__.lower() for r in self.researcher.retrievers)
        if has_mcp_retriever:
            self.logger.info("MCP retrievers configured and will be used with standard research flow")

        # Conduct research based on the source type
        if self.researcher.source_urls:
            self.logger.info("Using provided source URLs")
            research_data = await self._get_context_by_urls(self.researcher.source_urls)
            if research_data and len(research_data) == 0 and self.researcher.verbose:
                await stream_output(
                    "logs",
                    "answering_from_memory",
                    "🧐 I was unable to find relevant context in the provided sources...",
                    self.researcher.websocket,
                )
            if self.researcher.complement_source_urls:
                self.logger.info("Complementing with web search")
                additional_research = await self._get_context_by_web_search(self.researcher.query, [], self.researcher.query_domains)
                research_data += ' '.join(additional_research)
        elif self.researcher.report_source == ReportSource.Web.value:
            self.logger.info("Using web search with all configured retrievers")
            research_data = await self._get_context_by_web_search(self.researcher.query, [], self.researcher.query_domains)
        elif self.researcher.report_source == ReportSource.Local.value:
            self.logger.info("Using local search")
            document_data = await DocumentLoader(self.researcher.cfg.doc_path).load()
            self.logger.info(f"Loaded {len(document_data)} documents")
            if self.researcher.vector_store:
                self.researcher.vector_store.load(document_data)

            research_data = await self._get_context_by_web_search(self.researcher.query, document_data, self.researcher.query_domains)
        # Hybrid search including both local documents and web sources
        elif self.researcher.report_source == ReportSource.Hybrid.value:
            if self.researcher.document_urls:
                document_data = await OnlineDocumentLoader(self.researcher.document_urls).load()
            else:
                document_data = await DocumentLoader(self.researcher.cfg.doc_path).load()
            if self.researcher.vector_store:
                self.researcher.vector_store.load(document_data)
            # Plan and search the web exactly once. Reuse those same three work
            # items to select local context instead of invoking the entire
            # research pipeline a second time (which previously scheduled an
            # extra Codex/gap wave for hybrid reports).
            web_context = await self._get_context_by_web_search(self.researcher.query, [], self.researcher.query_domains)
            docs_context_parts = await asyncio.gather(
                *[
                    self.researcher.context_manager.get_similar_content_by_query(
                        work_item.query,
                        document_data,
                    )
                    for work_item in self.research_work_items
                ]
            )
            docs_context = " ".join(
                str(part) for part in docs_context_parts if part
            )
            research_data = self.researcher.prompt_family.join_local_web_documents(docs_context, web_context)
        elif self.researcher.report_source == ReportSource.Azure.value:
            from ..document.azure_document_loader import AzureDocumentLoader
            azure_loader = AzureDocumentLoader(
                container_name=os.getenv("AZURE_CONTAINER_NAME"),
                connection_string=os.getenv("AZURE_CONNECTION_STRING")
            )
            azure_files = await azure_loader.load()
            document_data = await DocumentLoader(azure_files).load()  # Reuse existing loader
            research_data = await self._get_context_by_web_search(self.researcher.query, document_data)
            
        elif self.researcher.report_source == ReportSource.LangChainDocuments.value:
            langchain_documents_data = await LangChainDocumentLoader(
                self.researcher.documents
            ).load()
            if self.researcher.vector_store:
                self.researcher.vector_store.load(langchain_documents_data)
            research_data = await self._get_context_by_web_search(
                self.researcher.query, langchain_documents_data, self.researcher.query_domains
            )
        elif self.researcher.report_source == ReportSource.LangChainVectorStore.value:
            research_data = await self._get_context_by_vectorstore(self.researcher.query, self.researcher.vector_store_filter)

        # Rank and curate the sources
        self.researcher.context = research_data
        if self.researcher.cfg.curate_sources:
            self.logger.info("Curating sources")
            self.researcher.context = await self.researcher.source_curator.curate_sources(research_data)

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "research_step_finalized",
                f"Finalized research step.\n💸 Total Research Costs: ${self.researcher.get_costs()}",
                self.researcher.websocket,
            )
            if self.json_handler:
                self.json_handler.update_content("costs", self.researcher.get_costs())
                self.json_handler.update_content("context", self.researcher.context)

        self.logger.info(f"Research completed. Context size: {len(str(self.researcher.context))}")
        # Expose structured audit data without changing the context/report API.
        self.researcher.evidence_items = self.evidence_items
        self.researcher.evidence_metrics = self.evidence_metrics
        self.researcher.research_work_items = self.research_work_items
        self.researcher.evidence_conflicts = self.evidence_conflicts
        self.researcher.codex_run_metadata = [run.copy() for run in self._codex_run_metadata]
        return self.researcher.context

    async def _get_context_by_urls(self, urls):
        """Scrapes and compresses the context from the given urls"""
        self.logger.info(f"Getting context from URLs: {urls}")
        
        new_search_urls = await self._get_new_urls(urls)
        self.logger.info(f"New URLs to process: {new_search_urls}")

        scraped_content = await self.researcher.scraper_manager.browse_urls(new_search_urls)
        self.logger.info(f"Scraped content from {len(scraped_content)} URLs")

        if self.researcher.vector_store:
            self.researcher.vector_store.load(scraped_content)

        context = await self.researcher.context_manager.get_similar_content_by_query(
            self.researcher.query, scraped_content
        )
        return context

    # Add logging to other methods similarly...

    async def _get_context_by_vectorstore(self, query, filter: dict | None = None):
        """
        Generates the context for the research task by searching the vectorstore
        Returns:
            context: List of context
        """
        self.logger.info(f"Starting vectorstore search for query: {query}")
        context = []
        # The planner always returns exactly three queries. Do not append the
        # original query as a hidden fourth unit of work.
        sub_queries = await self.plan_research(query)

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "subqueries",
                f"🗂️  I will conduct my research based on the following queries: {sub_queries}...",
                self.researcher.websocket,
                True,
                sub_queries,
            )

        # Using asyncio.gather to process the sub_queries asynchronously
        context = await asyncio.gather(
            *[
                self._process_sub_query_with_vectorstore(sub_query, filter)
                for sub_query in sub_queries
            ]
        )
        return context

    async def _get_context_by_web_search(self, query, scraped_data: list | None = None, query_domains: list | None = None):
        """
        Generates the context for the research task by searching the query and scraping the results
        Returns:
            context: List of context
        """
        self.logger.info(f"Starting web search for query: {query}")
        
        if scraped_data is None:
            scraped_data = []
        if query_domains is None:
            query_domains = []

        # **CONFIGURABLE MCP OPTIMIZATION: Control MCP strategy**
        mcp_retrievers = [r for r in self.researcher.retrievers if "mcpretriever" in r.__name__.lower()]
        
        # Get MCP strategy configuration
        mcp_strategy = self._get_mcp_strategy()
        
        if mcp_retrievers and self._mcp_results_cache is None:
            if mcp_strategy == "disabled":
                # MCP disabled - skip MCP research entirely
                self.logger.info("MCP disabled by strategy, skipping MCP research")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_disabled",
                        "⚡ MCP research disabled by configuration",
                        self.researcher.websocket,
                    )
            elif mcp_strategy == "fast":
                # Fast: Run MCP once with original query
                self.logger.info("MCP fast strategy: Running once with original query")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_optimization",
                        "🚀 MCP Fast: Running once for main query (performance mode)",
                        self.researcher.websocket,
                    )
                
                # Execute MCP research once with the original query
                mcp_context = await self._execute_mcp_research_for_queries([query], mcp_retrievers)
                self._mcp_results_cache = mcp_context
                self.logger.info(f"MCP results cached: {len(mcp_context)} total context entries")
            elif mcp_strategy == "deep":
                # Deep: Will run MCP for all queries (original behavior) - defer to per-query execution
                self.logger.info("MCP deep strategy: Will run for all queries")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_comprehensive",
                        "🔍 MCP Deep: Will run for each sub-query (thorough mode)",
                        self.researcher.websocket,
                    )
                # Don't cache - let each sub-query run MCP individually
            else:
                # Unknown strategy - default to fast
                self.logger.warning(f"Unknown MCP strategy '{mcp_strategy}', defaulting to fast")
                mcp_context = await self._execute_mcp_research_for_queries([query], mcp_retrievers)
                self._mcp_results_cache = mcp_context
                self.logger.info(f"MCP results cached: {len(mcp_context)} total context entries")

        # Plan exactly three work items. The original query is intentionally not
        # appended; doing so previously created an unbounded fourth Codex call.
        sub_queries = await self.plan_research(query, query_domains)
        self.logger.info(f"Generated sub-queries: {sub_queries}")
        work_items = self.research_work_items or self._normalize_work_items(sub_queries, query)

        if self.researcher.verbose:
            await stream_output(
                "logs",
                "subqueries",
                f"🗂️ I will conduct my research based on the following queries: {sub_queries}...",
                self.researcher.websocket,
                True,
                sub_queries,
            )

        self._research_progress_completed = 0
        self._write_worker_status(
            work_items=[item.to_dict() for item in work_items],
            gap_followups=0,
        )

        async def run_work_item(item: ResearchWorkItem):
            try:
                return await self._process_sub_query(item.query, scraped_data, query_domains)
            finally:
                self._research_progress_completed += 1
                self._write_worker_status(
                    work_items=[work_item.to_dict() for work_item in work_items],
                    gap_followups=0,
                )

        context_results = await asyncio.gather(
            *[run_work_item(item) for item in work_items],
            return_exceptions=True,
        )
        self.logger.info(f"Gathered context from {len(context_results)} sub-queries")

        context = []
        for item, result in zip(work_items, context_results):
            if isinstance(result, Exception):
                self.logger.error(
                    f"Sub-query failed but other contexts will be preserved: {item.query}: {result}",
                    exc_info=result,
                )
                continue
            if result:
                context.append(result)

        # One bounded gap pass is allowed after all initial evidence is visible.
        # Follow-ups are distinct from both the original prompt and initial work
        # items, and still execute at most three-way parallel.
        self.evidence_conflicts = self._find_evidence_conflicts()
        followup_queries = self._build_gap_followups(
            original_query=query,
            work_items=work_items,
            initial_results=context_results,
        )
        if followup_queries:
            self._gap_followup_rounds = 1
            self.gap_followup_queries = followup_queries
            self._write_worker_status(
                work_items=[item.to_dict() for item in work_items],
                gap_followups=len(followup_queries),
            )
            followup_results = await asyncio.gather(
                *[
                    self._process_sub_query(followup, [], query_domains)
                    for followup in followup_queries
                ],
                return_exceptions=True,
            )
            for followup, result in zip(followup_queries, followup_results):
                if isinstance(result, Exception):
                    self.logger.error("Gap follow-up failed: %s: %s", followup, result)
                elif result:
                    context.append(result)
            self.evidence_conflicts = self._find_evidence_conflicts()

        self._write_worker_status(
            work_items=[item.to_dict() for item in work_items],
            gap_followups=len(self.gap_followup_queries),
            evidence_sources=len({item.source_url for item in self.evidence_items}),
            evidence_conflicts=len(self.evidence_conflicts),
        )

        if context:
            combined_context = " ".join(context)
            self.logger.info(f"Combined context size: {len(combined_context)}")
            return combined_context
        return []

    def _build_gap_followups(
        self,
        *,
        original_query: str,
        work_items: list[ResearchWorkItem],
        initial_results: list[Any],
    ) -> list[str]:
        """Create at most one round of three non-duplicate evidence follow-ups."""

        if self._gap_followup_rounds:
            return []
        minimum_sources = max(
            1, int(os.getenv("RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM", "2"))
        )
        used = {self._query_key(original_query)}
        used.update(self._query_key(item.query) for item in work_items)
        followups: list[str] = []
        handled_stock_item: ResearchWorkItem | None = None

        # A single combined stock follow-up tends to over-index on U.S. names.
        # When the market-daily stock lane is weak, reserve the bounded gap
        # round for standalone regional queries, prioritizing the Asian markets.
        for item, result in zip(work_items, initial_results):
            if not self._is_market_stock_work_item(item):
                continue
            region_counts = self._market_stock_region_source_counts(item.query)
            source_count = self._query_http_source_counts.get(item.query, 0)
            failed = isinstance(result, Exception) or not str(result or "").strip()
            minimum_per_market = max(
                1,
                int(os.getenv("RESEARCH_MIN_STOCK_SOURCES_PER_MARKET", "4")),
            )
            missing_regions = [
                region
                for region, count in region_counts.items()
                if count < minimum_per_market
            ]
            # A broad stock lane can appear numerically complete while still
            # lacking target-date rows for the harder Asian markets. Whenever
            # any regional stock gap remains, spend the single bounded
            # follow-up round on Japan, Korea and Hong Kong; the initial lane
            # is consistently strongest for U.S. names.
            force_asian_regions = bool(missing_regions) or self._stock_initial_codex_failed(
                item
            )
            if (
                not failed
                and source_count >= minimum_sources
                and not missing_regions
                and not force_asian_regions
            ):
                continue
            handled_stock_item = item
            for followup in self._market_stock_gap_followups(
                item,
                region_counts=region_counts,
                minimum_per_market=minimum_per_market,
                force_asian_regions=force_asian_regions,
            ):
                key = self._query_key(followup)
                if key in used:
                    continue
                used.add(key)
                followups.append(followup)
                if len(followups) == 3:
                    return followups
            break

        for item, result in zip(work_items, initial_results):
            if item is handled_stock_item:
                continue
            source_count = self._query_http_source_counts.get(item.query, 0)
            failed = isinstance(result, Exception) or not str(result or "").strip()
            if not failed and source_count >= minimum_sources:
                continue
            missing = max(1, minimum_sources - source_count)
            followup = (
                f"{item.query}\nEvidence gap follow-up: locate at least {missing} additional "
                "independent primary HTTP(S) sources for "
                f"{', '.join(item.coverage_tags)}. Verify exact dates and values; do not repeat "
                "previously used sources."
            )
            key = self._query_key(followup)
            if key not in used:
                used.add(key)
                followups.append(followup)
            if len(followups) == 3:
                return followups

        # Contradictory numeric claims warrant targeted corroboration even when
        # every work item met the minimum source count.
        for conflict in self.evidence_conflicts:
            followup = (
                "Resolve this source conflict with a current primary source and explain the "
                f"as-of date and unit: {conflict['claim']} values={conflict['values']}"
            )
            key = self._query_key(followup)
            if key not in used:
                used.add(key)
                followups.append(followup)
            if len(followups) == 3:
                break

        minimum_total = self._minimum_total_http_sources()
        total_sources = len({item.source_url for item in self.evidence_items})
        if total_sources < minimum_total and len(followups) < 3:
            shortfall = minimum_total - total_sources
            for item in sorted(
                work_items,
                key=lambda work_item: self._query_http_source_counts.get(work_item.query, 0),
            ):
                if item is handled_stock_item:
                    continue
                followup = (
                    f"{item.query}\nTotal-source gap follow-up: collect additional distinct, "
                    f"direct primary HTTP(S) sources toward a report-wide shortfall of {shortfall}. "
                    "Do not repeat any previously used URL."
                )
                key = self._query_key(followup)
                if key not in used:
                    used.add(key)
                    followups.append(followup)
                if len(followups) == 3:
                    break
        return followups

    @staticmethod
    def _is_market_stock_work_item(item: ResearchWorkItem) -> bool:
        tags = " ".join(item.coverage_tags).casefold()
        return (
            tags.count("stocks >=4") >= 3
            or (
                "at least 16 distinct stocks" in item.query.casefold()
                and "hong kong" in item.query.casefold()
            )
        )

    def _market_stock_region_source_counts(self, query: str) -> dict[str, int]:
        """Count regional sources that contain an identifiable single-stock record."""

        markers = {
            "Japan": (
                "japan",
                "tokyo stock exchange",
                "jpx.co.jp",
                ".co.jp",
                ".jp/",
                "nikkei",
                "topix",
            ),
            "Korea": (
                "korea",
                "krx",
                "kospi",
                "kosdaq",
                ".co.kr",
                ".kr/",
            ),
            "Hong Kong": (
                "hong kong",
                "hkex",
                "hkg:",
                ".com.hk",
                ".hk/",
                "hang seng",
            ),
            "U.S.": (
                "united states",
                "u.s.",
                "nasdaq",
                "nyse",
                "sec.gov",
                "s&p 500",
            ),
        }
        sources_by_region: dict[str, set[str]] = {region: set() for region in markers}
        for evidence in self._evidence_by_query.get(query, []):
            haystack = " ".join(
                (
                    evidence.claim,
                    evidence.source_title,
                    evidence.summary,
                    evidence.source_url,
                )
            ).casefold()
            for region, region_markers in markers.items():
                if (
                    any(marker in haystack for marker in region_markers)
                    and self._is_recognizable_stock_source(region, evidence, haystack)
                ):
                    sources_by_region[region].add(evidence.source_url)
        return {region: len(urls) for region, urls in sources_by_region.items()}

    @staticmethod
    def _is_recognizable_stock_source(
        region: str,
        evidence: EvidenceItem,
        haystack: str,
    ) -> bool:
        """Reject index-only articles and accept explicit single-stock provenance."""

        url = evidence.source_url.casefold()
        structured_labels = (
            "market",
            "company",
            "ticker",
            "exchange",
            "close",
            "change",
        )
        structured_record = (
            haystack.count("|") >= 5
            and all(label in haystack for label in structured_labels)
            and bool(re.search(r"\d", haystack))
        )
        ticker_patterns = {
            "Japan": (
                r"\b\d{4}\.t\b",
                r"\b(?:tse|tyo|tokyo stock exchange)\s*[:：]?\s*\d{4}\b",
            ),
            "Korea": (
                r"\b\d{6}\.(?:ks|kq)\b",
                r"\b(?:krx|kospi|kosdaq)\s*[:：]?\s*\d{6}\b",
            ),
            "Hong Kong": (
                r"\b\d{3,5}\.hk\b",
                r"\b(?:hkg|hkex)\s*[:：]?\s*\d{3,5}\b",
            ),
            "U.S.": (
                r"\b(?:nasdaq|nyse)\s*[:：]\s*[a-z]{1,5}\b",
                r"\$[a-z]{1,5}\b",
            ),
        }
        regional_ticker = any(
            re.search(pattern, haystack) for pattern in ticker_patterns[region]
        )
        structured_rows = [line for line in haystack.splitlines() if line.count("|") >= 5]
        structured_record = structured_record and (
            regional_ticker or len(structured_rows) >= 2
        )
        ticker_label = re.search(
            r"\bticker\s*[:=|]\s*([a-z0-9.$-]{1,12})",
            haystack,
        )
        explicit_ticker_exchange = bool(
            ticker_label
            and ticker_label.group(1) not in {"ticker", "exchange", "n/a"}
            and re.search(r"\b(?:exchange|listed\s+on)\s*[:=|]?", haystack)
        )
        quote_slug = evidence.source_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        index_quote_tokens = (
            "kospi",
            "kosdaq",
            "nikkei",
            "topix",
            "hang-seng",
            "hstech",
            "^ks11",
            "^kq11",
            "^n225",
            "^hsi",
        )
        quote_identity = regional_ticker or (
            region == "U.S." and bool(re.fullmatch(r"[A-Z]{1,5}", quote_slug))
        )
        quote_page = bool(
            re.search(r"/(?:quote|quotes|equities|stocks?)/[^/?#]+", url)
            and quote_identity
            and not any(token in quote_slug.casefold() for token in index_quote_tokens)
        )
        company_ir = bool(
            re.search(r"/(?:ir|investor|investors|investor-relations)(?:/|$)", url)
        )
        priced_stock = regional_ticker and any(
            marker in haystack
            for marker in (" close", " closed", " price", " change", "%", " stock")
        )
        return structured_record or explicit_ticker_exchange or quote_page or company_ir or priced_stock

    def _stock_initial_codex_failed(self, item: ResearchWorkItem) -> bool:
        """Return true when the initial stock-lane Codex call produced no valid result."""

        query_key = self._query_key(item.query)
        runs = [
            run
            for run in self._codex_run_metadata
            if run.get("initial_work_item")
            and self._query_key(str(run.get("query") or "")) == query_key
        ]
        return bool(runs) and not any(run.get("status") == "completed" for run in runs)

    def _market_stock_gap_followups(
        self,
        item: ResearchWorkItem,
        *,
        region_counts: dict[str, int],
        minimum_per_market: int,
        force_asian_regions: bool = False,
    ) -> list[str]:
        """Build up to three standalone regional stock/index evidence queries."""

        region_specs = {
            "Japan": (
                "Nikkei 225",
                "TOPIX",
                "Toyota 7203, SoftBank 9984, MUFG 8306, Tokyo Electron 8035, Sony 6758, Kioxia 285A",
                "JPX/Nikkei official history, MarketWatch download-data, and Investing.com historical pages",
            ),
            "Korea": (
                "KOSPI",
                "KOSDAQ",
                "Samsung Electronics 005930, SK Hynix 000660, Hyundai Motor 005380, Naver 035420, Kakao 035720, Krafton 259960",
                "KRX official history, MarketWatch or Investing.com historical pages, and company IR",
            ),
            "Hong Kong": (
                "Hang Seng Index",
                "Hang Seng TECH Index",
                "Tencent 0700, Alibaba 9988, Meituan 3690, BYD 1211, Xiaomi 1810, HKEX 0388, CNOOC 0883",
                "Hang Seng Indexes historical data, HKEX, MarketWatch download-data, Investing.com, and company IR",
            ),
            "U.S.": (
                "S&P 500",
                "Nasdaq Composite",
                "NVIDIA NVDA, Apple AAPL, Microsoft MSFT, Micron MU, PepsiCo PEP, MARA, Dell DELL, SanDisk SNDK",
                "NYSE/Nasdaq/company IR, MarketWatch historical pages, AP, and SEC filings",
            ),
        }
        # Ties deliberately favor Japan, Korea, then Hong Kong. If U.S.
        # coverage is uniquely worse, it can still enter the three-query round.
        priority = {"Japan": 0, "Korea": 1, "Hong Kong": 2, "U.S.": 3}
        missing_regions = [
            region
            for region in region_specs
            if region_counts.get(region, 0) < minimum_per_market
        ]
        if force_asian_regions:
            selected = ["Japan", "Korea", "Hong Kong"]
        else:
            selected = sorted(
                missing_regions,
                key=lambda region: (region_counts.get(region, 0), priority[region]),
            )[:3]
        target_date = os.getenv("MCP_RESEARCH_TARGET_DATE", "the requested target date")
        timezone = os.getenv("MCP_RESEARCH_TIMEZONE", "Asia/Singapore")

        followups = []
        for region in selected:
            first_index, second_index, candidate_pool, source_hints = region_specs[region]
            followups.append(
                f"Market-daily regional evidence gap — {region}. Target trading date: "
                f"{target_date}; timezone: {timezone}. Investigate at least four {region}-listed "
                "stocks: at least two index-weight/high-liquidity leaders and at least two "
                "target-date event-driven or unusually moving names. For every stock provide "
                "company, ticker, exchange, exact target-date close, daily percentage change, "
                "target-date catalyst, recent fundamental background, principal risk, and a "
                "direct HTTP(S) source; use exchange filings and company IR where possible. "
                f"Use this only as a discovery starting pool, then select dynamically from names "
                f"with complete target-date evidence: {candidate_pool}. "
                f"Search likely direct data surfaces including {source_hints}. "
                f"Also report the exact target-date close and daily change for {first_index} and "
                f"{second_index}, with two independent direct HTTP(S) URLs for each index. "
                "Do not use estimates, do not substitute another trading date, and do not repeat "
                "previously used URLs."
            )
        return followups

    @staticmethod
    def _minimum_total_http_sources() -> int:
        return max(
            1,
            int(
                os.getenv(
                    "MCP_RESEARCH_MIN_HTTP_SOURCES",
                    os.getenv("RESEARCH_MIN_TOTAL_HTTP_SOURCES", "25"),
                )
            ),
        )

    def _find_evidence_conflicts(self) -> list[dict[str, Any]]:
        """Detect same-claim evidence that reports incompatible explicit values."""

        grouped: dict[str, dict[str, Any]] = {}
        for item in self.evidence_items:
            if item.value is None:
                continue
            if isinstance(item.value, bool):
                continue
            if isinstance(item.value, (int, float)):
                numeric = str(item.value)
            else:
                raw_value = str(item.value).strip()
                # Conflict detection is intentionally numeric. Structured table
                # rows or prose in a schema's value field are not competing
                # measurements of one claim.
                if len(raw_value) > 64 or "|" in raw_value:
                    continue
                numeric = re.sub(r"[$€£¥,%\s]", "", raw_value)
                if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", numeric):
                    continue
            try:
                decimal_value = Decimal(numeric)
            except InvalidOperation:
                continue
            if not decimal_value.is_finite():
                continue
            normalized_value = (
                "0"
                if decimal_value == 0
                else format(decimal_value.normalize(), "f")
            )
            normalized_claim = re.sub(r"[^\w]+", " ", item.claim.casefold()).strip()
            if not normalized_claim:
                continue
            claim_key = "|".join(
                (
                    normalized_claim,
                    (item.unit or "").strip().casefold(),
                    (item.as_of_date or "").strip(),
                )
            )
            group = grouped.setdefault(
                claim_key,
                {"claim": item.claim, "values": set(), "sources": set()},
            )
            group["values"].add(normalized_value)
            group["sources"].add(item.source_url)

        conflicts = []
        for group in grouped.values():
            if len(group["values"]) <= 1:
                continue
            conflicts.append(
                {
                    "claim": group["claim"],
                    "values": sorted(group["values"]),
                    "sources": sorted(group["sources"]),
                }
            )
        return conflicts

    def _get_mcp_strategy(self) -> str:
        """
        Get the MCP strategy configuration.
        
        Priority:
        1. Instance-level setting (self.researcher.mcp_strategy)
        2. Config file setting (self.researcher.cfg.mcp_strategy) 
        3. Default value ("fast")
        
        Returns:
            str: MCP strategy
                "disabled" = Skip MCP entirely
                "fast" = Run MCP once with original query (default)
                "deep" = Run MCP for all sub-queries
        """
        # Check instance-level setting first
        if hasattr(self.researcher, 'mcp_strategy') and self.researcher.mcp_strategy is not None:
            return self.researcher.mcp_strategy
        
        # Check config setting
        if hasattr(self.researcher.cfg, 'mcp_strategy'):
            return self.researcher.cfg.mcp_strategy
        
        # Default to fast mode
        return "fast"

    async def _execute_mcp_research_for_queries(self, queries: list, mcp_retrievers: list) -> list:
        """
        Execute MCP research for a list of queries.
        
        Args:
            queries: List of queries to research
            mcp_retrievers: List of MCP retriever classes
            
        Returns:
            list: Combined MCP context entries from all queries
        """
        all_mcp_context = []
        
        for i, query in enumerate(queries, 1):
            self.logger.info(f"Executing MCP research for query {i}/{len(queries)}: {query}")
            
            for retriever in mcp_retrievers:
                try:
                    mcp_results = await self._execute_mcp_research(retriever, query)
                    if mcp_results:
                        for result in mcp_results:
                            content = result.get("body", "")
                            url = result.get("href", "")
                            title = result.get("title", "")
                            
                            if content:
                                context_entry = {
                                    "content": content,
                                    "url": url,
                                    "title": title,
                                    "query": query,
                                    "source_type": "mcp"
                                }
                                all_mcp_context.append(context_entry)
                        
                        self.logger.info(f"Added {len(mcp_results)} MCP results for query: {query}")
                        
                        if self.researcher.verbose:
                            await stream_output(
                                "logs",
                                "mcp_results_cached",
                                f"✅ Cached {len(mcp_results)} MCP results from query {i}/{len(queries)}",
                                self.researcher.websocket,
                            )
                except Exception as e:
                    self.logger.error(f"Error in MCP research for query '{query}': {e}")
                    if self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_cache_error",
                            f"⚠️ MCP research error for query {i}, continuing with other sources",
                            self.researcher.websocket,
                        )
        
        return all_mcp_context

    async def _process_sub_query(
        self,
        sub_query: str,
        scraped_data: list | None = None,
        query_domains: list | None = None,
    ):
        """Takes in a sub query and scrapes urls based on it and gathers context."""
        if scraped_data is None:
            scraped_data = []
        if query_domains is None:
            query_domains = []
        if self.json_handler:
            self.json_handler.log_event("sub_query", {
                "query": sub_query,
                "scraped_data_size": len(scraped_data)
            })
        
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "running_subquery_research",
                f"\n🔍 Running research for '{sub_query}'...",
                self.researcher.websocket,
            )

        try:
            # Identify MCP retrievers
            mcp_retrievers = [r for r in self.researcher.retrievers if "mcpretriever" in r.__name__.lower()]
            # Initialize context components
            mcp_context = []
            web_context = ""
            
            # Get MCP strategy configuration
            mcp_strategy = self._get_mcp_strategy()
            
            # **CONFIGURABLE MCP PROCESSING**
            if mcp_retrievers:
                if mcp_strategy == "disabled":
                    # MCP disabled - skip entirely
                    self.logger.info(f"MCP disabled for sub-query: {sub_query}")
                elif mcp_strategy == "fast" and self._mcp_results_cache is not None:
                    # Fast: Use cached results
                    mcp_context = self._mcp_results_cache.copy()
                    
                    if self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_cache_reuse",
                            f"♻️ Reusing cached MCP results ({len(mcp_context)} sources) for: {sub_query}",
                            self.researcher.websocket,
                        )
                    
                    self.logger.info(f"Reused {len(mcp_context)} cached MCP results for sub-query: {sub_query}")
                elif mcp_strategy == "deep":
                    # Deep: Run MCP for every sub-query
                    self.logger.info(f"Running deep MCP research for: {sub_query}")
                    if self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_comprehensive_run",
                            f"🔍 Running deep MCP research for: {sub_query}",
                            self.researcher.websocket,
                        )
                    
                    mcp_context = await self._execute_mcp_research_for_queries([sub_query], mcp_retrievers)
                else:
                    # Fallback: if no cache and not deep mode, run MCP for this query
                    self.logger.warning("MCP cache not available, falling back to per-sub-query execution")
                    if self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_fallback",
                            f"🔌 MCP cache unavailable, running MCP research for: {sub_query}",
                            self.researcher.websocket,
                        )
                    
                    mcp_context = await self._execute_mcp_research_for_queries([sub_query], mcp_retrievers)
            
            # Get web search context using non-MCP retrievers (if no scraped data provided)
            if not scraped_data:
                scraped_data = await self._scrape_data_by_urls(sub_query, query_domains)
                self.logger.info(f"Scraped data size: {len(scraped_data)}")

            # Get similar content based on scraped data
            if scraped_data:
                web_context = await self.researcher.context_manager.get_similar_content_by_query(sub_query, scraped_data)
                self.logger.info(f"Web content found for sub-query: {len(str(web_context)) if web_context else 0} chars")

            # Combine MCP context with web context intelligently
            combined_context = self._combine_mcp_and_web_context(mcp_context, web_context, sub_query)
            
            # Log context combination results
            if combined_context:
                context_length = len(str(combined_context))
                self.logger.info(f"Combined context for '{sub_query}': {context_length} chars")
                
                if self.researcher.verbose:
                    mcp_count = len(mcp_context)
                    web_available = bool(web_context)
                    cache_used = self._mcp_results_cache is not None and mcp_retrievers and mcp_strategy != "deep"
                    cache_status = " (cached)" if cache_used else ""
                    await stream_output(
                        "logs",
                        "context_combined",
                        f"📚 Combined research context: {mcp_count} MCP sources{cache_status}, {'web content' if web_available else 'no web content'}",
                        self.researcher.websocket,
                    )
            else:
                self.logger.warning(f"No combined context found for sub-query: {sub_query}")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "subquery_context_not_found",
                        f"🤷 No content found for '{sub_query}'...",
                        self.researcher.websocket,
                    )
            
            if combined_context and self.json_handler:
                self.json_handler.log_event("content_found", {
                    "sub_query": sub_query,
                    "content_size": len(str(combined_context)),
                    "mcp_sources": len(mcp_context),
                    "web_content": bool(web_context)
                })
                
            return combined_context
            
        except Exception as e:
            self.logger.error(f"Error processing sub-query {sub_query}: {e}", exc_info=True)
            if self.researcher.verbose:
                await stream_output(
                    "logs",
                    "subquery_error",
                    f"❌ Error processing '{sub_query}': {str(e)}",
                    self.researcher.websocket,
                )
            return ""

    async def _execute_mcp_research(self, retriever, query):
        """
        Execute MCP research using the new two-stage approach.
        
        Args:
            retriever: The MCP retriever class
            query: The search query
            
        Returns:
            list: MCP research results
        """
        retriever_name = retriever.__name__
        
        self.logger.info(f"Executing MCP research with {retriever_name} for query: {query}")
        
        try:
            # Instantiate the MCP retriever with proper parameters
            # Pass the researcher instance (self.researcher) which contains both cfg and mcp_configs
            retriever_instance = retriever(
                query=query, 
                headers=self.researcher.headers,
                query_domains=self.researcher.query_domains,
                websocket=self.researcher.websocket,
                researcher=self.researcher  # Pass the entire researcher instance
            )
            
            if self.researcher.verbose:
                await stream_output(
                    "logs",
                    "mcp_retrieval_stage1",
                    f"🧠 Stage 1: Selecting optimal MCP tools for: {query}",
                    self.researcher.websocket,
                )
            
            # Execute the two-stage MCP search
            results = retriever_instance.search(
                max_results=self.researcher.cfg.max_search_results_per_query
            )
            
            if results:
                result_count = len(results)
                self.logger.info(f"MCP research completed: {result_count} results from {retriever_name}")
                
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_research_complete",
                        f"🎯 MCP research completed: {result_count} intelligent results obtained",
                        self.researcher.websocket,
                    )
                
                return results
            else:
                self.logger.info(f"No results returned from MCP research with {retriever_name}")
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "mcp_no_results",
                        f"ℹ️ No relevant information found via MCP for: {query}",
                        self.researcher.websocket,
                    )
                return []
                
        except Exception as e:
            self.logger.error(f"Error in MCP research with {retriever_name}: {str(e)}")
            if self.researcher.verbose:
                await stream_output(
                    "logs",
                    "mcp_research_error",
                    f"⚠️ MCP research error: {str(e)} - continuing with other sources",
                    self.researcher.websocket,
                )
            return []

    def _combine_mcp_and_web_context(self, mcp_context: list, web_context: str, sub_query: str) -> str:
        """
        Intelligently combine MCP and web research context.
        
        Args:
            mcp_context: List of MCP context entries
            web_context: Web research context string  
            sub_query: The sub-query being processed
            
        Returns:
            str: Combined context string
        """
        combined_parts = []
        
        # Add web context first if available
        if web_context and web_context.strip():
            combined_parts.append(web_context.strip())
            self.logger.debug(f"Added web context: {len(web_context)} chars")
        
        # Add MCP context with proper formatting
        if mcp_context:
            mcp_formatted = []
            
            for i, item in enumerate(mcp_context):
                content = item.get("content", "")
                url = item.get("url", "")
                title = item.get("title", f"MCP Result {i+1}")
                
                if content and content.strip():
                    # Create a well-formatted context entry
                    if url and url != "mcp://llm_analysis":
                        citation = f"\n\n*Source: {title} ({url})*"
                    else:
                        citation = f"\n\n*Source: {title}*"
                    
                    formatted_content = f"{content.strip()}{citation}"
                    mcp_formatted.append(formatted_content)
            
            if mcp_formatted:
                # Join MCP results with clear separation
                mcp_section = "\n\n---\n\n".join(mcp_formatted)
                combined_parts.append(mcp_section)
                self.logger.debug(f"Added {len(mcp_context)} MCP context entries")
        
        # Combine all parts
        if combined_parts:
            final_context = "\n\n".join(combined_parts)
            self.logger.info(f"Combined context for '{sub_query}': {len(final_context)} total chars")
            return final_context
        else:
            self.logger.warning(f"No context to combine for sub-query: {sub_query}")
            return ""

    async def _process_sub_query_with_vectorstore(self, sub_query: str, filter: dict | None = None):
        """Takes in a sub query and gathers context from the user provided vector store

        Args:
            sub_query (str): The sub-query generated from the original query

        Returns:
            str: The context gathered from search
        """
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "running_subquery_with_vectorstore_research",
                f"\n🔍 Running research for '{sub_query}'...",
                self.researcher.websocket,
            )

        context = await self.researcher.context_manager.get_similar_content_by_query_with_vectorstore(sub_query, filter)

        return context

    async def _get_new_urls(self, url_set_input):
        """Gets the new urls from the given url set.
        Args: url_set_input (set[str]): The url set to get the new urls from
        Returns: list[str]: The new urls from the given url set
        """

        new_urls = []
        for url in url_set_input:
            if url not in self.researcher.visited_urls:
                self.researcher.visited_urls.add(url)
                new_urls.append(url)
                if self.researcher.verbose:
                    await stream_output(
                        "logs",
                        "added_source_url",
                        f"✅ Added source url to research: {url}\n",
                        self.researcher.websocket,
                        True,
                        url,
                    )

        return new_urls

    async def _search_relevant_source_urls(self, query, query_domains: list | None = None):
        new_search_urls = []
        prefetched_content = []
        snippet_fallback_content = []
        if query_domains is None:
            query_domains = []

        search_results = await self._get_search_results_from_all_retrievers(query, query_domains)

        # Separate results that already have content from those needing scraping.
        for result in search_results:
            url = result.get("href") or result.get("url")
            raw_content = result.get("raw_content")
            if url and raw_content and len(raw_content) > 100:
                # Only raw_content signals that a retriever already fetched the full page.
                # body is snippet-sized text for most web retrievers and still needs scraping.
                self.researcher.visited_urls.add(url)
                prefetched_content.append({
                    "url": url,
                    "source": url,
                    "raw_content": raw_content,
                    "title": result.get("title", ""),
                })
                self.researcher.add_research_sources([{"url": url}])
            elif url:
                new_search_urls.append(url)
                body = result.get("body", "")
                if body and len(body) > 100:
                    snippet_fallback_content.append({
                        "url": url,
                        "source": url,
                        "raw_content": body,
                        "title": result.get("title", ""),
                    })

        # Get unique URLs
        new_search_urls = await self._get_new_urls(new_search_urls)
        random.shuffle(new_search_urls)

        return new_search_urls, prefetched_content, snippet_fallback_content

    async def _get_search_results_from_all_retrievers(
        self,
        query,
        query_domains: list | None = None,
        *,
        exclude_retriever_names: set[str] | None = None,
        record_evidence: bool = True,
    ):
        """Run the same query through every configured non-MCP retriever and merge results."""
        if query_domains is None:
            query_domains = []
        excluded = {name.casefold() for name in (exclude_retriever_names or set())}

        retriever_classes = [
            retriever_class
            for retriever_class in self.researcher.retrievers
            if "mcpretriever" not in retriever_class.__name__.lower()
            and retriever_class.__name__.casefold() not in excluded
        ]

        async def run_retriever(retriever_class):
            retriever_name = retriever_class.__name__
            semaphore = self._get_retriever_semaphore(retriever_name)
            try:
                async with semaphore:
                    is_codex = retriever_name.casefold() == "codexsearch"
                    if is_codex:
                        retriever = retriever_class(
                            query,
                            query_domains=query_domains,
                        )
                        is_initial_codex = False
                        self._codex_calls += 1
                        initial_query_keys = {
                            self._query_key(item.query) for item in self.research_work_items
                        }
                        is_initial_codex = self._query_key(query) in initial_query_keys
                        if is_initial_codex:
                            self._codex_initial_calls += 1
                        self._active_codex += 1
                        self._active_codex_peak = max(
                            self._active_codex_peak,
                            self._active_codex,
                        )
                        self._write_worker_status()
                        try:
                            results = await retriever.search_async(
                                max_results=self.researcher.cfg.max_search_results_per_query
                            )
                        finally:
                            for run in getattr(retriever, "run_history", []):
                                self._codex_run_metadata.append(
                                    {
                                        **run,
                                        "query": query,
                                        "initial_work_item": is_initial_codex,
                                    }
                                )
                            self._active_codex = max(0, self._active_codex - 1)
                            self._write_worker_status()
                    else:
                        results = []
                        retriever_queries = self._lightweight_web_retriever_queries(query)
                        # Queries remain sequential inside one semaphore lease;
                        # across report workers the configured ordinary-retriever
                        # ceiling still bounds live network calls.
                        for retriever_query in retriever_queries:
                            retriever = retriever_class(
                                retriever_query,
                                query_domains=query_domains,
                            )
                            if hasattr(retriever, "search_async"):
                                batch = await retriever.search_async(
                                    max_results=self.researcher.cfg.max_search_results_per_query
                                )
                            else:
                                batch = await asyncio.to_thread(
                                    retriever.search,
                                    max_results=self.researcher.cfg.max_search_results_per_query,
                                )
                            results.extend(batch or [])
                self.logger.info(
                    f"{retriever_name} returned {len(results or [])} results for query: {query}"
                )
                return retriever_name, results or []
            except Exception as e:
                self.logger.error(f"Error searching with {retriever_name}: {e}")
                return retriever_name, []

        retrieval_tasks = [
            run_retriever(retriever_class) for retriever_class in retriever_classes
        ]
        if (
            yahoo_instruments_for_initial_market_lane(query)
            or yahoo_instruments_for_initial_commodities_lane(query)
            or yahoo_instruments_for_initial_equities_lane(query)
            or yahoo_instruments_for_regional_gap(query)
        ):
            retrieval_tasks.append(self._run_yahoo_chart_fallback(query))
        if (
            index_html_supplements_for_initial_market_lane(query)
            or index_html_supplements_for_regional_gap(query)
        ):
            retrieval_tasks.append(self._run_index_html_supplements(query))
        gathered = await asyncio.gather(*retrieval_tasks)

        merged_results = []
        results_by_key: dict[str, dict[str, Any]] = {}
        for retriever_name, results in gathered:
            for result in results:
                if not isinstance(result, dict):
                    continue
                url = result.get("href") or result.get("url")
                canonical_url = canonical_http_url(str(url or ""))
                dedupe_key = canonical_url or url or f"{retriever_name}:{result.get('title', '')}:{result.get('body', '')[:120]}"
                if dedupe_key in results_by_key:
                    self._merge_duplicate_search_result(
                        results_by_key[dedupe_key],
                        result,
                        retriever_name,
                    )
                    continue
                if canonical_url:
                    result["href"] = canonical_url
                result.setdefault("retriever", retriever_name)
                merged_results.append(result)
                results_by_key[dedupe_key] = result

        if record_evidence:
            self._record_search_evidence(query, merged_results)
        return merged_results

    async def _run_yahoo_chart_fallback(
        self,
        query: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Fetch exact allowlisted history without making Yahoo a general retriever."""

        instruments = (
            yahoo_instruments_for_initial_market_lane(query)
            or yahoo_instruments_for_initial_commodities_lane(query)
            or yahoo_instruments_for_initial_equities_lane(query)
            or yahoo_instruments_for_regional_gap(query)
        )
        target_date = target_date_for_regional_gap(
            query,
            os.getenv("MCP_RESEARCH_TARGET_DATE"),
        )
        if not instruments or target_date is None:
            return "YahooChart", []
        semaphore = self._get_retriever_semaphore("YahooChart")

        async def fetch_one(instrument):
            try:
                async with semaphore:
                    quote = await asyncio.to_thread(
                        fetch_yahoo_chart,
                        instrument,
                        target_date,
                    )
                return quote.to_search_result()
            except Exception as exc:
                self.logger.info(
                    "Yahoo chart fallback skipped %s: %s",
                    instrument.symbol,
                    exc,
                )
                return None

        fetched = await asyncio.gather(*(fetch_one(instrument) for instrument in instruments))
        return "YahooChart", [result for result in fetched if result is not None]

    async def _run_index_html_supplements(
        self,
        query: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Fetch allowlisted index pages in the shared ordinary pool."""

        supplements = (
            index_html_supplements_for_initial_market_lane(query)
            or index_html_supplements_for_regional_gap(query)
        )
        target_date = target_date_for_regional_gap(
            query,
            os.getenv("MCP_RESEARCH_TARGET_DATE"),
        )
        if not supplements or target_date is None:
            return "IndexHtml", []
        semaphore = self._get_retriever_semaphore("IndexHtml")

        async def fetch_one(supplement):
            try:
                async with semaphore:
                    quote = await asyncio.to_thread(
                        fetch_index_html_supplement,
                        supplement,
                        target_date,
                    )
                return quote.to_search_result()
            except Exception as exc:
                self.logger.info(
                    "Index HTML supplement skipped %s (%s): %s",
                    supplement.symbol,
                    supplement.provider,
                    exc,
                )
                return None

        fetched = await asyncio.gather(
            *(fetch_one(supplement) for supplement in supplements)
        )
        return "IndexHtml", [result for result in fetched if result is not None]

    @staticmethod
    def _merge_duplicate_search_result(
        existing: dict[str, Any],
        incoming: dict[str, Any],
        retriever_name: str,
    ) -> None:
        """Preserve provenance/evidence when retrievers cite the same URL."""

        retrievers = existing.setdefault(
            "retrievers",
            [str(existing.get("retriever") or "")],
        )
        if retriever_name not in retrievers:
            retrievers.append(retriever_name)

        existing_evidence = existing.get("evidence")
        incoming_evidence = incoming.get("evidence")
        if isinstance(existing_evidence, dict):
            existing_evidence = [existing_evidence]
        if not isinstance(existing_evidence, list):
            existing_evidence = []
        if isinstance(incoming_evidence, dict):
            incoming_evidence = [incoming_evidence]
        if isinstance(incoming_evidence, list):
            evidence_by_checksum = {
                str(item.get("checksum") or json.dumps(item, sort_keys=True, default=str)): item
                for item in existing_evidence
                if isinstance(item, dict)
            }
            for item in incoming_evidence:
                if not isinstance(item, dict):
                    continue
                checksum = str(item.get("checksum") or json.dumps(item, sort_keys=True, default=str))
                evidence_by_checksum.setdefault(checksum, item)
            existing["evidence"] = list(evidence_by_checksum.values())

        incoming_raw = str(incoming.get("raw_content") or "").strip()
        existing_raw = str(existing.get("raw_content") or "").strip()
        if len(incoming_raw) > len(existing_raw):
            existing["raw_content"] = incoming_raw
        incoming_body = str(incoming.get("body") or "").strip()
        existing_body = str(existing.get("body") or "").strip()
        if incoming_body and incoming_body not in existing_body:
            existing["body"] = "\n\n".join(part for part in (existing_body, incoming_body) if part)

    def _record_search_evidence(self, query: str, results: list[dict[str, Any]]) -> None:
        query_evidence: list[EvidenceItem] = []
        source_urls: set[str] = set()
        for result in results:
            url = canonical_http_url(str(result.get("href") or result.get("url") or ""))
            if url is None:
                continue
            evidence_before = len(query_evidence)
            structured = result.get("evidence")
            if isinstance(structured, dict):
                structured = [structured]
            added_structured = False
            if isinstance(structured, list):
                for item in structured:
                    if not isinstance(item, dict):
                        continue
                    try:
                        evidence_kwargs: dict[str, Any] = dict(
                            claim=str(item.get("claim") or result.get("body") or ""),
                            value=item.get("value"),
                            unit=str(item["unit"]) if item.get("unit") is not None else None,
                            as_of_date=(
                                str(item["as_of_date"])
                                if item.get("as_of_date") is not None
                                else None
                            ),
                            source_url=url,
                            source_title=str(
                                item.get("source_title") or result.get("title") or ""
                            ),
                            retriever=str(
                                item.get("retriever") or result.get("retriever") or ""
                            ),
                            summary=str(item.get("summary") or result.get("body") or ""),
                        )
                        if item.get("retrieved_at"):
                            evidence_kwargs["retrieved_at"] = str(item["retrieved_at"])
                        evidence = EvidenceItem(**evidence_kwargs)
                    except (TypeError, ValueError):
                        continue
                    query_evidence.append(evidence)
                    added_structured = True
            if not added_structured:
                evidence = EvidenceItem.from_search_result(
                    result,
                    retriever=str(result.get("retriever") or ""),
                )
                if evidence is not None:
                    query_evidence.append(evidence)
            if len(query_evidence) > evidence_before:
                source_urls.add(url)

        query_evidence = deduplicate_evidence(query_evidence)
        self._evidence_by_query[query] = query_evidence
        self._query_http_source_counts[query] = len(source_urls)
        for item in query_evidence:
            self._evidence_by_checksum.setdefault(item.checksum, item)

    def _get_retriever_semaphore(self, retriever_name: str):
        """Limit expensive retrievers so sub-query fan-out does not overload the system."""
        semaphore_key = (
            "CodexSearch" if retriever_name == "CodexSearch" else "__ordinary__"
        )
        if semaphore_key not in self._retriever_semaphores:
            if semaphore_key == "CodexSearch":
                limit = min(
                    3,
                    max(1, int(os.getenv("CODEX_SEARCH_RETRIEVER_CONCURRENCY", "3"))),
                )
            else:
                limit = int(os.getenv("SEARCH_RETRIEVER_CONCURRENCY", "4"))
            self._retriever_semaphores[semaphore_key] = asyncio.Semaphore(max(1, limit))
        return self._retriever_semaphores[semaphore_key]

    async def _scrape_data_by_urls(self, sub_query, query_domains: list | None = None):
        """
        Runs a sub-query across multiple retrievers and scrapes the resulting URLs.
        Retrievers that already provide full content (e.g. PubMed Central) have their
        content passed through directly without re-scraping.

        Args:
            sub_query (str): The sub-query to search for.

        Returns:
            list: A list of scraped content results.
        """
        if query_domains is None:
            query_domains = []

        (
            new_search_urls,
            prefetched_content,
            snippet_fallback_content,
        ) = await self._search_relevant_source_urls(sub_query, query_domains)

        # Log the research process if verbose mode is on
        if self.researcher.verbose:
            await stream_output(
                "logs",
                "researching",
                "🤔 Researching for relevant information across multiple sources...\n",
                self.researcher.websocket,
            )

        # Scrape URLs that need fetching (skip those already provided by retrievers)
        scraped_content = await self.researcher.scraper_manager.browse_urls(new_search_urls)

        # Merge pre-fetched content from retrievers that already provide full text
        scraped_content.extend(prefetched_content)
        if not scraped_content and snippet_fallback_content:
            self.logger.warning(
                "Live scraping returned no usable content; falling back to search result snippets."
            )
            scraped_content.extend(snippet_fallback_content)

        if self.researcher.vector_store:
            self.researcher.vector_store.load(scraped_content)

        return scraped_content

    async def _search(self, retriever, query):
        """
        Perform a search using the specified retriever.
        
        Args:
            retriever: The retriever class to use
            query: The search query
            
        Returns:
            list: Search results
        """
        retriever_name = retriever.__name__
        is_mcp_retriever = "mcpretriever" in retriever_name.lower()
        
        self.logger.info(f"Searching with {retriever_name} for query: {query}")
        
        try:
            # Instantiate the retriever
            retriever_instance = retriever(
                query=query, 
                headers=self.researcher.headers,
                query_domains=self.researcher.query_domains,
                websocket=self.researcher.websocket if is_mcp_retriever else None,
                researcher=self.researcher if is_mcp_retriever else None
            )
            
            # Log MCP server configurations if using MCP retriever
            if is_mcp_retriever and self.researcher.verbose:
                await stream_output(
                    "logs",
                    "mcp_retrieval",
                    f"🔌 Consulting MCP server(s) for information on: {query}",
                    self.researcher.websocket,
                )
            
            # Perform the search
            if hasattr(retriever_instance, 'search'):
                results = retriever_instance.search(
                    max_results=self.researcher.cfg.max_search_results_per_query
                )
                
                # Log result information
                if results:
                    result_count = len(results)
                    self.logger.info(f"Received {result_count} results from {retriever_name}")
                    
                    # Special logging for MCP retriever
                    if is_mcp_retriever:
                        if self.researcher.verbose:
                            await stream_output(
                                "logs",
                                "mcp_results",
                                f"✓ Retrieved {result_count} results from MCP server",
                                self.researcher.websocket,
                            )
                        
                        # Log result details
                        for i, result in enumerate(results[:3]):  # Log first 3 results
                            title = result.get("title", "No title")
                            url = result.get("href", "No URL")
                            content_length = len(result.get("body", "")) if result.get("body") else 0
                            self.logger.info(f"MCP result {i+1}: '{title}' from {url} ({content_length} chars)")
                            
                        if result_count > 3:
                            self.logger.info(f"... and {result_count - 3} more MCP results")
                else:
                    self.logger.info(f"No results returned from {retriever_name}")
                    if is_mcp_retriever and self.researcher.verbose:
                        await stream_output(
                            "logs",
                            "mcp_no_results",
                            f"ℹ️ No relevant information found from MCP server for: {query}",
                            self.researcher.websocket,
                        )
                
                return results
            else:
                self.logger.error(f"Retriever {retriever_name} does not have a search method")
                return []
        except Exception as e:
            self.logger.error(f"Error searching with {retriever_name}: {str(e)}")
            if is_mcp_retriever and self.researcher.verbose:
                await stream_output(
                    "logs",
                    "mcp_error",
                    f"❌ Error retrieving information from MCP server: {str(e)}",
                    self.researcher.websocket,
                )
            return []
            
    async def _extract_content(self, results):
        """
        Extract content from search results using the browser manager.
        
        Args:
            results: Search results
            
        Returns:
            list: Extracted content
        """
        self.logger.info(f"Extracting content from {len(results)} search results")
        
        # Get the URLs from the search results
        urls = []
        for result in results:
            if isinstance(result, dict) and "href" in result:
                urls.append(result["href"])
        
        # Skip if no URLs found
        if not urls:
            return []
            
        # Make sure we don't visit URLs we've already visited
        new_urls = [url for url in urls if url not in self.researcher.visited_urls]
        
        # Return empty if no new URLs
        if not new_urls:
            return []
            
        # Scrape the content from the URLs
        scraped_content = await self.researcher.scraper_manager.browse_urls(new_urls)
        
        # Add the URLs to visited_urls
        self.researcher.visited_urls.update(new_urls)
        
        return scraped_content
        
    async def _summarize_content(self, query, content):
        """
        Summarize the extracted content.
        
        Args:
            query: The search query
            content: The extracted content
            
        Returns:
            str: Summarized content
        """
        self.logger.info(f"Summarizing content for query: {query}")
        
        # Skip if no content
        if not content:
            return ""
            
        # Summarize the content using the context manager
        summary = await self.researcher.context_manager.get_similar_content_by_query(
            query, content
        )
        
        return summary
        
    async def _update_search_progress(self, current, total):
        """
        Update the search progress.
        
        Args:
            current: Current number of sub-queries processed
            total: Total number of sub-queries
        """
        if self.researcher.verbose and self.researcher.websocket:
            progress = int((current / total) * 100)
            await stream_output(
                "logs",
                "research_progress",
                f"📊 Research Progress: {progress}%",
                self.researcher.websocket,
                True,
                {
                    "current": current,
                    "total": total,
                    "progress": progress
                }
            )
