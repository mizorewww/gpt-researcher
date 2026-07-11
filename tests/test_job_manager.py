import asyncio
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gpt_researcher.job_manager import JobManager, JobQueueFullError, atomic_write_json
from gpt_researcher.mcp_research_worker import _redact


FAKE_WORKER = r"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--job-dir", type=Path, required=True)
args = parser.parse_args()
spec = json.loads((args.job_dir / "spec.json").read_text())
(args.job_dir / "started").write_text(str(os.getpid()))
if spec["query"] == "invalid result":
    (args.job_dir / "result.json").write_text("not-json")
    raise SystemExit(0)
if spec["query"] == "orphan":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (args.job_dir / "child_pid").write_text(str(child.pid))
if spec["query"] == "large stderr":
    print("x" * 10000, file=sys.stderr, flush=True)
    print("stdout-is-isolated", flush=True)
if spec["query"] == "hang":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    (args.job_dir / "child_pid").write_text(str(child.pid))
    time.sleep(60)
delay = float(spec.get("fake_delay", 0.05))
time.sleep(delay)
(args.job_dir / "worker_status.json").write_text(json.dumps({
    "phase": "writer", "progress": {"completed": 3, "total": 3}, "active_codex": 0
}))
report = args.job_dir / "report.md"
report.write_text("report for " + spec["query"])
(args.job_dir / "result.json").write_text(json.dumps({
    "status": "completed",
    "result": {
        "path": str(report),
        "report": report.read_text(),
        "http_sources_count": 25,
        "work_item_count": 3,
        "codex_initial_calls": 3,
        "active_codex_peak": 3,
        "quality_gate_passed": True,
        "total_cost_usd": 0.1
    }
}))
"""


class TestJobManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worker = self.root / "fake_worker.py"
        self.worker.write_text(textwrap.dedent(FAKE_WORKER), encoding="utf-8")
        self.managers: list[JobManager] = []

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.shutdown()
        self.temporary.cleanup()

    def manager(self, **kwargs):
        manager = JobManager(
            self.root / "jobs",
            worker_command=(sys.executable, str(self.worker)),
            terminate_grace_seconds=0.2,
            **kwargs,
        )
        self.managers.append(manager)
        return manager

    async def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.02)
        self.fail("condition was not reached before timeout")

    async def test_default_budget_and_retention(self):
        manager = self.manager()
        self.assertEqual(manager.timeout_seconds, 2700)
        self.assertEqual(manager.retention_seconds, 72 * 3600)

    async def test_worker_redaction_preserves_sk_hynix_urls_and_removes_keys(self):
        url = "https://example.com/news/sk-hynix-seeks-us-listing-in-2026"
        self.assertEqual(_redact(url), url)
        fake_key = "sk-" + "a" * 32
        redacted = _redact(f"Authorization: Bearer {fake_key}")
        self.assertNotIn(fake_key, redacted)
        self.assertIn("[REDACTED]", redacted)

    async def test_worker_environment_preserves_profile_without_unrelated_state(self):
        manager = self.manager()
        job_id = "00000000-0000-4000-8000-000000000010"
        with patch.dict(
            os.environ,
            {
                "LANGUAGE": "chinese",
                "TAVILY_SEARCH_DEPTH": "advanced",
                "SEARCH_RETRIEVER_CONCURRENCY": "4",
                "UNRELATED_PRIVATE_VALUE": "must-not-leak",
            },
            clear=False,
        ):
            environment = manager._subprocess_env(job_id)
        self.assertEqual(environment["LANGUAGE"], "chinese")
        self.assertEqual(environment["TAVILY_SEARCH_DEPTH"], "advanced")
        self.assertEqual(environment["SEARCH_RETRIEVER_CONCURRENCY"], "4")
        self.assertNotIn("UNRELATED_PRIVATE_VALUE", environment)

    async def test_three_workers_run_while_nine_are_queued(self):
        manager = self.manager(
            max_concurrent_jobs=3,
            max_queued_jobs=9,
            timeout_seconds=5,
        )
        jobs = [
            await manager.submit({"query": f"job-{index}", "fake_delay": 0.3})
            for index in range(12)
        ]
        with self.assertRaises(JobQueueFullError):
            await manager.submit({"query": "overflow"})

        await self.wait_for(
            lambda: sum(manager.compact_status(item["job_id"])["status"] == "running" for item in jobs)
            == 3
        )
        statuses = [manager.compact_status(item["job_id"])["status"] for item in jobs]
        self.assertEqual(statuses.count("running"), 3)
        self.assertEqual(statuses.count("queued"), 9)

        await self.wait_for(
            lambda: all(
                manager.compact_status(item["job_id"])["status"] == "completed"
                for item in jobs
            ),
            timeout=6,
        )

    async def test_compact_status_bulk_wait_and_report_redaction(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=5,
        )
        first = await manager.submit({"query": "same title", "fake_delay": 0.1})
        second = await manager.submit({"query": "same title", "fake_delay": 0.1})
        statuses = await manager.wait_many([first["job_id"], second["job_id"]], 1)
        self.assertEqual(len(statuses), 2)
        self.assertNotEqual(
            statuses[0]["artifacts"]["job_dir"], statuses[1]["artifacts"]["job_dir"]
        )
        await self.wait_for(
            lambda: manager.compact_status(second["job_id"])["status"] == "completed"
        )
        compact = manager.compact_status(second["job_id"])
        self.assertNotIn("result", compact)
        result = manager.result(second["job_id"])
        self.assertNotIn("report", result["result"])
        self.assertEqual(result["http_sources_count"], 25)
        self.assertEqual(result["work_item_count"], 3)
        self.assertTrue(Path(result["manifest_path"]).exists())
        expanded = manager.result(second["job_id"], include_report=True)
        self.assertIn("report", expanded["result"])
        for artifact in ("spec", "status", "events", "result", "manifest", "report"):
            self.assertTrue(Path(compact["artifacts"][artifact]).exists())

    async def test_cancel_terminates_worker_process_group(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=30,
        )
        submitted = await manager.submit({"query": "hang"})
        job_dir = Path(submitted["artifacts"]["job_dir"])
        await self.wait_for(lambda: (job_dir / "child_pid").exists())
        child_pid = int((job_dir / "child_pid").read_text())
        atomic_write_json(
            job_dir / "worker_status.json",
            {"phase": "retrieval", "active_codex": 1},
        )
        cancelled = await manager.cancel(submitted["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["phase"], "cancelled")
        self.assertIn("cancelled", manager.result(submitted["job_id"])["error"])
        await self.wait_for(lambda: not _process_is_live(child_pid))

    async def test_timeout_terminates_nested_session_descendants(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=0.2,
        )
        submitted = await manager.submit({"query": "hang"})
        job_dir = Path(submitted["artifacts"]["job_dir"])
        await self.wait_for(lambda: (job_dir / "child_pid").exists())
        child_pid = int((job_dir / "child_pid").read_text())
        await self.wait_for(
            lambda: manager.compact_status(submitted["job_id"])["status"]
            == "timed_out",
            timeout=3,
        )
        await self.wait_for(lambda: not _process_is_live(child_pid))
        self.assertIn("exceeded", manager.result(submitted["job_id"])["error"])

    async def test_job_budget_includes_time_waiting_in_queue(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=0.25,
        )
        first = await manager.submit({"query": "first budget", "fake_delay": 1})
        second = await manager.submit({"query": "queued budget", "fake_delay": 0.05})
        await self.wait_for(
            lambda: manager.compact_status(first["job_id"])["status"] == "timed_out"
        )
        await self.wait_for(
            lambda: manager.compact_status(second["job_id"])["status"] == "timed_out"
        )
        # Even if the semaphore releases in the same event-loop tick as the
        # deadline, the second job must not receive a fresh execution budget.
        self.assertLess(
            manager.compact_status(second["job_id"])["elapsed_seconds"], 0.75
        )

    async def test_zero_exit_with_corrupt_result_fails_closed(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=5,
        )
        submitted = await manager.submit({"query": "invalid result"})
        await self.wait_for(
            lambda: manager.compact_status(submitted["job_id"])["status"] == "failed"
        )
        result = manager.result(submitted["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("invalid or incomplete result", result["error"])

    async def test_normal_worker_exit_cleans_residual_process_group(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=5,
        )
        submitted = await manager.submit({"query": "orphan"})
        job_dir = Path(submitted["artifacts"]["job_dir"])
        await self.wait_for(lambda: (job_dir / "child_pid").exists())
        child_pid = int((job_dir / "child_pid").read_text())
        await self.wait_for(
            lambda: manager.compact_status(submitted["job_id"])["status"] == "completed"
        )
        await self.wait_for(lambda: not _process_is_live(child_pid))
        status = manager._status_unchecked(submitted["job_id"])
        self.assertIn(child_pid, status["residual_processes_terminated"])

    async def test_cancel_during_spawn_race_finishes_cancelled(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=5,
        )
        original_spawn = asyncio.create_subprocess_exec
        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            spawn_entered.set()
            await release_spawn.wait()
            return await original_spawn(*args, **kwargs)

        with patch(
            "gpt_researcher.job_manager.asyncio.create_subprocess_exec",
            new=delayed_spawn,
        ):
            submitted = await manager.submit({"query": "spawn race", "fake_delay": 1})
            await spawn_entered.wait()
            cancellation = asyncio.create_task(manager.cancel(submitted["job_id"]))
            await asyncio.sleep(0)
            release_spawn.set()
            cancelled = await cancellation

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["phase"], "cancelled")

    async def test_shutdown_during_spawn_does_not_lose_worker_process(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=5,
        )
        original_spawn = asyncio.create_subprocess_exec
        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()
        spawned = []

        async def delayed_spawn(*args, **kwargs):
            spawn_entered.set()
            await release_spawn.wait()
            process = await original_spawn(*args, **kwargs)
            spawned.append(process)
            return process

        with patch(
            "gpt_researcher.job_manager.asyncio.create_subprocess_exec",
            new=delayed_spawn,
        ):
            submitted = await manager.submit({"query": "hang"})
            await spawn_entered.wait()
            shutdown = asyncio.create_task(manager.shutdown())
            await asyncio.sleep(0)
            release_spawn.set()
            await shutdown

        self.assertEqual(
            manager.compact_status(submitted["job_id"])["status"], "interrupted"
        )
        self.assertEqual(len(spawned), 1)
        await self.wait_for(lambda: not _process_is_live(spawned[0].pid))

    async def test_worker_stdout_isolated_and_stderr_finalize_is_bounded(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=5,
        )
        with patch.dict(os.environ, {"MCP_RESEARCH_MAX_STDERR_BYTES": "1024"}):
            submitted = await manager.submit({"query": "large stderr"})
            await self.wait_for(
                lambda: manager.compact_status(submitted["job_id"])["status"]
                == "completed"
            )
        stderr_path = Path(
            manager.compact_status(submitted["job_id"])["artifacts"]["stderr"]
        )
        stderr = stderr_path.read_text(encoding="utf-8")
        self.assertIn("stderr bytes omitted", stderr)
        self.assertIn("stdout-is-isolated", stderr)
        self.assertLess(len(stderr.encode("utf-8")), 1400)

    async def test_restart_kills_owned_orphan_worker_and_nested_child(self):
        job_id = "00000000-0000-4000-8000-000000000009"
        job_dir = self.root / "jobs" / job_id
        job_dir.mkdir(parents=True)
        atomic_write_json(job_dir / "spec.json", {"query": "hang", "job_id": job_id})
        orphan = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self.worker),
            "--job-dir",
            str(job_dir),
            start_new_session=True,
        )
        await self.wait_for(lambda: (job_dir / "child_pid").exists())
        child_pid = int((job_dir / "child_pid").read_text())
        atomic_write_json(
            job_dir / "status.json",
            {
                "job_id": job_id,
                "status": "running",
                "worker_pid": orphan.pid,
                "created_at_epoch": time.time(),
            },
        )

        recovered = JobManager(
            self.root / "jobs",
            retention_hours=1,
            worker_command=(sys.executable, str(self.worker)),
            terminate_grace_seconds=0.2,
        )
        self.managers.append(recovered)

        self.assertEqual(recovered.compact_status(job_id)["status"], "interrupted")
        cleanup = recovered._status_unchecked(job_id)["orphan_cleanup"]
        self.assertTrue(cleanup["attempted"])
        self.assertTrue(cleanup["terminated"])
        await self.wait_for(lambda: not _process_is_live(orphan.pid))
        await self.wait_for(lambda: not _process_is_live(child_pid))

    async def test_restart_marks_active_jobs_interrupted_and_ttl_cleans(self):
        manager = self.manager(
            max_concurrent_jobs=1,
            max_queued_jobs=1,
            timeout_seconds=30,
        )
        submitted = await manager.submit({"query": "hang"})
        await self.wait_for(
            lambda: manager.compact_status(submitted["job_id"])["status"] == "running"
        )
        await manager.shutdown()
        self.assertEqual(manager.compact_status(submitted["job_id"])["status"], "interrupted")

        old_id = "00000000-0000-4000-8000-000000000001"
        old_dir = self.root / "jobs" / old_id
        old_dir.mkdir()
        atomic_write_json(
            old_dir / "status.json",
            {
                "job_id": old_id,
                "status": "completed",
                "finished_at_epoch": time.time() - 7200,
            },
        )
        legacy_id = "00000000-0000-4000-8000-000000000002"
        legacy_dir = self.root / "jobs" / legacy_id
        legacy_dir.mkdir()
        atomic_write_json(
            legacy_dir / "status.json",
            {"job_id": legacy_id, "status": "failed"},
        )
        old_timestamp = time.time() - 7200
        os.utime(legacy_dir / "status.json", (old_timestamp, old_timestamp))
        recovered = JobManager(
            self.root / "jobs",
            retention_hours=1,
            worker_command=(sys.executable, str(self.worker)),
        )
        self.managers.append(recovered)
        self.assertFalse(old_dir.exists())
        self.assertFalse(legacy_dir.exists())
        self.assertEqual(
            recovered.compact_status(submitted["job_id"])["status"], "interrupted"
        )

    async def test_restart_requeues_durable_queued_job(self):
        job_id = "00000000-0000-4000-8000-000000000008"
        job_dir = self.root / "jobs" / job_id
        job_dir.mkdir(parents=True)
        atomic_write_json(
            job_dir / "spec.json",
            {"query": "recovered queued", "job_id": job_id, "fake_delay": 0.05},
        )
        atomic_write_json(
            job_dir / "status.json",
            {
                "job_id": job_id,
                "status": "queued",
                "created_at": "2026-07-10T00:00:00+00:00",
                "created_at_epoch": time.time(),
                "timeout_seconds": 5,
            },
        )
        atomic_write_json(job_dir / "events.json", [])
        atomic_write_json(job_dir / "manifest.json", {"version": 1, "job_id": job_id})
        recovered = JobManager(
            self.root / "jobs",
            worker_command=(sys.executable, str(self.worker)),
            timeout_seconds=5,
        )
        self.managers.append(recovered)

        await recovered.wait_status(job_id, 1)
        await self.wait_for(
            lambda: recovered.compact_status(job_id)["status"] == "completed"
        )

    async def test_bulk_wait_does_not_spin_when_one_job_is_already_terminal(self):
        manager = self.manager(
            max_concurrent_jobs=2,
            max_queued_jobs=2,
            timeout_seconds=5,
        )
        quick = await manager.submit({"query": "quick", "fake_delay": 0.05})
        slow = await manager.submit({"query": "slow", "fake_delay": 0.6})
        await self.wait_for(
            lambda: manager.compact_status(quick["job_id"])["status"] == "completed"
        )
        await self.wait_for(
            lambda: manager.compact_status(slow["job_id"])["status"] == "running"
        )
        started = time.monotonic()
        await manager.wait_many([quick["job_id"], slow["job_id"]], wait_seconds=0.2)
        self.assertGreaterEqual(time.monotonic() - started, 0.15)


def _process_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        import psutil

        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (ImportError, psutil.Error):
        return True


if __name__ == "__main__":
    unittest.main()
