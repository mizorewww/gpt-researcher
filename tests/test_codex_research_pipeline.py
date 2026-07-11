import asyncio
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_search.codex_search import bounded_timeout, build_codex_env, run_codex
from gpt_researcher.evidence import (
    EvidenceItem,
    canonical_http_url,
    unique_http_sources,
)
from gpt_researcher.retrievers.codex.codex import CodexSearch, _GlobalCodexSlot
from gpt_researcher.skills.researcher import ResearchConductor


class EvidenceTests(unittest.TestCase):
    def test_legacy_backend_has_no_market_specific_retrieval_path(self):
        project_root = Path(__file__).resolve().parents[1]
        self.assertFalse((project_root / "gpt_researcher/market_data.py").exists())
        generic_sources = "\n".join(
            path.read_text(encoding="utf-8").casefold()
            for path in (
                project_root / "gpt_researcher/skills/researcher.py",
                project_root / "gpt_researcher/mcp_profile_server.py",
            )
        )
        for forbidden in (
            "market-daily regional evidence gap",
            "_market_daily_work_items",
            "_market_report_coverage",
            "yahoochart",
            "hstech",
        ):
            self.assertNotIn(forbidden, generic_sources)

    def test_evidence_requires_real_http_and_canonicalizes_tracking(self):
        with self.assertRaises(ValueError):
            EvidenceItem(claim="x", source_url="codex-search://local")
        with self.assertRaises(ValueError):
            EvidenceItem(claim="   ", source_url="https://example.com")
        with self.assertRaises(ValueError):
            EvidenceItem(
                claim="invalid value",
                value={"nested": "object"},
                source_url="https://example.com",
            )

        first = EvidenceItem(
            claim="Close was 10",
            source_url="HTTPS://Example.COM/market/?utm_source=test&b=2&a=1#top",
        )
        second = EvidenceItem(
            claim="Another claim",
            source_url="https://example.com/market?a=1&b=2",
        )
        self.assertEqual(first.source_url, "https://example.com/market?a=1&b=2")
        self.assertEqual(unique_http_sources([first, second]), [first.source_url])
        self.assertIsNone(canonical_http_url("mcp://analysis"))
        self.assertEqual(
            canonical_http_url("https://example.test/path."),
            "https://example.test/path",
        )


class CodexResultParsingTests(unittest.TestCase):
    def test_retriever_hard_caps_timeout_and_retries(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_SEARCH_RETRIEVER_TIMEOUT": "999",
                "CODEX_SEARCH_RETRIEVER_RETRIES": "7",
            },
        ):
            retriever = CodexSearch("bounded")

        self.assertEqual(retriever.timeout, 300)
        self.assertEqual(retriever.retries, 1)
        self.assertEqual(bounded_timeout("999"), 300)

    def test_helper_and_inner_codex_environments_use_explicit_allowlists(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_SEARCH_CODEX_BIN": "/tmp/fake-codex",
                "CODEX_SEARCH_PRIVATE_TOKEN": "must-not-leak",
                "TAVILY_API_KEY": "must-not-leak-either",
                "HTTPS_PROXY": "http://127.0.0.1:8080",
            },
            clear=True,
        ):
            retriever_env = CodexSearch("env")._helper_env()
            inner_env = build_codex_env(SimpleNamespace(codex_home=None))

        self.assertEqual(retriever_env["CODEX_SEARCH_CODEX_BIN"], "/tmp/fake-codex")
        self.assertEqual(retriever_env["HTTPS_PROXY"], "http://127.0.0.1:8080")
        self.assertEqual(inner_env["HTTPS_PROXY"], "http://127.0.0.1:8080")
        for env in (retriever_env, inner_env):
            self.assertNotIn("CODEX_SEARCH_PRIVATE_TOKEN", env)
            self.assertNotIn("TAVILY_API_KEY", env)
        self.assertNotIn("CODEX_SEARCH_CODEX_BIN", inner_env)

    def test_helper_uses_schema_and_readonly_isolated_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inspection_path = root / "inspection.json"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import json
                    import os
                    import stat
                    import sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    workdir = Path(args[args.index("--cd") + 1])
                    schema = Path(args[args.index("--output-schema") + 1])
                    output = Path(args[args.index("--output-last-message") + 1])
                    write_failed = False
                    try:
                        (workdir / "should-not-write").write_text("x")
                    except OSError:
                        write_failed = True
                    Path(__INSPECTION__).write_text(json.dumps({
                        "args": args,
                        "workdir_mode": stat.S_IMODE(workdir.stat().st_mode),
                        "write_failed": write_failed,
                        "schema": json.loads(schema.read_text()),
                    }))
                    output.write_text(json.dumps({
                        "claims": [],
                        "sources": [],
                        "caveats": [],
                    }))
                    """
                )
                .replace("__INSPECTION__", repr(str(inspection_path)))
                .lstrip(),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            args = SimpleNamespace(
                codex_bin=str(fake_codex),
                model_provider=None,
                supports_websockets=False,
                reasoning_effort=None,
                service_tier=None,
                use_user_config=False,
                model=None,
                codex_home=None,
                workdir=None,
                timeout=5,
                show_events=False,
                telemetry_file=None,
            )

            run_codex(args, "test", search_enabled=True, label="search")
            inspection = json.loads(inspection_path.read_text(encoding="utf-8"))

        self.assertEqual(inspection["workdir_mode"], 0o555)
        self.assertTrue(inspection["write_failed"])
        self.assertEqual(
            inspection["args"][inspection["args"].index("--sandbox") + 1],
            "read-only",
        )
        self.assertIn("--skip-git-repo-check", inspection["args"])
        self.assertEqual(
            set(inspection["schema"]["required"]),
            {"claims", "sources", "caveats"},
        )

    def test_incomplete_thread_event_is_retryable_but_empty_sources_are_not(self):
        self.assertTrue(
            CodexSearch._is_transient(
                '{"type":"thread.started","thread_id":"test"}',
                "failed",
            )
        )
        self.assertFalse(
            CodexSearch._is_transient(
                "Codex returned no valid HTTP(S) sources",
                "invalid_output",
            )
        )
        self.assertFalse(
            CodexSearch._is_transient(
                '{"type":"thread.started"} You have hit your usage limit; purchase more credits',
                "failed",
            )
        )
        self.assertTrue(CodexSearch._is_transient("HTTP status 503", "failed"))
        self.assertFalse(
            CodexSearch._is_transient("Expected at most 500 characters", "failed")
        )

    def test_structured_payload_becomes_one_result_per_real_source(self):
        retriever = CodexSearch("market close")
        payload = {
            "claims": [
                {
                    "claim": "Index closed higher",
                    "value": 123.4,
                    "unit": "points",
                    "as_of_date": "2026-07-09",
                    "source_urls": ["https://exchange.example/data?utm_source=x"],
                    "summary": "Official close.",
                }
            ],
            "sources": [
                {
                    "url": "https://exchange.example/data",
                    "title": "Exchange data",
                    "summary": "Primary market data.",
                },
                {
                    "url": "codex-search://local",
                    "title": "Synthetic",
                    "summary": "Must be ignored.",
                },
            ],
            "caveats": [],
        }

        results = retriever._parse_results(json.dumps(payload), max_results=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["href"], "https://exchange.example/data")
        self.assertIn("Source URL: https://exchange.example/data", results[0]["body"])
        self.assertEqual(results[0]["evidence"][0]["value"], 123.4)

    def test_malformed_claim_is_isolated_without_failing_valid_source(self):
        retriever = CodexSearch("malformed claim")
        payload = {
            "claims": [
                {
                    "claim": "Boolean is outside the evidence schema",
                    "value": True,
                    "unit": None,
                    "as_of_date": None,
                    "source_urls": ["https://example.com/source"],
                    "summary": "invalid claim value",
                }
            ],
            "sources": [
                {
                    "url": "https://example.com/source",
                    "title": "Valid source",
                    "summary": "A valid source summary remains usable.",
                }
            ],
            "caveats": [],
        }

        results = retriever._parse_results(json.dumps(payload), max_results=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["href"], "https://example.com/source")
        self.assertEqual(
            results[0]["evidence"][0]["claim"],
            "A valid source summary remains usable.",
        )

    def test_legacy_markdown_fallback_keeps_only_http_links(self):
        retriever = CodexSearch("legacy")
        results = retriever._parse_results(
            "Finding supported by [Official](https://example.com/fact). Ignore codex-search://local.",
            max_results=5,
        )
        self.assertEqual(
            [result["href"] for result in results], ["https://example.com/fact"]
        )


class ConductorPlanningTests(unittest.IsolatedAsyncioTestCase):
    def test_conflict_detection_normalizes_equivalent_numeric_formats(self):
        conductor = ResearchConductor(SimpleNamespace())
        same_value = [
            EvidenceItem(
                claim="target-date metric",
                value=value,
                unit="points",
                as_of_date="2026-07-09",
                source_url=f"https://example.com/same-{index}",
            )
            for index, value in enumerate(("3,200.0", 3200))
        ]
        for item in same_value:
            conductor._evidence_by_checksum[item.checksum] = item
        self.assertEqual(conductor._find_evidence_conflicts(), [])

        different = EvidenceItem(
            claim="target-date metric",
            value=3201,
            unit="points",
            as_of_date="2026-07-09",
            source_url="https://example.com/different",
        )
        conductor._evidence_by_checksum[different.checksum] = different

        conflicts = conductor._find_evidence_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["values"], ["3200", "3201"])

    async def test_planning_presearch_excludes_codex_and_normalizes_three_queries(self):
        calls = {"codex": 0, "tavily": 0}

        class CodexSearch:
            def __init__(self, query, query_domains=None):
                calls["codex"] += 1

            async def search_async(self, max_results=5):
                raise AssertionError("Codex must not run during planning")

        class TavilySearch:
            def __init__(self, query, query_domains=None):
                self.query = query

            def search(self, max_results=5):
                calls["tavily"] += 1
                return [{"href": "https://example.com", "body": "planning context"}]

        researcher = SimpleNamespace(
            retrievers=[CodexSearch, TavilySearch],
            cfg=SimpleNamespace(max_search_results_per_query=5),
            websocket=None,
            role="analyst",
            parent_query="",
            report_type="research_report",
            add_costs=lambda *args, **kwargs: None,
            kwargs={},
        )
        conductor = ResearchConductor(researcher)

        with (
            patch(
                "gpt_researcher.skills.researcher.plan_research_outline",
                new=AsyncMock(return_value=["one", "two", "three", "four"]),
            ),
            patch(
                "gpt_researcher.skills.researcher.stream_output",
                new=AsyncMock(),
            ),
        ):
            queries = await conductor.plan_research("investigate this")

        self.assertEqual(queries, ["one", "two", "three"])
        self.assertEqual(len(conductor.research_work_items), 3)
        self.assertEqual(calls, {"codex": 0, "tavily": 1})

    def test_lightweight_retriever_query_is_domain_neutral_and_bounded(self):
        conductor = ResearchConductor(SimpleNamespace())
        query = "Investigate an arbitrary domain with primary evidence. " * 20

        compact = conductor._compact_web_retriever_query(query)

        self.assertLessEqual(len(compact), 380)
        self.assertEqual(conductor._lightweight_web_retriever_queries(query), [compact])

    def test_planner_work_item_count_is_not_forced_to_stress_test_width(self):
        conductor = ResearchConductor(SimpleNamespace())

        work_items = conductor._normalize_work_items(
            ["one sufficient evidence lane"], "generic research request"
        )

        self.assertEqual([item.query for item in work_items], ["one sufficient evidence lane"])

    def test_generic_gap_followups_preserve_each_work_item(self):
        conductor = ResearchConductor(SimpleNamespace())
        work_items = conductor._normalize_work_items([], "generic research request")
        conductor._query_http_source_counts = {item.query: 0 for item in work_items}

        with patch.dict(
            os.environ,
            {
                "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "2",
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "6",
            },
        ):
            followups = conductor._build_gap_followups(
                original_query="generic research request",
                work_items=work_items,
                initial_results=["", "", ""],
            )

        self.assertEqual(len(followups), 3)
        self.assertTrue(all("Evidence gap follow-up" in query for query in followups))
        self.assertTrue(
            all(item.query in followup for item, followup in zip(work_items, followups))
        )

    def test_same_url_merges_retriever_provenance_and_evidence(self):
        existing = {
            "href": "https://example.com/data",
            "retriever": "TavilySearch",
            "body": "Tavily snippet",
            "evidence": [{"checksum": "a", "claim": "first"}],
        }
        incoming = {
            "href": "https://example.com/data",
            "body": "Codex claim",
            "raw_content": "Long Codex evidence",
            "evidence": [{"checksum": "b", "claim": "second"}],
        }

        ResearchConductor._merge_duplicate_search_result(
            existing, incoming, "CodexSearch"
        )

        self.assertEqual(existing["retrievers"], ["TavilySearch", "CodexSearch"])
        self.assertEqual(
            {item["checksum"] for item in existing["evidence"]}, {"a", "b"}
        )
        self.assertIn("Tavily snippet", existing["body"])
        self.assertIn("Codex claim", existing["body"])

    def test_gap_followups_are_disabled_after_the_single_round(self):
        conductor = ResearchConductor(SimpleNamespace())
        work_items = conductor._normalize_work_items([], "generic research request")
        conductor._gap_followup_rounds = 1

        followups = conductor._build_gap_followups(
            original_query="generic research request",
            work_items=work_items,
            initial_results=["", "", ""],
        )

        self.assertEqual(followups, [])


class ConductorConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_ordinary_retrievers_share_one_four_slot_semaphore(self):
        conductor = ResearchConductor(SimpleNamespace())
        with patch.dict(os.environ, {"SEARCH_RETRIEVER_CONCURRENCY": "4"}):
            tavily = conductor._get_retriever_semaphore("TavilySearch")
            provider_a = conductor._get_retriever_semaphore("ProviderA")
            provider_b = conductor._get_retriever_semaphore("ProviderB")
            brave = conductor._get_retriever_semaphore("BraveSearch")
            codex = conductor._get_retriever_semaphore("CodexSearch")

        self.assertIs(tavily, provider_a)
        self.assertIs(tavily, provider_b)
        self.assertIs(tavily, brave)
        self.assertIsNot(tavily, codex)
        self.assertEqual(tavily._value, 4)

    async def test_hybrid_report_reuses_one_web_plan_for_local_context(self):
        class ContextManager:
            def __init__(self):
                self.queries = []

            async def get_similar_content_by_query(self, query, data):
                self.queries.append(query)
                return f"local:{query}"

        class PromptFamily:
            @staticmethod
            def join_local_web_documents(local, web):
                return f"{local}|{web}"

        researcher = SimpleNamespace(
            query="hybrid research",
            retrievers=[],
            cfg=SimpleNamespace(doc_path="unused", curate_sources=False),
            verbose=False,
            websocket=None,
            visited_urls=set(),
            agent="analyst",
            role="role",
            source_urls=[],
            complement_source_urls=False,
            report_source="hybrid",
            document_urls=[],
            vector_store=None,
            query_domains=[],
            context_manager=ContextManager(),
            prompt_family=PromptFamily(),
            get_costs=lambda: 0,
        )
        conductor = ResearchConductor(researcher)

        async def one_web_pass(query, scraped_data=None, query_domains=None):
            conductor.research_work_items = conductor._normalize_work_items(
                ["hybrid one", "hybrid two", "hybrid three"],
                query,
            )
            return "web context"

        conductor._get_context_by_web_search = AsyncMock(side_effect=one_web_pass)
        loader = SimpleNamespace(
            load=AsyncMock(return_value=[{"content": "local doc"}])
        )
        with patch(
            "gpt_researcher.skills.researcher.DocumentLoader",
            return_value=loader,
        ):
            context = await conductor.conduct_research()

        self.assertEqual(conductor._get_context_by_web_search.await_count, 1)
        self.assertEqual(
            researcher.context_manager.queries,
            ["hybrid one", "hybrid two", "hybrid three"],
        )
        self.assertIn("web context", context)

    async def test_generic_gap_followups_run_three_way_parallel(self):
        original = "Investigate a complex domain with independent evidence lanes"
        researcher = SimpleNamespace(
            retrievers=[],
            cfg=SimpleNamespace(),
            verbose=False,
            websocket=None,
        )
        conductor = ResearchConductor(researcher)
        work_items = conductor._normalize_work_items([], original)
        state = {"active": 0, "peak": 0}

        async def fake_plan(query, query_domains=None):
            conductor.research_work_items = work_items
            return [item.query for item in work_items]

        async def fake_process(query, scraped_data=None, query_domains=None):
            if "Evidence gap follow-up" in query:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                await asyncio.sleep(0.06)
                state["active"] -= 1
                return f"follow-up evidence for {query}"
            conductor._query_http_source_counts[query] = 0
            return ""

        conductor.plan_research = fake_plan
        conductor._process_sub_query = fake_process
        with patch.dict(
            os.environ,
            {
                "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "2",
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "6",
            },
        ):
            context = await conductor._get_context_by_web_search(original)

        self.assertTrue(context)
        self.assertEqual(len(conductor.gap_followup_queries), 3)
        self.assertEqual(state["peak"], 3)

    async def test_three_initial_codex_searches_overlap_without_fourth_original_query(
        self,
    ):
        state = {"active": 0, "peak": 0, "calls": 0}

        class CodexSearch:
            def __init__(self, query, query_domains=None):
                self.query = query
                self.run_history = []

            async def search_async(self, max_results=5):
                state["calls"] += 1
                call_number = state["calls"]
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                await asyncio.sleep(0.08)
                state["active"] -= 1
                self.run_history.append(
                    {
                        "pid": 1000 + call_number,
                        "attempt": 1,
                        "started_at": f"start-{call_number}",
                        "finished_at": f"finish-{call_number}",
                        "status": "completed",
                    }
                )
                suffix = abs(hash(self.query))
                return [
                    {
                        "href": f"https://codex.example/{suffix}",
                        "title": "Codex evidence",
                        "body": "source-backed evidence " * 20,
                        "raw_content": "source-backed evidence " * 20,
                    }
                ]

        class TavilySearch:
            def __init__(self, query, query_domains=None):
                self.query = query

            def search(self, max_results=5):
                suffix = abs(hash(self.query))
                return [
                    {
                        "href": f"https://tavily.example/{suffix}",
                        "title": "Tavily evidence",
                        "body": "independent evidence " * 20,
                        "raw_content": "independent evidence " * 20,
                    }
                ]

        class ContextManager:
            async def get_similar_content_by_query(self, query, data):
                return "\n".join(item["raw_content"] for item in data)

        class ScraperManager:
            async def browse_urls(self, urls):
                return []

        class FakeResearcher:
            def __init__(self):
                self.retrievers = [CodexSearch, TavilySearch]
                self.cfg = SimpleNamespace(max_search_results_per_query=5)
                self.verbose = False
                self.websocket = None
                self.visited_urls = set()
                self.research_sources = []
                self.context_manager = ContextManager()
                self.scraper_manager = ScraperManager()
                self.vector_store = None
                self.report_type = "research_report"
                self.query_domains = []

            def add_research_sources(self, sources):
                self.research_sources.extend(sources)

        conductor = ResearchConductor(FakeResearcher())

        async def fake_plan(query, query_domains=None):
            conductor.research_work_items = conductor._normalize_work_items(
                ["lane one", "lane two", "lane three", "lane four"], query
            )
            return [item.query for item in conductor.research_work_items]

        conductor.plan_research = fake_plan
        with patch.dict(
            os.environ,
            {
                "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "3",
                "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "1",
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "1",
            },
        ):
            context = await conductor._get_context_by_web_search("original query")

        self.assertTrue(context)
        self.assertEqual(state["calls"], 3)
        self.assertEqual(state["peak"], 3)
        self.assertEqual(len(conductor.research_work_items), 3)
        self.assertEqual(conductor.gap_followup_queries, [])
        self.assertEqual(conductor.evidence_metrics["codex_initial_calls"], 3)
        self.assertEqual(conductor.evidence_metrics["active_codex_peak"], 3)
        self.assertEqual(conductor.evidence_metrics["codex_run_count"], 3)
        self.assertEqual(conductor.evidence_metrics["codex_pids"], [1001, 1002, 1003])


class CodexCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_transient_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = CodexSearch("fatal")
            retriever.slot_directory = Path(directory) / "slots"
            retriever.retries = 1
            retriever._run_helper = AsyncMock(
                return_value=(1, "", "invalid configuration", 1234)
            )

            results = await retriever.search_async()

            self.assertEqual(results, [])
            self.assertEqual(retriever._run_helper.await_count, 1)

    async def test_transient_failure_retries_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = CodexSearch("transient")
            retriever.slot_directory = Path(directory) / "slots"
            retriever.retries = 1
            retriever.retry_delay = 0
            payload = {
                "claims": [],
                "sources": [
                    {
                        "url": "https://example.com/recovered",
                        "title": "Recovered",
                        "summary": "valid evidence",
                    }
                ],
                "caveats": [],
            }
            retriever._run_helper = AsyncMock(
                side_effect=[
                    (1, "", "HTTP status 503", 1234),
                    (0, json.dumps(payload), "", 1235),
                ]
            )

            results = await retriever.search_async()

            self.assertEqual(results[0]["href"], "https://example.com/recovered")
            self.assertEqual(retriever._run_helper.await_count, 2)

    async def test_codex_specific_result_limit_keeps_multi_entity_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = CodexSearch("multi-entity market evidence")
            retriever.slot_directory = Path(directory) / "slots"
            retriever.retries = 0
            payload = {
                "claims": [],
                "sources": [
                    {
                        "url": f"https://example.com/source-{index}",
                        "title": f"Source {index}",
                        "summary": "verified evidence",
                    }
                    for index in range(10)
                ],
                "caveats": [],
            }
            retriever._run_helper = AsyncMock(
                return_value=(0, json.dumps(payload), "", 1234)
            )

            with patch.dict(os.environ, {"CODEX_SEARCH_MAX_RESULTS": "7"}):
                results = await retriever.search_async(max_results=2)

            self.assertEqual(len(results), 7)

    async def test_global_slot_pool_caps_twelve_calls_at_nine(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {"active": 0, "peak": 0}

            async def use_slot():
                async with _GlobalCodexSlot(limit=9, directory=Path(directory)):
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                    await asyncio.sleep(0.08)
                    state["active"] -= 1

            await asyncio.gather(*(use_slot() for _ in range(12)))

            self.assertEqual(state["peak"], 9)

    async def test_global_slot_is_exclusive_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            slot_directory = Path(directory) / "slots"
            acquired_marker = Path(directory) / "child-acquired"
            script = textwrap.dedent(
                """
                import asyncio
                import sys
                from pathlib import Path
                from gpt_researcher.retrievers.codex.codex import _GlobalCodexSlot

                async def main():
                    async with _GlobalCodexSlot(limit=1, directory=Path(sys.argv[1])):
                        Path(sys.argv[2]).write_text("acquired", encoding="utf-8")

                asyncio.run(main())
                """
            )

            async with _GlobalCodexSlot(limit=1, directory=slot_directory):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    script,
                    str(slot_directory),
                    str(acquired_marker),
                    cwd=str(Path(__file__).resolve().parents[1]),
                )
                await asyncio.sleep(0.2)
                self.assertFalse(acquired_marker.exists())

            await asyncio.wait_for(process.wait(), timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertTrue(acquired_marker.exists())

    async def test_helper_reports_inner_codex_pid_and_slot_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import json
                    import sys
                    import time
                    from pathlib import Path

                    args = sys.argv[1:]
                    output = Path(args[args.index("--output-last-message") + 1])
                    output.write_text(json.dumps({
                        "claims": [{
                            "claim": "Verified fact",
                            "value": 1,
                            "unit": "point",
                            "as_of_date": "2026-07-09",
                            "source_urls": ["https://example.com/fact"],
                            "summary": "source-backed",
                        }],
                        "sources": [{
                            "url": "https://example.com/fact",
                            "title": "Example",
                            "summary": "source-backed",
                        }],
                        "caveats": [],
                    }))
                    time.sleep(0.1)
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            retriever = CodexSearch("telemetry")
            retriever.slot_directory = root / "slots"
            retriever.retries = 0
            with patch.dict(
                os.environ,
                {
                    "CODEX_SEARCH_CODEX_BIN": str(fake_codex),
                    "CODEX_SEARCH_USE_USER_CONFIG": "false",
                },
            ):
                results = await retriever.search_async()

            self.assertEqual(results[0]["href"], "https://example.com/fact")
            metadata = retriever.last_run_metadata
            self.assertIsInstance(metadata["helper_pid"], int)
            self.assertIsInstance(metadata["codex_pid"], int)
            self.assertNotEqual(metadata["helper_pid"], metadata["codex_pid"])
            self.assertIsNotNone(metadata["slot_acquired_at"])
            self.assertIsNotNone(metadata["slot_released_at"])
            self.assertIsNotNone(metadata["codex_started_at"])
            self.assertIsNotNone(metadata["codex_finished_at"])

    async def test_helper_timeout_terminates_inner_codex_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_path = root / "inner-child.pid"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import subprocess
                    import sys
                    import time
                    from pathlib import Path

                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    Path(__PID_FILE__).write_text(str(child.pid), encoding="utf-8")
                    time.sleep(60)
                    """
                )
                .replace("__PID_FILE__", repr(str(child_pid_path)))
                .lstrip(),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            retriever = CodexSearch("timeout")
            retriever.slot_directory = root / "slots"
            retriever.timeout = 1
            retriever.retries = 0

            with patch.dict(
                os.environ,
                {"CODEX_SEARCH_CODEX_BIN": str(fake_codex)},
            ):
                results = await retriever.search_async()

            self.assertEqual(results, [])
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("Inner Codex child survived helper timeout")

    async def test_cancellation_terminates_helper_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_path = root / "child.pid"
            helper = root / "fake_helper.py"
            helper.write_text(
                textwrap.dedent(
                    """
                    import subprocess
                    import sys
                    import time
                    from pathlib import Path

                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    Path(__PID_FILE__).write_text(str(child.pid))
                    time.sleep(60)
                    """
                ).replace("__PID_FILE__", repr(str(child_pid_path))),
                encoding="utf-8",
            )
            retriever = CodexSearch("cancel me")
            retriever.helper_path = helper
            retriever.slot_directory = root / "slots"
            retriever.retries = 0

            task = asyncio.create_task(retriever.search_async())
            for _ in range(100):
                if child_pid_path.exists():
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual(retriever.last_run_metadata["status"], "cancelled")
            self.assertIsNotNone(retriever.last_run_metadata["pid"])
            self.assertEqual(retriever.last_run_metadata["slot"], 0)

            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("Codex helper child survived cancellation")


if __name__ == "__main__":
    unittest.main()
