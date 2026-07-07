import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpt_researcher import mcp_profile_server


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
                        "RETRIEVER": "tavily,codex",
                    },
                    clear=False,
                ),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    asyncio.run(mcp_profile_server.research_report("market query"))

            payload = json.loads(str(cm.exception))

            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["report_type"], "research_report")
            self.assertEqual(payload["sources_count"], 0)
            self.assertEqual(payload["visited_urls_count"], 1)
            self.assertEqual(payload["context_chars"], 0)
            self.assertTrue(payload["fallback_used"])
            self.assertEqual([attempt["status"] for attempt in payload["attempts"]], ["invalid", "invalid"])
            self.assertEqual(
                [attempt["retriever"] for attempt in payload["attempts"]],
                ["tavily,codex", "tavily"],
            )

            failure_path = output_dir / "market query.failed.json"
            self.assertTrue(failure_path.exists())
            saved_payload = json.loads(failure_path.read_text())
            self.assertEqual(saved_payload["status"], "failed")

            self.assertEqual(list(output_dir.glob("*.md")), [])

    def test_research_report_structures_fallback_errors(self):
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
                        "RETRIEVER": "tavily,codex",
                    },
                    clear=False,
                ),
            ):
                with self.assertRaises(RuntimeError) as cm:
                    asyncio.run(mcp_profile_server.research_report("broken query"))

            payload = json.loads(str(cm.exception))

            self.assertEqual(payload["status"], "failed")
            self.assertEqual([attempt["status"] for attempt in payload["attempts"]], ["error", "error"])
            self.assertIn("ValueError: retriever exploded", payload["attempts"][0]["reason"])
            self.assertTrue((output_dir / "broken query.failed.json").exists())

    def test_research_report_start_and_status_return_failed_job(self):
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
                        "RETRIEVER": "tavily,codex",
                    },
                    clear=False,
                ),
            ):
                async def run_job():
                    started = await mcp_profile_server.research_report_start("async market query")
                    self.assertEqual(started["status"], "running")
                    self.assertIn(started["job_id"], mcp_profile_server.RESEARCH_JOBS)
                    await asyncio.sleep(0)
                    return await mcp_profile_server.research_report_status(started["job_id"])

                status = asyncio.run(run_job())

            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["failure"]["status"], "failed")
            self.assertEqual(status["failure"]["sources_count"], 0)
            self.assertEqual(status["failure"]["visited_urls_count"], 1)
            self.assertTrue((output_dir / "async market query.failed.json").exists())


if __name__ == "__main__":
    unittest.main()
