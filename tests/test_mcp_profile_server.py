import asyncio
import inspect
import json
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
        self.evidence_items = []
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
    def test_mcp_exposes_one_blocking_research_tool(self):
        self.assertEqual(
            set(mcp_profile_server.mcp._tool_manager._tools), {"research_report"}
        )
        signature = inspect.signature(mcp_profile_server.research_report)
        self.assertEqual(list(signature.parameters), ["query"])

    def test_writer_catalog_is_domain_neutral_deduplicated_and_bounded(self):
        first = EvidenceItem(
            claim="Revenue increased",
            value=12.5,
            unit="percent",
            as_of_date="2026-07-10",
            source_url="https://example.com/filing",
            source_title="Company filing",
            retriever="PrimarySource",
            summary="Audited year-over-year result",
        )
        second = EvidenceItem(
            claim="A separate risk was disclosed",
            source_url="https://example.com/risk",
            source_title="Risk filing",
            retriever="PrimarySource",
            summary="A material risk disclosure",
        )
        researcher = SimpleNamespace(evidence_items=[first, first, second])

        catalog = json.loads(
            mcp_profile_server._writer_evidence_catalog(researcher, max_chars=10_000)
        )
        bounded = json.loads(
            mcp_profile_server._writer_evidence_catalog(researcher, max_chars=10)
        )

        self.assertEqual(len(catalog["evidence"]), 2)
        self.assertEqual(
            {item["source_url"] for item in catalog["evidence"]},
            {"https://example.com/filing", "https://example.com/risk"},
        )
        self.assertEqual(bounded, {"evidence": []})

    def test_report_validation_rejects_unretrieved_urls_without_rewriting(self):
        researcher = SimpleNamespace(
            context="grounded context " * 300,
            visited_urls={"https://example.com/filing"},
            evidence_items=[
                EvidenceItem(
                    claim="Supported claim",
                    source_url="https://example.com/filing",
                )
            ],
        )
        supported = "Supported [filing](https://example.com/filing)."
        unsupported = supported + " Invented https://example.com/not-retrieved."

        with patch.dict(
            "os.environ",
            {
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "1",
                "MCP_RESEARCH_MIN_CONTEXT_CHARS": "10",
            },
            clear=False,
        ):
            self.assertIsNone(
                mcp_profile_server._invalid_report_reason(supported, researcher)
            )
            reason = mcp_profile_server._invalid_report_reason(unsupported, researcher)

        self.assertIn("absent from retrieved evidence", reason)
        self.assertIn("https://example.com/not-retrieved", unsupported)

    def test_report_metrics_do_not_count_visited_urls_as_sources(self):
        metrics = mcp_profile_server._report_metrics(FakeResearcher())

        self.assertEqual(metrics["sources_count"], 0)
        self.assertEqual(metrics["context_chunks_count"], 0)
        self.assertEqual(metrics["context_chars"], 0)
        self.assertEqual(metrics["visited_urls_count"], 1)
        self.assertEqual(metrics["http_sources_count"], 0)

    def test_evidence_gate_does_not_require_a_concurrency_shape(self):
        researcher = SimpleNamespace(
            context="grounded context " * 30,
            research_work_items=[SimpleNamespace(query="one lane")],
            evidence_items=[
                EvidenceItem(claim="First", source_url="https://example.com/one"),
                EvidenceItem(claim="Second", source_url="https://example.com/two"),
            ],
            evidence_metrics={
                "minimum_http_sources_per_work_item": 1,
                "per_work_item_http_sources": {"1": 2},
                "codex_initial_calls": 1,
                "active_codex_peak": 1,
            },
        )

        with patch.dict(
            "os.environ",
            {
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "2",
                "MCP_RESEARCH_MIN_CONTEXT_CHARS": "10",
                "RETRIEVER": "tavily,codex",
            },
            clear=False,
        ):
            reason = mcp_profile_server._invalid_evidence_reason(researcher)

        self.assertIsNone(reason)

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
        self.assertIn("http_sources_count: 0", markdown)
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
                        "MCP_RESEARCH_RETRIEVAL_ATTEMPTS": "1",
                        "MCP_RESEARCH_FALLBACK_RETRIEVER": "",
                        "RETRIEVER": "tavily,codex",
                    },
                    clear=False,
                ),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    asyncio.run(
                        mcp_profile_server._run_research_report(
                            "investigate a generic topic"
                        )
                    )

            payload = json.loads(str(cm.exception))
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["sources_count"], 0)
            self.assertEqual(payload["http_sources_count"], 0)
            self.assertFalse(payload["fallback_used"])
            self.assertEqual(
                [attempt["stage"] for attempt in payload["attempts"]],
                ["retrieval"],
            )
            self.assertTrue(
                (output_dir / "investigate a generic topic.failed.json").exists()
            )
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
            self.assertEqual(
                [attempt["status"] for attempt in payload["attempts"]], ["error"]
            )
            self.assertIn(
                "ValueError: retriever exploded", payload["attempts"][0]["reason"]
            )
            self.assertTrue((output_dir / "broken query.failed.json").exists())

    def test_research_report_waits_for_isolated_worker_and_returns_full_result(self):
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
                    result = await mcp_profile_server.research_report(
                        "investigate a generic topic"
                    )
                await manager.shutdown()
                return result

            result = asyncio.run(run_job())

            self.assertEqual(result["http_sources_count"], 25)
            self.assertEqual(result["report"], "report for investigate a generic topic")

    def test_independent_research_report_calls_can_run_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = root / "barrier_worker.py"
            marker = '(args.job_dir / "started").write_text(str(os.getpid()))'
            barrier = """
deadline = time.time() + 2
while len(list(args.job_dir.parent.glob("*/started"))) < 2:
    if time.time() >= deadline:
        raise SystemExit("second worker did not start concurrently")
    time.sleep(0.01)
"""
            worker.write_text(
                textwrap.dedent(FAKE_WORKER).replace(marker, marker + barrier),
                encoding="utf-8",
            )

            async def run_reports():
                manager = JobManager(
                    root / "jobs",
                    worker_command=(__import__("sys").executable, str(worker)),
                    max_concurrent_jobs=2,
                    timeout_seconds=5,
                )
                try:
                    with patch.object(mcp_profile_server, "_JOB_MANAGER", manager):
                        return await asyncio.gather(
                            mcp_profile_server.research_report("first question"),
                            mcp_profile_server.research_report("second question"),
                        )
                finally:
                    await manager.shutdown()

            results = asyncio.run(run_reports())
            self.assertEqual(
                [result["report"] for result in results],
                ["report for first question", "report for second question"],
            )


if __name__ == "__main__":
    unittest.main()
