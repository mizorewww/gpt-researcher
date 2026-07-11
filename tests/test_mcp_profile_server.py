import asyncio
import json
import os
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gpt_researcher import mcp_profile_server
from gpt_researcher.evidence import EvidenceItem
from gpt_researcher.job_manager import JobManager
from tests.test_job_manager import FAKE_WORKER


class FakeResearcher:
    def __init__(self, *args, **kwargs):
        self.context = []
        self.visited_urls = {"https://example.com/placeholder"}
        self.costs = 0.0123

    async def conduct_research(self):
        return self.context

    async def write_report(self):
        return "I could not gather any source material. No sources were retrieved."

    def get_costs(self):
        return self.costs


class ErrorResearcher(FakeResearcher):
    async def conduct_research(self):
        raise ValueError("retriever exploded")


class TestMcpProfileServer(unittest.TestCase):
    @staticmethod
    def complete_index_ledger_evidence() -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for position, (name, ticker, aliases) in enumerate(
            mcp_profile_server._INDEX_LEDGER_SPECS
        ):
            encoded_ticker = ticker.replace("^", "%5E")
            if ticker == "998405.T":
                retriever = "IndexHtml"
                primary_title = "Yahoo! Finance Japan: TOPIX (998405.T) historical close"
                primary_url = "https://finance.yahoo.co.jp/quote/998405.T/history"
            elif ticker == "HSTECH":
                retriever = "IndexHtml"
                primary_title = "Investing.com: Hang Seng TECH Index historical close"
                primary_url = "https://www.investing.com/indices/hang-seng-tech-historical-data"
            else:
                retriever = "YahooChart"
                primary_title = f"Yahoo Finance chart: {name} ({ticker})"
                primary_url = (
                    "https://query2.finance.yahoo.com/v8/finance/chart/"
                    f"{encoded_ticker}?interval=1d&period1=1&period2=2"
                )
            common = {
                "as_of_date": "2026-07-09",
                "source_title": primary_title,
                "source_url": primary_url,
                "retriever": retriever,
            }
            evidence.extend(
                (
                    EvidenceItem(
                        claim=f"{ticker} target-date close",
                        value=1000 + position,
                        unit="index points",
                        **common,
                    ),
                    EvidenceItem(
                        claim=f"{ticker} target-date daily percentage change",
                        value=round(0.1 + position / 10, 6),
                        unit="percent",
                        **common,
                    ),
                )
            )
            if ticker == "998405.T":
                evidence.append(
                    EvidenceItem(
                        claim="TOPIX target-date close from Investing.com",
                        value=1000 + position,
                        unit="index points",
                        as_of_date="2026-07-09",
                        source_title="Investing.com TOPIX historical close",
                        source_url="https://www.investing.com/indices/topix-historical-data",
                        retriever="IndexHtml",
                    )
                )
            elif ticker == "HSTECH":
                evidence.append(
                    EvidenceItem(
                        claim="Hang Seng TECH target-date close",
                        value=1000 + position,
                        unit="index points",
                        as_of_date="2026-07-09",
                        source_title="Hang Seng Bank HSTECH historical close",
                        source_url="https://cbbc.hangseng.com/en-hk/market/stock/code/hstech",
                        retriever="IndexHtml",
                    )
                )
            else:
                canonical_label = aliases[0]
                evidence.append(
                    EvidenceItem(
                        claim=f"{canonical_label} target-date corroborating close",
                        summary=f"{canonical_label} closed on 2026-07-09",
                        as_of_date="2026-07-09",
                        source_title=f"{canonical_label} historical data",
                        source_url=f"https://secondary.test/index-{position}/history",
                        retriever="TavilySearch",
                    )
                )
        return evidence

    @staticmethod
    def complete_commodity_ledger_evidence() -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for position, (name, ticker, _aliases, unit, _basis) in enumerate(
            mcp_profile_server._COMMODITY_LEDGER_SPECS
        ):
            source_url = (
                "https://query2.finance.yahoo.com/v8/finance/chart/"
                f"{ticker.replace('=', '%3D')}?interval=1d&period1=1&period2=2"
            )
            common = {
                "as_of_date": "2026-07-09",
                "source_title": f"Yahoo Finance chart: {name} ({ticker})",
                "source_url": source_url,
                "retriever": "YahooChart",
            }
            evidence.extend(
                (
                    EvidenceItem(
                        claim=f"{ticker} target-date close",
                        value=70 + position,
                        unit="USD",
                        **common,
                    ),
                    EvidenceItem(
                        claim=f"{ticker} target-date daily percentage change",
                        value=round(-2 + position / 10, 6),
                        unit="percent",
                        **common,
                    ),
                    EvidenceItem(
                        claim=f"{name} target-date corroborating price",
                        summary=f"{name} settlement on 2026-07-09 in {unit}",
                        as_of_date="2026-07-09",
                        source_title=f"{name} futures settlement",
                        source_url=f"https://secondary.test/commodity-{position}/history",
                        retriever="TavilySearch",
                    ),
                )
            )
        return evidence

    @staticmethod
    def complete_stock_ledger_evidence() -> list[EvidenceItem]:
        pools = {
            "US": (
                "Nasdaq",
                "USD",
                (("Apple", "AAPL", 0.8), ("Microsoft", "MSFT", 0.3),
                 ("Nvidia", "NVDA", 3.0), ("Micron", "MU", -7.0),
                 ("Tesla", "TSLA", 7.0)),
            ),
            "Japan": (
                "Tokyo",
                "JPY",
                (("Toyota Motor", "7203.T", -2.2), ("SoftBank Group", "9984.T", -0.1),
                 ("Tokyo Electron", "8035.T", 5.5), ("Kioxia", "285A.T", 8.3),
                 ("Sony", "6758.T", -2.0)),
            ),
            "Korea": (
                "KSE",
                "KRW",
                (("Samsung Electronics", "005930.KS", 0.2),
                 ("SK Hynix", "000660.KS", 5.3),
                 ("Hyundai Motor", "005380.KS", -3.6),
                 ("Naver", "035420.KS", -4.3), ("Kakao", "035720.KS", -2.8)),
            ),
            "Hong Kong": (
                "HKSE",
                "HKD",
                (("Tencent", "0700.HK", -1.9), ("Alibaba", "9988.HK", 0.5),
                 ("Meituan", "3690.HK", -2.9), ("BYD", "1211.HK", -3.7),
                 ("Xiaomi", "1810.HK", -1.1)),
            ),
        }
        evidence: list[EvidenceItem] = []
        for market, (exchange, unit, candidates) in pools.items():
            for position, (company, ticker, change) in enumerate(candidates):
                url = (
                    "https://query2.finance.yahoo.com/v8/finance/chart/"
                    f"{ticker}?interval=1d&period1=1&period2=2"
                )
                summary = (
                    f"Market: {market} | Company/Index: {company} | Ticker: {ticker} | "
                    f"Exchange: {exchange} | Date: 2026-07-09 | Close: "
                    f"{100 + position} {unit} | Change: {change:+.1f}%"
                )
                common = {
                    "as_of_date": "2026-07-09",
                    "source_title": f"Yahoo Finance chart: {company} ({ticker})",
                    "source_url": url,
                    "retriever": "YahooChart",
                    "summary": summary,
                }
                evidence.extend(
                    (
                        EvidenceItem(
                            claim=f"{ticker} target-date close",
                            value=100 + position,
                            unit=unit,
                            **common,
                        ),
                        EvidenceItem(
                            claim=f"{ticker} target-date daily percentage change",
                            value=change,
                            unit="percent",
                            **common,
                        ),
                    )
                )
        return evidence

    @staticmethod
    def complete_market_report() -> str:
        index_names = (
            "S&P 500",
            "Dow Jones",
            "Nasdaq Composite",
            "Russell 2000",
            "Nikkei 225",
            "TOPIX",
            "KOSPI",
            "KOSDAQ",
            "Hang Seng Index",
            "Hang Seng TECH",
        )
        commodity_names = ("WTI", "Brent", "Gold", "Copper")
        lines = [
            f"| {name} | 100 | +1% | 2026-07-09 | driver | https://a.test/{i} | https://b.test/{i} |"
            for i, name in enumerate(index_names)
        ]
        lines.extend(
            f"| {name} | 100 | USD/unit | Aug-2026 futures | +1% | 2026-07-09 | driver | https://c.test/{i} | https://d.test/{i} |"
            for i, name in enumerate(commodity_names)
        )
        markets = ("美国", "日本", "韩国", "香港")
        for market_index, market in enumerate(markets):
            for stock_index in range(4):
                selection = "liquid leader" if stock_index < 2 else "event mover"
                ticker = f"T{market_index}{stock_index}"
                lines.append(
                    f"| {market} | Company {ticker} | {ticker} | EX | 10 | +2% | "
                    f"{selection} | catalyst | fundamentals | risks | "
                    f"https://ir.test/{ticker} |"
                )
        return "\n".join(lines)

    def test_market_coverage_gate_requires_complete_tables(self):
        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观", self.complete_market_report()
        )

        self.assertTrue(audit["passed"])
        self.assertEqual(audit["distinct_stocks"], 16)
        self.assertEqual(audit["stock_counts_by_market"], {
            "US": 4,
            "Japan": 4,
            "Korea": 4,
            "Hong Kong": 4,
        })

    def test_market_coverage_gate_fails_missing_or_single_source_rows(self):
        report = self.complete_market_report()
        report = report.replace(
            "| KOSDAQ | 100 | +1% | 2026-07-09 | driver | https://a.test/7 | https://b.test/7 |",
            "| KOSDAQ | 100 | +1% | 2026-07-09 | driver | https://a.test/7 |",
        )
        report = report.replace("| 香港 | Company T33", "| 香港 | Company T33 |  |")

        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观", report
        )

        self.assertFalse(audit["passed"])
        self.assertIn("KOSDAQ", audit["indices_without_two_direct_sources"])

    def test_market_coverage_accepts_combined_double_source_cells(self):
        report = self.complete_market_report()
        for index in range(10):
            report = report.replace(
                f"https://a.test/{index} | https://b.test/{index}",
                f"https://a.test/{index} ; https://b.test/{index}",
            )
        for index in range(4):
            report = report.replace(
                f"https://c.test/{index} | https://d.test/{index}",
                f"https://c.test/{index} ; https://d.test/{index}",
            )

        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观",
            report,
            target_date="2026-07-09",
        )

        self.assertTrue(audit["passed"])
        self.assertEqual(audit["indices_without_two_direct_sources"], [])
        self.assertEqual(audit["commodities_without_two_direct_sources"], [])

    def test_market_coverage_accepts_deterministic_short_index_labels(self):
        report = self.complete_market_report()
        report = report.replace("| Dow Jones |", "| Dow |")
        report = report.replace("| Nasdaq Composite |", "| Nasdaq |")
        report = report.replace("| Hang Seng Index |", "| Hang Seng |")
        report = report.replace("| TOPIX |", "| 东证指数 |")

        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观",
            report,
            target_date="2026-07-09",
        )

        self.assertTrue(audit["passed"])
        self.assertEqual(audit["missing_indices"], [])
        self.assertEqual(audit["indices_without_two_direct_sources"], [])
        self.assertEqual(audit["invalid_or_unverified_index_rows"], [])

        translated = report.replace("| Nasdaq |", "| 纳斯达克 |")
        self.assertTrue(
            mcp_profile_server._market_report_coverage(
                "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观",
                translated,
                target_date="2026-07-09",
            )["passed"]
        )

    def test_market_coverage_does_not_count_hang_seng_tech_as_cash_index(self):
        report = self.complete_market_report().replace(
            "| Hang Seng Index | 100 | +1% | 2026-07-09 | driver | https://a.test/8 | https://b.test/8 |\n",
            "",
        )

        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观",
            report,
            target_date="2026-07-09",
        )

        self.assertFalse(audit["passed"])
        self.assertIn("Hang Seng", audit["missing_indices"])
        self.assertIn("Hang Seng", audit["indices_without_two_direct_sources"])
        self.assertIn("Hang Seng", audit["invalid_or_unverified_index_rows"])

    def test_market_coverage_gate_requires_the_frozen_target_date(self):
        report = self.complete_market_report().replace(
            "| Dow Jones | 100 | +1% | 2026-07-09 |",
            "| Dow Jones | 100 | +1% | 2026-07-08 |",
        ).replace(
            "| Brent | 100 | USD/unit | Aug-2026 futures | +1% | 2026-07-09 |",
            "| Brent | 100 | USD/unit | Aug-2026 futures | +1% | 2026-07-08 |",
        )

        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观",
            report,
            target_date="2026-07-09",
        )

        self.assertFalse(audit["passed"])
        self.assertEqual(audit["expected_target_date"], "2026-07-09")
        self.assertIn("Dow", audit["invalid_or_unverified_index_rows"])
        self.assertIn("Brent", audit["invalid_or_unverified_commodity_rows"])

    def test_market_coverage_accepts_bold_single_character_markets_and_ten_columns(self):
        lines = self.complete_market_report().splitlines()[:14]
        markets = ("**美**", "**日**", "**韩**", "**港**")
        for market_index, market in enumerate(markets):
            for stock_index in range(4):
                selection = "流动性龙头" if stock_index < 2 else "事件驱动型"
                ticker = f"T{market_index}{stock_index}"
                company = f"Company {ticker}"
                if market == "**港**" and stock_index == 2:
                    company = "立讯精密"
                    ticker = "-"
                lines.append(
                    f"| {market} | {company} | {ticker} | EX | 10 | +2% | "
                    f"{selection} | catalyst | fundamentals and risks | "
                    f"https://ir.test/{market_index}-{stock_index} |"
                )

        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观", "\n".join(lines)
        )

        self.assertFalse(audit["passed"])
        self.assertEqual(audit["missing_indices"], [])
        self.assertEqual(audit["indices_without_two_direct_sources"], [])
        self.assertEqual(audit["missing_commodities"], [])
        self.assertEqual(audit["commodities_without_two_direct_sources"], [])
        self.assertEqual(audit["distinct_stocks"], 15)
        self.assertEqual(
            audit["stock_counts_by_market"],
            {"US": 4, "Japan": 4, "Korea": 4, "Hong Kong": 3},
        )
        self.assertEqual(len(audit["incomplete_stock_rows"]), 1)
        self.assertIn("立讯精密", audit["incomplete_stock_rows"][0])

    def test_market_coverage_accepts_composite_market_labels(self):
        report = self.complete_market_report()
        replacements = {
            "| 美国 |": "| **美国 (US)** |",
            "| 日本 |": "| **日本 (Japan)** |",
            "| 韩国 |": "| **韩国 (Korea)** |",
            "| 香港 |": "| **香港 (Hong Kong)** |",
        }
        for source, target in replacements.items():
            report = report.replace(source, target)

        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观", report
        )

        self.assertTrue(audit["passed"])
        self.assertEqual(audit["distinct_stocks"], 16)

    def test_market_coverage_rejects_estimates_and_non_direct_stock_sources(self):
        report = self.complete_market_report()
        report = report.replace(
            "| TOPIX | 100 | +1% |",
            "| TOPIX | 100 (估值) | +1% |",
        )
        report = report.replace(
            "| Copper | 100 | USD/unit |",
            "| Copper | 100 (estimated) | USD/unit |",
        )
        report = report.replace(
            "| 美国 | Company T00 | T00 | EX | 10 | +2% |",
            "| 美国 | Company T00 | T00 | EX | 10 (估值) | +2% |",
        ).replace("https://ir.test/T01", "https://ir.test/")
        report = report.replace("https://a.test/0", "https://a.test/")
        report = report.replace("https://c.test/0", "https://c.test/")

        audit = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观", report
        )

        self.assertFalse(audit["passed"])
        self.assertIn("S&P 500", audit["indices_without_two_direct_sources"])
        self.assertIn("WTI", audit["commodities_without_two_direct_sources"])
        self.assertIn("TOPIX", audit["invalid_or_unverified_index_rows"])
        self.assertIn("Copper", audit["invalid_or_unverified_commodity_rows"])
        self.assertTrue(any("T00" in row for row in audit["incomplete_stock_rows"]))
        self.assertTrue(any("T01" in row for row in audit["incomplete_stock_rows"]))

    def test_market_report_combines_coverage_and_unretrieved_url_feedback(self):
        report = self.complete_market_report().replace(
            "| TOPIX | 100 | +1% |",
            "| TOPIX | 100 (估值) | +1% |",
        )
        urls = sorted(set(re.findall(r"https?://[^\s|]+", report)))
        researcher = FakeResearcher()
        researcher.context = "grounded research context " * 200
        researcher.evidence_items = [
            EvidenceItem(claim=f"evidence {index}", source_url=url)
            for index, url in enumerate(urls[:-1])
        ]

        reason = mcp_profile_server._invalid_report_reason(
            report,
            researcher,
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观",
        )

        self.assertIsNotNone(reason)
        self.assertIn("coverage gate failed", reason)
        self.assertIn("absent from retrieved evidence", reason)
        self.assertIn(urls[-1], reason)

    def test_report_url_sanitizer_restores_equivalent_and_removes_invented_urls(self):
        report = self.complete_market_report()
        urls = sorted(set(re.findall(r"https?://[^\s|]+", report)))
        yahoo = (
            "https://query2.finance.yahoo.com/v8/finance/chart/285A.T"
            "?events=history&interval=1d"
        )
        researcher = FakeResearcher()
        researcher.context = "grounded research context " * 200
        researcher.evidence_items = [
            EvidenceItem(claim=f"evidence {index}", source_url=url)
            for index, url in enumerate([*urls, yahoo])
        ]
        report += (
            "\n| 日本 | Kioxia | 285A.T | Tokyo | 10 | +2% | event mover | "
            "catalyst | fundamentals | risks | "
            "[Yahoo](https://query2.finance.yahoo.com/v8/finance/chart/285a.t?events=history&interval=1d) |\n"
            "Invented [source](https://example.invalid/article) and "
            "https://example.invalid/bare"
        )

        sanitized, audit = mcp_profile_server._sanitize_report_urls(
            report, researcher
        )

        self.assertIn(yahoo, sanitized)
        self.assertNotIn("https://example.invalid", sanitized)
        self.assertIn("Invented source", sanitized)
        self.assertNotIn("[](", sanitized)
        self.assertTrue(audit["normalized"])
        self.assertEqual(len(audit["removed"]), 2)

    def test_writer_output_repair_keeps_complete_duplicate_restart(self):
        complete = "# Daily Report\n\n## Summary\ntext\n\n## Tables\n" + self.complete_market_report()
        corrupted = complete + "\n| US |" + complete

        repaired, audit = mcp_profile_server._repair_writer_output(corrupted)

        self.assertEqual(repaired, complete)
        self.assertEqual(audit[0]["repair"], "duplicate_report_restart")
        self.assertGreater(audit[0]["removed_prefix_chars"], 0)

    def test_structured_evidence_digest_keeps_distinct_table_rows(self):
        researcher = FakeResearcher()
        common = {
            "claim": "Market | Company | Ticker | Exchange | Close | Change",
            "retriever": "CodexSearch",
            "as_of_date": "2026-07-09",
        }
        researcher.evidence_items = [
            EvidenceItem(
                **common,
                summary="Japan | Toyota | 7203 | TSE | 2824 | -2.25%",
                source_url="https://example.com/toyota-a",
            ),
            EvidenceItem(
                **common,
                summary="Japan | Toyota | 7203 | TSE | 2824 | -2.25%",
                source_url="https://example.com/toyota-b",
            ),
            EvidenceItem(
                **common,
                summary="Japan | SoftBank | 9984 | TSE | 5757 | -0.10%",
                source_url="https://example.com/softbank",
            ),
        ]

        records = json.loads(mcp_profile_server._structured_evidence_digest(researcher))

        self.assertEqual(len(records), 2)
        toyota = next(record for record in records if "Toyota" in record["summary"])
        self.assertEqual(len(toyota["source_urls"]), 2)

    def test_writer_evidence_catalog_includes_market_balanced_web_sources(self):
        researcher = FakeResearcher()
        researcher.evidence_items = [
            EvidenceItem(
                claim="TOPIX exact close",
                value="4020.37",
                unit="index points",
                as_of_date="2026-07-09",
                source_url="https://example.com/codex-topix",
                retriever="CodexSearch",
            ),
            EvidenceItem(
                claim=("old rows " * 300) + "Jul 09, 2026 WTI settled at 74.23, down 0.30%",
                source_url="https://example.com/wti-history",
                source_title="WTI historical data",
                retriever="TavilySearch",
            ),
            EvidenceItem(
                claim="2026-07-09 KOSPI and Samsung exact closing prices +2.1%",
                source_url="https://example.com/korea-close",
                retriever="TavilySearch",
            ),
            EvidenceItem(
                claim="2026-07-09 Hang Seng and Tencent exact closing prices +1.2%",
                source_url="https://example.com/hong-kong-close",
                retriever="TavilySearch",
            ),
        ]

        with patch.dict(os.environ, {"MCP_RESEARCH_TARGET_DATE": "2026-07-09"}):
            catalog = json.loads(mcp_profile_server._writer_evidence_catalog(researcher))

        self.assertEqual(len(catalog["structured_codex_claims"]), 1)
        web_urls = {item["source_url"] for item in catalog["web_source_excerpts"]}
        self.assertEqual(
            web_urls,
            {
                "https://example.com/wti-history",
                "https://example.com/korea-close",
                "https://example.com/hong-kong-close",
            },
        )
        wti = next(
            item
            for item in catalog["web_source_excerpts"]
            if item["source_url"].endswith("wti-history")
        )
        self.assertIn("Jul 09, 2026 WTI settled at 74.23", wti["excerpt"])
        self.assertTrue(wti["excerpt"].startswith("…"))
        self.assertLessEqual(len(wti["excerpt"]), 902)

    def test_writer_catalog_preserves_yahoo_target_date_flat_row(self):
        researcher = FakeResearcher()
        source_url = (
            "https://query2.finance.yahoo.com/v8/finance/chart/0700.HK"
            "?period1=1782691200&period2=1783728000&interval=1d"
        )
        flat_row = (
            "Market: Hong Kong | Company/Index: Tencent | Ticker: 0700.HK | "
            "Exchange: Hong Kong | Date: 2026-07-09 | Close: 469.6 HKD | "
            "Change: -1.921470% | Previous close: 478.8 on 2026-07-08"
        )
        researcher.evidence_items = [
            EvidenceItem(
                claim="0700.HK target-date close",
                value=469.6,
                unit="HKD",
                as_of_date="2026-07-09",
                source_url=source_url,
                source_title="Yahoo Finance chart: Tencent (0700.HK)",
                retriever="YahooChart",
                summary=flat_row,
            )
        ]

        with patch.dict(os.environ, {"MCP_RESEARCH_TARGET_DATE": "2026-07-09"}):
            catalog = json.loads(mcp_profile_server._writer_evidence_catalog(researcher))

        yahoo = next(
            item
            for item in catalog["web_source_excerpts"]
            if "query2.finance.yahoo.com" in item["source_url"]
        )
        self.assertIn("Date: 2026-07-09", yahoo["excerpt"])
        self.assertIn("Close: 469.6 HKD", yahoo["excerpt"])
        self.assertIn("Change: -1.921470%", yahoo["excerpt"])

    def test_writer_evidence_catalog_respects_character_budget(self):
        researcher = FakeResearcher()
        researcher.evidence_items = [
            EvidenceItem(
                claim=f"2026-07-09 KOSPI close {index}.25 +1.2% " + ("detail " * 500),
                source_url=f"https://example.com/korea-{index}",
                retriever="TavilySearch",
            )
            for index in range(20)
        ]

        with patch.dict(os.environ, {"MCP_RESEARCH_TARGET_DATE": "2026-07-09"}):
            catalog = mcp_profile_server._writer_evidence_catalog(researcher, max_chars=2_500)

        self.assertLessEqual(len(catalog), 2_500)
        self.assertTrue(json.loads(catalog)["web_source_excerpts"])

    def test_writer_catalog_reserves_exact_rows_and_truncation_prone_sources(self):
        researcher = FakeResearcher()
        researcher.evidence_items = [
            EvidenceItem(
                claim="Japan row: SoftBank",
                summary=(
                    "Japan | SoftBank | 9984 | TSE | Not supported in accessible source | "
                    "+10.7% | event mover"
                ),
                as_of_date="2026-07-09",
                source_url="https://example.com/unsupported-softbank",
                retriever="CodexSearch",
            ),
            EvidenceItem(
                claim="Samsung Electronics target-date stock evidence",
                value=(
                    "Company: Samsung Electronics; ticker: 005930; exchange: KRX; "
                    "target trading date: 2026-07-09; close: KRW 278000; "
                    "daily change: +0.18%; catalyst: earnings; fundamentals: memory; "
                    "principal risk: cycle"
                ),
                as_of_date="2026-07-09",
                source_url="https://example.com/samsung-exact",
                retriever="CodexSearch",
            ),
            EvidenceItem(
                claim="09.07.2026 NIKKEI movers table Advantest +5.86%",
                source_title="Nikkei 225 Market Movers",
                source_url="https://markets.example.com/market-movers/nikkei_225",
                retriever="TavilySearch",
            ),
            EvidenceItem(
                claim="07/08/2026 Tencent close HKD478.80 historical row",
                source_title="Download 700 Data | Tencent Holdings Ltd. Price Data",
                source_url=(
                    "https://www.marketwatch.com/investing/stock/700/"
                    "download-data?countrycode=hk"
                ),
                retriever="TavilySearch",
            ),
            EvidenceItem(
                claim="2026-07-09 TOPIX close 4020.37 daily change +0.35%",
                source_title="TOPIX historical data source one",
                source_url="https://example.com/topix-history-one",
                retriever="TavilySearch",
            ),
            EvidenceItem(
                claim="2026-07-09 TOPIX close 4020.37 daily change +0.35%",
                source_title="TOPIX historical data source two",
                source_url="https://example.com/topix-history-two",
                retriever="TavilySearch",
            ),
        ]
        researcher.evidence_items.extend(
            EvidenceItem(
                claim=f"2026-07-09 generic S&P numeric source {index} +1.0%",
                source_url=f"https://noise.test/{index}",
                retriever="TavilySearch",
            )
            for index in range(20)
        )

        with patch.dict(os.environ, {"MCP_RESEARCH_TARGET_DATE": "2026-07-09"}):
            raw_catalog = mcp_profile_server._writer_evidence_catalog(
                researcher, max_chars=8_000
            )
        catalog = json.loads(raw_catalog)

        self.assertLessEqual(len(raw_catalog), 8_000)
        structured = json.dumps(catalog["structured_codex_claims"])
        self.assertIn("Samsung Electronics target-date", structured)
        self.assertNotIn("Not supported in accessible source", structured)
        web_urls = {item["source_url"] for item in catalog["web_source_excerpts"]}
        self.assertTrue(any("market-movers/nikkei_225" in url for url in web_urls))
        self.assertTrue(any("/stock/700/download-data" in url for url in web_urls))
        self.assertIn("https://example.com/topix-history-one", web_urls)
        self.assertIn("https://example.com/topix-history-two", web_urls)

    def test_market_writer_contract_is_final_and_forbids_vertical_stock_tables(self):
        contract = mcp_profile_server._market_writer_table_contract("2026-07-09")

        self.assertIn("NON-NEGOTIABLE FINAL TABLE CONTRACT", contract)
        self.assertIn("exact 2026-07-09 data", contract)
        self.assertIn('vertical "Item | Detail"', contract)
        self.assertIn("N/A", contract)
        self.assertIn("Direct source MUST be\n   the LAST column", contract)
        self.assertIn("Never shorten a retrieved URL to a domain homepage", contract)
        self.assertTrue(contract.rstrip().endswith("END NON-NEGOTIABLE CONTRACT ================"))

    def test_index_source_pair_ledger_is_complete_and_deterministic(self):
        evidence = self.complete_index_ledger_evidence()
        forward = mcp_profile_server._index_source_pair_ledger(
            SimpleNamespace(evidence_items=evidence), "2026-07-09"
        )
        reverse = mcp_profile_server._index_source_pair_ledger(
            SimpleNamespace(evidence_items=list(reversed(evidence))), "2026-07-09"
        )

        self.assertEqual(forward, reverse)
        ledger = json.loads(forward)
        self.assertEqual(len(ledger["entries"]), 10)
        self.assertEqual(
            [entry["ticker"] for entry in ledger["entries"]],
            [ticker for _name, ticker, _aliases in mcp_profile_server._INDEX_LEDGER_SPECS],
        )
        self.assertTrue(
            all(entry["source_1"] != entry["source_2"] for entry in ledger["entries"])
        )
        topix = next(entry for entry in ledger["entries"] if entry["ticker"] == "998405.T")
        hstech = next(entry for entry in ledger["entries"] if entry["ticker"] == "HSTECH")
        self.assertIn("finance.yahoo.co.jp", topix["source_1"])
        self.assertIn("investing.com", topix["source_2"])
        self.assertIn("investing.com", hstech["source_1"])
        self.assertIn("hangseng.com", hstech["source_2"])

    def test_commodity_source_pair_ledger_is_complete_and_deterministic(self):
        evidence = self.complete_commodity_ledger_evidence()
        forward = mcp_profile_server._commodity_source_pair_ledger(
            SimpleNamespace(evidence_items=evidence), "2026-07-09"
        )
        reverse = mcp_profile_server._commodity_source_pair_ledger(
            SimpleNamespace(evidence_items=list(reversed(evidence))), "2026-07-09"
        )

        self.assertEqual(forward, reverse)
        ledger = json.loads(forward)
        self.assertEqual(len(ledger["entries"]), 4)
        self.assertEqual(
            [entry["ticker"] for entry in ledger["entries"]],
            [
                ticker
                for _name, ticker, _aliases, _unit, _basis in (
                    mcp_profile_server._COMMODITY_LEDGER_SPECS
                )
            ],
        )
        self.assertTrue(
            all(entry["source_1"] != entry["source_2"] for entry in ledger["entries"])
        )
        self.assertEqual(
            {entry["currency_unit"] for entry in ledger["entries"]},
            {"USD/barrel", "USD/troy ounce", "USD/pound"},
        )

    def test_market_ledger_fidelity_requires_exact_values_and_sources(self):
        evidence = [
            *self.complete_index_ledger_evidence(),
            *self.complete_commodity_ledger_evidence(),
            *self.complete_stock_ledger_evidence(),
        ]
        researcher = SimpleNamespace(evidence_items=evidence)
        indices = json.loads(
            mcp_profile_server._index_source_pair_ledger(
                researcher, "2026-07-09"
            )
        )["entries"]
        commodities = json.loads(
            mcp_profile_server._commodity_source_pair_ledger(
                researcher, "2026-07-09"
            )
        )["entries"]
        stocks = json.loads(
            mcp_profile_server._stock_row_ledger(researcher, "2026-07-09")
        )["entries"]
        lines = [
            f"| {row['index']} | {row['close']} | {row['daily_move']} | "
            f"{row['data_date']} | driver | {row['source_1']} | {row['source_2']} |"
            for row in indices
        ]
        lines.extend(
            f"| {row['commodity']} | {row['price']} | {row['currency_unit']} | "
            f"{row['contract_basis']} | {row['daily_move']} | {row['data_date']} | "
            f"driver | {row['source_1']} | {row['source_2']} |"
            for row in commodities
        )
        lines.extend(
            f"| {row['market']} | {row['company']} | {row['ticker']} | "
            f"{row['exchange']} | {row['close']} | {row['daily_move']} | "
            f"{row['selection_type']} | catalyst | fundamentals | risks | "
            f"{row['direct_source']} |"
            for row in stocks
        )
        report = "\n".join(lines)

        passed = mcp_profile_server._market_ledger_fidelity(
            researcher, report, "2026-07-09"
        )
        bad_hsi = next(row for row in indices if row["index"] == "Hang Seng")
        corrupted = report.replace(
            bad_hsi["source_2"],
            "https://wrong.test/hstech",
            1,
        ).replace("| AAPL | Nasdaq | 100 USD |", "| AAPL | Nasdaq | 999 USD |")
        failed = mcp_profile_server._market_ledger_fidelity(
            researcher, corrupted, "2026-07-09"
        )
        repaired, repair_audit = mcp_profile_server._enforce_market_ledger_rows(
            corrupted, researcher, "2026-07-09"
        )
        repaired_fidelity = mcp_profile_server._market_ledger_fidelity(
            researcher, repaired, "2026-07-09"
        )

        self.assertTrue(passed["passed"])
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["index_mismatches"][0]["index"], "Hang Seng")
        self.assertEqual(failed["stock_mismatches"][0]["ticker"], "AAPL")
        self.assertTrue(repaired_fidelity["passed"])
        self.assertIn("catalyst", repaired)
        self.assertTrue(
            any(item["entity"] == "index:Hang Seng" for item in repair_audit)
        )
        self.assertTrue(any(item["entity"] == "stock:AAPL" for item in repair_audit))

        combined_sources = report
        for row in indices:
            combined_sources = combined_sources.replace(
                f"| {row['source_1']} | {row['source_2']} |",
                f"| {row['source_1']} ; {row['source_2']} |",
            )
        for row in commodities:
            combined_sources = combined_sources.replace(
                f"| {row['source_1']} | {row['source_2']} |",
                f"| {row['source_1']} ; {row['source_2']} |",
            )
        combined_repaired, _ = mcp_profile_server._enforce_market_ledger_rows(
            combined_sources, researcher, "2026-07-09"
        )
        self.assertTrue(
            mcp_profile_server._market_ledger_fidelity(
                researcher, combined_repaired, "2026-07-09"
            )["passed"]
        )

    def test_index_ledger_does_not_match_dow_through_gold_summary(self):
        dow_ticker = "^DJI"
        primary_url = (
            "https://query2.finance.yahoo.com/v8/finance/chart/%5EDJI"
            "?interval=1d&period1=1&period2=2"
        )
        evidence = [
            EvidenceItem(
                claim=f"{dow_ticker} target-date close",
                value=45000,
                unit="index points",
                as_of_date="2026-07-09",
                source_title="Yahoo Finance chart: Dow Jones (^DJI)",
                source_url=primary_url,
                retriever="YahooChart",
            ),
            EvidenceItem(
                claim=f"{dow_ticker} target-date daily percentage change",
                value=0.3,
                unit="percent",
                as_of_date="2026-07-09",
                source_title="Yahoo Finance chart: Dow Jones (^DJI)",
                source_url=primary_url,
                retriever="YahooChart",
            ),
            EvidenceItem(
                claim="Gold target-date market report",
                summary="AP also mentioned that the Dow Jones moved higher.",
                source_title="AP Gold and commodities update",
                source_url="https://ap.test/commodities/gold-update",
                retriever="TavilySearch",
            ),
        ]

        ledger = json.loads(
            mcp_profile_server._index_source_pair_ledger(
                SimpleNamespace(evidence_items=evidence), "2026-07-09"
            )
        )

        self.assertNotIn("^DJI", {entry["ticker"] for entry in ledger["entries"]})

    def test_index_ledger_omits_incomplete_primary_pairs(self):
        evidence = [
            EvidenceItem(
                claim="^GSPC target-date close",
                value=6200,
                unit="index points",
                as_of_date="2026-07-09",
                source_title="Yahoo Finance chart: S&P 500 (^GSPC)",
                source_url=(
                    "https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC"
                    "?interval=1d&period1=1&period2=2"
                ),
                retriever="YahooChart",
            ),
            EvidenceItem(
                claim="S&P 500 target-date corroboration",
                source_title="S&P 500 historical data",
                source_url="https://secondary.test/sp500/history",
                retriever="TavilySearch",
            ),
        ]

        ledger = json.loads(
            mcp_profile_server._index_source_pair_ledger(
                SimpleNamespace(evidence_items=evidence), "2026-07-09"
            )
        )

        self.assertEqual(ledger["entries"], [])

    def test_all_ledgers_are_immediately_before_final_contract(self):
        evidence = [
            *self.complete_index_ledger_evidence(),
            *self.complete_commodity_ledger_evidence(),
            *self.complete_stock_ledger_evidence(),
        ]
        constraints = mcp_profile_server._market_writer_final_constraints(
            SimpleNamespace(evidence_items=evidence),
            "2026-07-09",
        )

        ledger_end = "================ END INDEX SOURCE-PAIR LEDGER ================"
        commodity_start = "================ DETERMINISTIC COMMODITY SOURCE-PAIR LEDGER ================"
        commodity_end = "================ END COMMODITY SOURCE-PAIR LEDGER ================"
        stock_start = "================ DETERMINISTIC STOCK ROW LEDGER ================"
        stock_end = "================ END STOCK ROW LEDGER ================"
        contract_start = "================ NON-NEGOTIABLE FINAL TABLE CONTRACT ================"
        self.assertIn(f"{ledger_end}\n\n{commodity_start}", constraints)
        self.assertIn(f"{commodity_end}\n\n{stock_start}", constraints)
        self.assertIn(f"{stock_end}\n\n{contract_start}", constraints)
        self.assertIn("direct_source VERBATIM as the LAST column", constraints)
        self.assertTrue(
            constraints.rstrip().endswith("END NON-NEGOTIABLE CONTRACT ================")
        )

    def test_stock_row_ledger_selects_exactly_four_per_market_deterministically(self):
        evidence = self.complete_stock_ledger_evidence()
        forward = mcp_profile_server._stock_row_ledger(
            SimpleNamespace(evidence_items=evidence), "2026-07-09"
        )
        reverse = mcp_profile_server._stock_row_ledger(
            SimpleNamespace(evidence_items=list(reversed(evidence))), "2026-07-09"
        )

        self.assertEqual(forward, reverse)
        ledger = json.loads(forward)
        self.assertTrue(ledger["complete"])
        self.assertEqual(len(ledger["entries"]), 16)
        for market in mcp_profile_server._STOCK_LEDGER_LEADERS:
            rows = [row for row in ledger["entries"] if row["market"] == market]
            self.assertEqual(len(rows), 4)
            self.assertEqual(
                [row["selection_type"] for row in rows].count("liquid leader"), 2
            )
            self.assertEqual(
                [row["selection_type"] for row in rows].count("event mover"), 2
            )
        selected_movers = {
            row["ticker"]
            for row in ledger["entries"]
            if row["selection_type"] == "event mover"
        }
        self.assertEqual(
            selected_movers,
            {
                "MU",
                "TSLA",
                "285A.T",
                "8035.T",
                "035420.KS",
                "005380.KS",
                "1211.HK",
                "3690.HK",
            },
        )
        for row in ledger["entries"]:
            self.assertEqual(
                list(row)[:7],
                [
                    "market",
                    "company",
                    "ticker",
                    "exchange",
                    "close",
                    "daily_move",
                    "selection_type",
                ],
            )
            self.assertEqual(list(row)[-1], "direct_source")
            self.assertNotIn(
                "?",
                " ".join(str(row[key]) for key in list(row)[:7]),
            )

    def test_stock_row_ledger_omits_and_flags_incomplete_region(self):
        evidence = [
            item
            for item in self.complete_stock_ledger_evidence()
            if not item.claim.startswith("MSFT ")
        ]

        ledger = json.loads(
            mcp_profile_server._stock_row_ledger(
                SimpleNamespace(evidence_items=evidence), "2026-07-09"
            )
        )

        self.assertFalse(ledger["complete"])
        us_gap = next(gap for gap in ledger["gaps"] if gap.get("market") == "US")
        self.assertEqual(us_gap["missing_liquid_leaders"], ["MSFT"])
        self.assertFalse(any(row["market"] == "US" for row in ledger["entries"]))
        self.assertEqual(len(ledger["entries"]), 12)

    def test_markdown_urls_are_canonicalized_without_label_suffixes(self):
        report = self.complete_market_report()
        urls = sorted(set(re.findall(r"https?://[^\s|]+", report)))
        for url in urls:
            report = report.replace(url, f"[{url}]({url})")
        researcher = FakeResearcher()
        researcher.context = "grounded research context " * 200
        researcher.evidence_items = [
            EvidenceItem(claim=f"evidence {index}", source_url=url)
            for index, url in enumerate(urls)
        ]

        coverage = mcp_profile_server._market_report_coverage(
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观", report
        )
        reason = mcp_profile_server._invalid_report_reason(
            report,
            researcher,
            "帮我调研昨天的股票市场，美日韩港、大宗商品和宏观",
        )

        self.assertTrue(coverage["passed"])
        self.assertEqual(coverage["report_http_sources_count"], len(urls))
        self.assertIsNone(reason)

    def test_market_coverage_accepts_multiword_english_market_labels(self):
        report = self.complete_market_report().replace(
            "| 韩国 |", "| South Korea |"
        ).replace("| 香港 |", "| Hong Kong |")

        coverage = mcp_profile_server._market_report_coverage(
            "stock market daily report",
            report,
            target_date="2026-07-09",
        )

        self.assertTrue(coverage["passed"])
        self.assertEqual(coverage["stock_counts_by_market"]["Korea"], 4)
        self.assertEqual(coverage["stock_counts_by_market"]["Hong Kong"], 4)

    def test_report_metrics_do_not_count_visited_urls_as_sources(self):
        metrics = mcp_profile_server._report_metrics(FakeResearcher())

        self.assertEqual(metrics["sources_count"], 0)
        self.assertEqual(metrics["context_chunks_count"], 0)
        self.assertEqual(metrics["context_chars"], 0)
        self.assertEqual(metrics["visited_urls_count"], 1)

    def test_frontmatter_separates_sources_from_visited_urls(self):
        markdown = mcp_profile_server._frontmatter(
            task_id="task-1",
            title="Test",
            query="query",
            report_type="research_report",
            report_source="web",
            tone="objective",
            researcher=FakeResearcher(),
        )

        self.assertIn("sources_count: 0", markdown)
        self.assertIn("visited_urls_count: 1", markdown)
        self.assertIn("context_chars: 0", markdown)

    def test_research_report_raises_when_all_attempts_fail_quality(self):
        import gpt_researcher

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            with (
                patch.object(gpt_researcher, "GPTResearcher", FakeResearcher),
                patch.object(mcp_profile_server, "OUTPUT_DIR", output_dir),
                patch.dict(
                    "os.environ",
                    {
                        "MCP_RESEARCH_MIXED_ATTEMPTS": "1",
                        "MCP_RESEARCH_RETRIEVAL_ATTEMPTS": "1",
                        "MCP_RESEARCH_FALLBACK_RETRIEVER": "",
                        "RETRIEVER": "tavily,codex",
                    },
                    clear=False,
                ),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    asyncio.run(mcp_profile_server._run_research_report("market query"))

            payload = json.loads(str(cm.exception))

            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["report_type"], "research_report")
            self.assertEqual(payload["sources_count"], 0)
            self.assertEqual(payload["visited_urls_count"], 1)
            self.assertEqual(payload["context_chars"], 0)
            self.assertFalse(payload["fallback_used"])
            self.assertEqual(payload["http_sources_count"], 0)
            self.assertEqual(
                [attempt["stage"] for attempt in payload["attempts"]],
                ["retrieval"],
            )
            self.assertEqual(
                [attempt["status"] for attempt in payload["attempts"]],
                ["ok"],
            )

            failure_path = output_dir / "market query.failed.json"
            self.assertTrue(failure_path.exists())
            saved_payload = json.loads(failure_path.read_text())
            self.assertEqual(saved_payload["status"], "failed")

            self.assertEqual(list(output_dir.glob("*.md")), [])

    def test_research_report_structures_retrieval_errors_with_fallback_disabled(self):
        import gpt_researcher

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            with (
                patch.object(gpt_researcher, "GPTResearcher", ErrorResearcher),
                patch.object(mcp_profile_server, "OUTPUT_DIR", output_dir),
                patch.dict(
                    "os.environ",
                    {
                        "MCP_RESEARCH_MIXED_ATTEMPTS": "1",
                        "MCP_RESEARCH_RETRIEVAL_ATTEMPTS": "1",
                        "MCP_RESEARCH_FALLBACK_RETRIEVER": "",
                        "RETRIEVER": "tavily,codex",
                    },
                    clear=False,
                ),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    asyncio.run(mcp_profile_server._run_research_report("broken query"))

            payload = json.loads(str(cm.exception))

            self.assertEqual(payload["status"], "failed")
            self.assertEqual([attempt["status"] for attempt in payload["attempts"]], ["error"])
            self.assertEqual(payload["attempts"][0]["stage"], "retrieval")
            self.assertIn("ValueError: retriever exploded", payload["attempts"][0]["reason"])
            self.assertTrue((output_dir / "broken query.failed.json").exists())

    def test_research_report_start_status_result_use_isolated_worker(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = root / "fake_worker.py"
            worker.write_text(textwrap.dedent(FAKE_WORKER), encoding="utf-8")

            async def run_job():
                manager = JobManager(
                    root / "jobs",
                    worker_command=(__import__("sys").executable, str(worker)),
                    timeout_seconds=5,
                )
                with patch.object(mcp_profile_server, "_JOB_MANAGER", manager):
                    started = await mcp_profile_server.research_report_start(
                        "昨天的市场日报", timezone="Asia/Singapore"
                    )
                    self.assertEqual(started["status"], "queued")
                    self.assertNotEqual(
                        manager.compact_status(started["job_id"])["status"], "not_found"
                    )
                    while True:
                        status = await mcp_profile_server.research_report_status(
                            started["job_id"], wait_seconds=1
                        )
                        if status["status"] == "completed":
                            break
                    result = mcp_profile_server.research_report_result(started["job_id"])
                    expanded = mcp_profile_server.research_report_result(
                        started["job_id"], include_report=True
                    )
                await manager.shutdown()
                return started, status, result, expanded

            started, status, result, expanded = asyncio.run(run_job())

            self.assertEqual(status["status"], "completed")
            self.assertNotIn("result", status)
            self.assertEqual(result["http_sources_count"], 25)
            self.assertNotIn("report", result["result"])
            self.assertIn("report", expanded["result"])
            self.assertEqual(started["timezone"], "Asia/Singapore")


if __name__ == "__main__":
    unittest.main()
