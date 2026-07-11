"""Persistent subprocess scheduler for MCP research jobs.

The MCP process is deliberately kept small: long-running research happens in a
new process group per job, while this module owns queueing, cancellation and
the durable job state exposed by the MCP tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil


TERMINAL_STATUSES = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "interrupted",
}
ACTIVE_STATUSES = {"queued", "running"}


class JobQueueFullError(RuntimeError):
    """Raised when the configured pending-job capacity has been reached."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_text(path: Path, text: str) -> None:
    """Replace *path* atomically with UTF-8 text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


class JobManager:
    """Queue and supervise isolated research worker subprocesses."""

    def __init__(
        self,
        jobs_dir: Path,
        *,
        max_concurrent_jobs: int = 3,
        max_queued_jobs: int = 9,
        timeout_seconds: float = 2700,
        retention_hours: float = 72,
        worker_command: Sequence[str] | None = None,
        worker_env: Mapping[str, str] | None = None,
        terminate_grace_seconds: float = 5,
    ) -> None:
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be at least 1")
        if max_queued_jobs < 0:
            raise ValueError("max_queued_jobs cannot be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.jobs_dir = Path(jobs_dir).expanduser().resolve()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.max_concurrent_jobs = max_concurrent_jobs
        self.max_queued_jobs = max_queued_jobs
        self.timeout_seconds = timeout_seconds
        self.retention_seconds = max(0.0, retention_hours * 3600)
        self.worker_command = tuple(
            worker_command
            or (sys.executable, "-m", "gpt_researcher.mcp_research_worker")
        )
        self.worker_env = dict(worker_env or {})
        self.terminate_grace_seconds = terminate_grace_seconds

        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancel_requested: set[str] = set()
        self._recovered_queued: set[str] = set()
        self._recover_and_cleanup()

    def _job_dir(self, job_id: str) -> Path:
        # UUID validation also prevents path traversal through public MCP input.
        try:
            normalized = str(__import__("uuid").UUID(job_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("invalid research job id") from exc
        return self.jobs_dir / normalized

    @staticmethod
    def _paths(job_dir: Path) -> dict[str, Path]:
        return {
            "spec": job_dir / "spec.json",
            "status": job_dir / "status.json",
            "events": job_dir / "events.json",
            "result": job_dir / "result.json",
            "stderr": job_dir / "stderr.log",
            "stderr_pending": job_dir / ".stderr.pending",
            "worker_status": job_dir / "worker_status.json",
            "manifest": job_dir / "manifest.json",
        }

    def _finalize_stderr(self, job_id: str) -> None:
        paths = self._paths(self._job_dir(job_id))
        pending = paths["stderr_pending"]
        if not pending.exists():
            return
        try:
            try:
                configured_max = int(
                    os.getenv("MCP_RESEARCH_MAX_STDERR_BYTES", str(10 * 1024 * 1024))
                )
            except ValueError:
                configured_max = 10 * 1024 * 1024
            max_bytes = max(1024, configured_max)
            size = pending.stat().st_size
            with pending.open("rb") as handle:
                if size <= max_bytes:
                    raw = handle.read()
                else:
                    head_size = max_bytes // 2
                    tail_size = max_bytes - head_size
                    head = handle.read(head_size)
                    handle.seek(-tail_size, os.SEEK_END)
                    tail = handle.read(tail_size)
                    omitted = size - len(head) - len(tail)
                    raw = (
                        head
                        + f"\n...[{omitted} stderr bytes omitted]...\n".encode()
                        + tail
                    )
            text = raw.decode("utf-8", errors="replace")
        except OSError:
            pending.unlink(missing_ok=True)
            return
        secret_env = {**os.environ, **self.worker_env}
        for key, secret in secret_env.items():
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
        atomic_write_text(paths["stderr"], text)
        pending.unlink(missing_ok=True)

    def _redact_error(self, value: object) -> str:
        text = str(value)
        for key, secret in {**os.environ, **self.worker_env}.items():
            if len(secret) >= 8 and any(
                marker in key.upper()
                for marker in ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
            ):
                text = text.replace(secret, "[REDACTED]")
        return re.sub(
            r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+",
            r"\1[REDACTED]",
            text,
        )[:4000]

    def _recover_and_cleanup(self) -> None:
        """Recover durable state before accepting new work."""
        now = time.time()
        for job_dir in self.jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue
            paths = self._paths(job_dir)
            status = read_json(paths["status"], {})
            if not isinstance(status, dict):
                continue
            state = status.get("status")
            if state == "queued":
                try:
                    self._recovered_queued.add(str(__import__("uuid").UUID(job_dir.name)))
                except ValueError:
                    pass
                continue
            if state == "running":
                cleanup = self._terminate_recovered_worker(job_dir, status)
                status.update(
                    {
                        "status": "interrupted",
                        "phase": "interrupted",
                        "finished_at": utc_now(),
                        "finished_at_epoch": now,
                        "updated_at": utc_now(),
                        "updated_at_epoch": now,
                        "error": "coordinator restarted while the job was active",
                        "orphan_cleanup": cleanup,
                    }
                )
                atomic_write_json(paths["status"], status)
                events = read_json(paths["events"], [])
                if not isinstance(events, list):
                    events = []
                events.append(
                    {
                        "at": utc_now(),
                        "event": "interrupted",
                        "reason": "coordinator restart",
                        "orphan_cleanup": cleanup,
                    }
                )
                atomic_write_json(paths["events"], events)
                manifest = read_json(paths["manifest"], {})
                if not isinstance(manifest, dict):
                    manifest = {"version": 1, "job_id": job_dir.name}
                manifest.update(
                    {
                        "status": "interrupted",
                        "finished_at": status["finished_at"],
                        "orphan_cleanup": cleanup,
                    }
                )
                atomic_write_json(paths["manifest"], manifest)
                pending = paths["stderr_pending"]
                if pending.exists():
                    self._finalize_stderr(job_dir.name)
                continue

            finished = self._finished_epoch(status, paths["status"])
            if (
                state in TERMINAL_STATUSES
                and self.retention_seconds >= 0
                and finished
                and now - finished > self.retention_seconds
            ):
                shutil.rmtree(job_dir, ignore_errors=True)

    def _ensure_recovered_tasks(self) -> None:
        """Re-enqueue durable queued jobs once an event loop is available."""

        for job_id in list(self._recovered_queued):
            if job_id in self._tasks:
                self._recovered_queued.discard(job_id)
                continue
            status = self._status_unchecked(job_id)
            if not status or status.get("status") != "queued":
                self._recovered_queued.discard(job_id)
                continue
            task = asyncio.create_task(self._execute(job_id), name=f"research-job-{job_id}")
            self._tasks[job_id] = task
            task.add_done_callback(lambda _task, jid=job_id: self._tasks.pop(jid, None))
            self._recovered_queued.discard(job_id)

    def _terminate_recovered_worker(
        self, job_dir: Path, status: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Safely terminate a worker left behind by a dead coordinator."""

        try:
            pid = int(status.get("worker_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0:
            return {"attempted": False, "reason": "no worker pid"}

        try:
            root = psutil.Process(pid)
            command = root.cmdline()
        except psutil.NoSuchProcess:
            return {"attempted": True, "terminated": True, "reason": "already exited"}
        except (psutil.AccessDenied, OSError) as exc:
            return {
                "attempted": False,
                "terminated": False,
                "reason": f"unable to inspect worker: {type(exc).__name__}",
            }

        # PID reuse must never let recovery signal an unrelated process. Resolve
        # the explicit --job-dir argument so macOS /var and /private/var aliases
        # compare correctly.
        owned = False
        for index, value in enumerate(command[:-1]):
            if value != "--job-dir":
                continue
            try:
                owned = Path(command[index + 1]).resolve() == job_dir.resolve()
            except OSError:
                owned = False
            break
        if not owned:
            return {
                "attempted": False,
                "terminated": False,
                "reason": "worker pid ownership check failed",
            }

        descendants = root.children(recursive=True)
        tracked = [root, *descendants]
        try:
            if os.name == "posix" and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                root.terminate()
        except (ProcessLookupError, psutil.NoSuchProcess):
            pass

        _, alive = psutil.wait_procs(
            tracked, timeout=min(max(self.terminate_grace_seconds, 0.1), 2.0)
        )
        for item in alive:
            try:
                item.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            _, alive = psutil.wait_procs(alive, timeout=2.0)
        late_owned = self._processes_owned_by_job(job_dir.name)
        for item in late_owned.values():
            try:
                item.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        late_alive: list[psutil.Process] = []
        if late_owned:
            _, late_alive = psutil.wait_procs(list(late_owned.values()), timeout=2.0)
        survivor_ids = sorted({item.pid for item in alive + late_alive})
        return {
            "attempted": True,
            "terminated": not survivor_ids,
            "worker_pid": pid,
            "descendants": sorted(
                {item.pid for item in descendants} | set(late_owned)
            ),
            "survivors": survivor_ids,
        }

    def cleanup_expired(self) -> int:
        """Delete terminal job directories beyond their retention window."""
        removed = 0
        now = time.time()
        for job_dir in self.jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue
            status_path = self._paths(job_dir)["status"]
            status = read_json(status_path, {})
            if not isinstance(status, dict) or status.get("status") not in TERMINAL_STATUSES:
                continue
            finished = self._finished_epoch(status, status_path)
            if finished and now - finished > self.retention_seconds:
                shutil.rmtree(job_dir, ignore_errors=True)
                removed += 1
        return removed

    @staticmethod
    def _finished_epoch(status: Mapping[str, Any], status_path: Path) -> float:
        try:
            explicit = float(status.get("finished_at_epoch") or 0)
        except (TypeError, ValueError):
            explicit = 0
        if explicit:
            return explicit
        try:
            return status_path.stat().st_mtime
        except OSError:
            return 0

    def _status_unchecked(self, job_id: str) -> dict[str, Any] | None:
        try:
            job_dir = self._job_dir(job_id)
        except ValueError:
            return None
        payload = read_json(self._paths(job_dir)["status"])
        return payload if isinstance(payload, dict) else None

    def _append_event(self, job_id: str, event: str, **details: Any) -> None:
        paths = self._paths(self._job_dir(job_id))
        events = read_json(paths["events"], [])
        if not isinstance(events, list):
            events = []
        events.append({"at": utc_now(), "event": event, **details})
        atomic_write_json(paths["events"], events)

    def _write_status(self, job_id: str, **changes: Any) -> dict[str, Any]:
        paths = self._paths(self._job_dir(job_id))
        status = read_json(paths["status"], {})
        if not isinstance(status, dict):
            status = {"job_id": job_id}
        now = time.time()
        status.update(changes)
        status.update({"updated_at": utc_now(), "updated_at_epoch": now})
        atomic_write_json(paths["status"], status)
        return status

    def _write_manifest(self, job_id: str, **changes: Any) -> None:
        path = self._paths(self._job_dir(job_id))["manifest"]
        manifest = read_json(path, {})
        if not isinstance(manifest, dict):
            manifest = {}
        manifest.update(changes)
        atomic_write_json(path, manifest)

    async def submit(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Persist and enqueue one job, returning its initial compact status."""
        self._ensure_recovered_tasks()
        async with self._lock:
            self.cleanup_expired()
            active_count = 0
            for job_dir in self.jobs_dir.iterdir():
                if not job_dir.is_dir():
                    continue
                state = read_json(self._paths(job_dir)["status"], {})
                if isinstance(state, dict) and state.get("status") in ACTIVE_STATUSES:
                    active_count += 1
            total_capacity = self.max_concurrent_jobs + self.max_queued_jobs
            if active_count >= total_capacity:
                raise JobQueueFullError(
                    "research queue is full "
                    f"({self.max_concurrent_jobs} running + {self.max_queued_jobs} queued)"
                )

            job_id = str(uuid4())
            job_dir = self._job_dir(job_id)
            job_dir.mkdir(mode=0o700, parents=True)
            now = time.time()
            normalized_spec = {
                **dict(spec),
                "job_id": job_id,
                "created_at": utc_now(),
                "timeout_seconds": float(spec.get("timeout_seconds") or self.timeout_seconds),
            }
            atomic_write_json(self._paths(job_dir)["spec"], normalized_spec)
            atomic_write_json(
                self._paths(job_dir)["status"],
                {
                    "job_id": job_id,
                    "status": "queued",
                    "phase": "queued",
                    "progress": {"completed": 0, "total": 3},
                    "active_codex": 0,
                    "created_at": normalized_spec["created_at"],
                    "created_at_epoch": now,
                    "updated_at": normalized_spec["created_at"],
                    "updated_at_epoch": now,
                    "started_at": None,
                    "finished_at": None,
                    "timeout_seconds": normalized_spec["timeout_seconds"],
                    "worker_pid": None,
                },
            )
            atomic_write_json(
                self._paths(job_dir)["events"],
                [{"at": normalized_spec["created_at"], "event": "queued"}],
            )
            atomic_write_json(
                self._paths(job_dir)["manifest"],
                {
                    "version": 1,
                    "job_id": job_id,
                    "prompt_sha256": hashlib.sha256(
                        str(normalized_spec.get("query", "")).encode("utf-8")
                    ).hexdigest(),
                    "created_at": normalized_spec["created_at"],
                    "config": {
                        "max_concurrent_jobs": self.max_concurrent_jobs,
                        "max_queued_jobs": self.max_queued_jobs,
                        "timeout_seconds": normalized_spec["timeout_seconds"],
                    },
                    "cost_scope": {
                        "reported_total": "GPT Researcher LLM provider callbacks",
                        "codex_cli": "unavailable from Codex CLI telemetry",
                    },
                },
            )
            task = asyncio.create_task(self._execute(job_id), name=f"research-job-{job_id}")
            self._tasks[job_id] = task
            task.add_done_callback(lambda _task, jid=job_id: self._tasks.pop(jid, None))
            return self.compact_status(job_id)

    def _subprocess_env(self, job_id: str) -> dict[str, str]:
        """Build a bounded worker environment without unrelated shell state."""
        exact = {
            "HOME",
            "PATH",
            "TMPDIR",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "PYTHONPATH",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "CODEX_HOME",
            "XDG_CONFIG_HOME",
            "RETRIEVER",
            "FAST_LLM",
            "SMART_LLM",
            "STRATEGIC_LLM",
            "EMBEDDING",
            "LANGUAGE",
            "TOTAL_WORDS",
            "SMART_TOKEN_LIMIT",
            "MAX_SCRAPER_WORKERS",
        }
        prefixes = (
            "MCP_RESEARCH_",
            "CODEX_SEARCH_",
            "GPT_RESEARCHER_",
            "TAVILY_",
            "RESEARCH_",
            "SEARCH_RETRIEVER_",
            "COMPRESSION_",
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key in exact
            or key.endswith("_API_KEY")
            or key.endswith("_API_BASE")
            or key.startswith(prefixes)
        }
        env.update(self.worker_env)
        env.update(
            {
                "MCP_RESEARCH_JOB_ID": job_id,
                "MCP_RESEARCH_JOB_DIR": str(self._job_dir(job_id)),
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    async def _execute(self, job_id: str) -> None:
        status = self._status_unchecked(job_id) or {}
        budget = float(status.get("timeout_seconds") or self.timeout_seconds)
        created_at = float(status.get("created_at_epoch") or time.time())
        remaining = max(0.001, budget - max(0.0, time.time() - created_at))
        try:
            await asyncio.wait_for(self._execute_within_budget(job_id), timeout=remaining)
        except TimeoutError:
            async with self._lock:
                current = self._status_unchecked(job_id)
                if current and current.get("status") != "cancelled":
                    now = time.time()
                    error = "research job exceeded MCP_RESEARCH_JOB_TIMEOUT"
                    self._write_status(
                        job_id,
                        status="timed_out",
                        phase="timed_out",
                        active_codex=0,
                        finished_at=utc_now(),
                        finished_at_epoch=now,
                        error=error,
                    )
                    self._append_event(job_id, "timed_out", error=error)
                    self._write_manifest(
                        job_id,
                        status="timed_out",
                        finished_at=utc_now(),
                        error=error,
                    )

    async def _execute_within_budget(self, job_id: str) -> None:
        process: asyncio.subprocess.Process | None = None
        try:
            async with self._semaphore:
                async with self._lock:
                    status = self._status_unchecked(job_id)
                    if not status or status.get("status") != "queued":
                        return
                    now = time.time()
                    self._write_status(
                        job_id,
                        status="running",
                        phase="starting",
                        started_at=utc_now(),
                        started_at_epoch=now,
                    )
                    self._append_event(job_id, "started")

                paths = self._paths(self._job_dir(job_id))
                paths["stderr_pending"].unlink(missing_ok=True)
                with paths["stderr_pending"].open("wb") as stderr_handle:
                    spawn_task = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            *self.worker_command,
                            "--job-dir",
                            str(self._job_dir(job_id)),
                            cwd=str(self.jobs_dir.parent),
                            env=self._subprocess_env(job_id),
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=stderr_handle,
                            stderr=stderr_handle,
                            start_new_session=True,
                        )
                    )
                    try:
                        # Shield process creation so coordinator shutdown cannot
                        # lose a successfully spawned worker before its PID is
                        # registered and terminated.
                        process = await asyncio.shield(spawn_task)
                    except asyncio.CancelledError:
                        process = await spawn_task
                        self._processes[job_id] = process
                        await self._terminate_process_group(process)
                        raise
                    self._processes[job_id] = process
                    async with self._lock:
                        self._write_status(job_id, worker_pid=process.pid, phase="research")
                        self._append_event(job_id, "worker_spawned", pid=process.pid)
                        self._write_manifest(
                            job_id,
                            worker_pid=process.pid,
                            started_at=utc_now(),
                        )
                        cancel_before_wait = job_id in self._cancel_requested

                    if cancel_before_wait:
                        await self._terminate_process_group(process)

                    spec = read_json(paths["spec"], {})
                    timeout = float(spec.get("timeout_seconds") or self.timeout_seconds)
                    timed_out = False
                    try:
                        await asyncio.wait_for(process.wait(), timeout=timeout)
                    except TimeoutError:
                        timed_out = True
                        await self._terminate_process_group(process)

                    residual_pids = await self._cleanup_residual_process_group(process.pid)

                self._processes.pop(job_id, None)
                self._finalize_stderr(job_id)

                async with self._lock:
                    finished_at = utc_now()
                    finished_epoch = time.time()
                    if job_id in self._cancel_requested:
                        terminal = "cancelled"
                        error = "research job was cancelled"
                    elif timed_out:
                        terminal = "timed_out"
                        error = "research job exceeded MCP_RESEARCH_JOB_TIMEOUT"
                    elif self._is_valid_completed_result(paths["result"], process.returncode):
                        terminal = "completed"
                        error = None
                    else:
                        terminal = "failed"
                        envelope = read_json(paths["result"], {})
                        error = (
                            envelope.get("error")
                            if isinstance(envelope, dict)
                            else None
                        ) or (
                            "research worker returned an invalid or incomplete result"
                            if process.returncode == 0
                            else f"research worker exited with code {process.returncode}"
                        )

                    self._write_status(
                        job_id,
                        status=terminal,
                        phase=terminal,
                        active_codex=0,
                        finished_at=finished_at,
                        finished_at_epoch=finished_epoch,
                        worker_exit_code=process.returncode,
                        error=error,
                        residual_processes_terminated=residual_pids,
                    )
                    self._append_event(
                        job_id,
                        terminal,
                        exit_code=process.returncode,
                        error=error,
                    )
                    result = read_json(paths["result"], {})
                    result_body = (
                        result.get("result") or result.get("failure") or {}
                        if isinstance(result, dict)
                        else {}
                    )
                    self._write_manifest(
                        job_id,
                        status=terminal,
                        finished_at=finished_at,
                        worker_exit_code=process.returncode,
                        report_path=(
                            result_body.get("path")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        total_cost_usd=(
                            result_body.get("total_cost_usd")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        http_sources_count=(
                            result_body.get("http_sources_count")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        work_item_count=(
                            result_body.get("work_item_count")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        codex_initial_calls=(
                            result_body.get("codex_initial_calls")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        codex_total_calls=(
                            result_body.get("codex_total_calls")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        active_codex_peak=(
                            result_body.get("active_codex_peak")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        quality_gate_passed=(
                            result_body.get("quality_gate_passed")
                            if isinstance(result_body, dict)
                            else False
                        ),
                        target_date=(
                            result_body.get("target_date")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        timezone=(
                            result_body.get("timezone")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        profile=(
                            result_body.get("profile")
                            if isinstance(result_body, dict)
                            else None
                        ),
                        work_items=(
                            result_body.get("research_work_items", [])
                            if isinstance(result_body, dict)
                            else []
                        ),
                        evidence_items=(
                            result_body.get("evidence_items", [])
                            if isinstance(result_body, dict)
                            else []
                        ),
                        evidence_conflicts=(
                            result_body.get("evidence_conflicts", [])
                            if isinstance(result_body, dict)
                            else []
                        ),
                        sources=(
                            result_body.get("source_urls", [])
                            if isinstance(result_body, dict)
                            else []
                        ),
                        codex_pids=(
                            result_body.get("codex_pids", [])
                            if isinstance(result_body, dict)
                            else []
                        ),
                        codex_runs=(
                            result_body.get("codex_runs", [])
                            if isinstance(result_body, dict)
                            else []
                        ),
                        stage_attempts=(
                            result_body.get("attempts", [])
                            if isinstance(result_body, dict)
                            else []
                        ),
                        evidence_metrics=(
                            result_body.get("evidence_metrics", {})
                            if isinstance(result_body, dict)
                            else {}
                        ),
                        coverage_audit=(
                            result_body.get("coverage_audit", {})
                            if isinstance(result_body, dict)
                            else {}
                        ),
                        worker_interval={
                            "started_at": (
                                self._status_unchecked(job_id) or {}
                            ).get("started_at"),
                            "finished_at": finished_at,
                        },
                        residual_processes_terminated=residual_pids,
                    )
        except asyncio.CancelledError:
            if process and process.returncode is None:
                await self._terminate_process_group(process)
            self._processes.pop(job_id, None)
            paths = self._paths(self._job_dir(job_id))
            self._finalize_stderr(job_id)
            async with self._lock:
                current = self._status_unchecked(job_id)
                if current and current.get("status") in ACTIVE_STATUSES:
                    now = time.time()
                    cancelled = job_id in self._cancel_requested
                    terminal = "cancelled" if cancelled else "interrupted"
                    reason = (
                        "research job was cancelled"
                        if cancelled
                        else "coordinator stopped while the job was active"
                    )
                    self._write_status(
                        job_id,
                        status=terminal,
                        phase=terminal,
                        active_codex=0,
                        finished_at=utc_now(),
                        finished_at_epoch=now,
                        error=reason,
                    )
                    self._append_event(job_id, terminal, reason=reason)
                    self._write_manifest(
                        job_id,
                        status=terminal,
                        finished_at=utc_now(),
                        error=reason,
                    )
            raise
        except Exception as exc:
            if process and process.returncode is None:
                await self._terminate_process_group(process)
            self._processes.pop(job_id, None)
            self._finalize_stderr(job_id)
            safe_error = f"{type(exc).__name__}: {self._redact_error(exc)}"
            async with self._lock:
                current = self._status_unchecked(job_id)
                if current and current.get("status") in ACTIVE_STATUSES:
                    now = time.time()
                    self._write_status(
                        job_id,
                        status="failed",
                        phase="failed",
                        active_codex=0,
                        finished_at=utc_now(),
                        finished_at_epoch=now,
                        error=safe_error,
                    )
                    self._append_event(
                        job_id,
                        "failed",
                        error=safe_error,
                    )
                    self._write_manifest(
                        job_id,
                        status="failed",
                        finished_at=utc_now(),
                        error=safe_error,
                    )
        finally:
            self._cancel_requested.discard(job_id)

    @staticmethod
    def _is_valid_completed_result(path: Path, returncode: int | None) -> bool:
        if returncode != 0:
            return False
        envelope = read_json(path)
        return (
            isinstance(envelope, dict)
            and envelope.get("status") == "completed"
            and isinstance(envelope.get("result"), dict)
        )

    async def _cleanup_residual_process_group(self, process_group: int) -> list[int]:
        """Clean children that outlived a normally exiting worker.

        The worker is a process-group leader. Immediately after it is reaped,
        that group id cannot be reused while any old member remains, so it is
        safe to signal the residual group. Codex currently inherits this group.
        """
        if os.name != "posix":
            return []
        members: list[int] = []
        for candidate in psutil.process_iter(["pid"]):
            try:
                if candidate.pid != process_group and os.getpgid(candidate.pid) == process_group:
                    members.append(candidate.pid)
            except (OSError, psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not members:
            return []
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return members
        deadline = time.monotonic() + max(0.1, self.terminate_grace_seconds)
        while time.monotonic() < deadline and any(_pid_is_live(pid) for pid in members):
            await asyncio.sleep(0.05)
        survivors = [pid for pid in members if _pid_is_live(pid)]
        if survivors:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for pid in survivors:
                try:
                    psutil.Process(pid).kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return members

    async def _terminate_process_group(self, process: asyncio.subprocess.Process) -> None:
        job_id = next(
            (candidate for candidate, child in self._processes.items() if child is process),
            None,
        )
        tracked: dict[int, psutil.Process] = {}
        try:
            root = psutil.Process(process.pid)
            for item in (root, *root.children(recursive=True)):
                tracked[item.pid] = item
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        if job_id:
            tracked.update(self._processes_owned_by_job(job_id))
        try:
            if process.returncode is None and os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            elif process.returncode is None:
                process.terminate()
        except ProcessLookupError:
            pass
        for pid, item in tracked.items():
            if pid == process.pid:
                continue
            try:
                item.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            if process.returncode is None:
                await asyncio.wait_for(process.wait(), timeout=self.terminate_grace_seconds)
        except TimeoutError:
            pass

        # A worker can spawn a new-session child between the initial process-tree
        # snapshot and TERM. Once the worker has stopped, rescan by its unique
        # job identity before deciding cleanup is complete.
        if job_id:
            tracked.update(self._processes_owned_by_job(job_id))
        alive: list[psutil.Process] = []
        if tracked:
            _, alive = await asyncio.to_thread(
                psutil.wait_procs, list(tracked.values()), timeout=0
            )
        if process.returncode is None or alive:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            for item in alive:
                try:
                    item.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if alive:
                await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)
        if process.returncode is None:
            await process.wait()
        if job_id:
            late = self._processes_owned_by_job(job_id)
            for item in late.values():
                try:
                    item.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if late:
                await asyncio.to_thread(psutil.wait_procs, list(late.values()), timeout=3)

    @staticmethod
    def _processes_owned_by_job(job_id: str) -> dict[int, psutil.Process]:
        owned: dict[int, psutil.Process] = {}
        for process in psutil.process_iter(["pid"]):
            try:
                if process.environ().get("MCP_RESEARCH_JOB_ID") == job_id:
                    owned[process.pid] = process
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return owned

    def _telemetry(self, job_id: str, status: Mapping[str, Any]) -> dict[str, Any]:
        telemetry = read_json(self._paths(self._job_dir(job_id))["worker_status"], {})
        if not isinstance(telemetry, dict):
            telemetry = {}
        progress = telemetry.get("progress")
        if not isinstance(progress, dict):
            progress = status.get("progress") or {"completed": 0, "total": 3}
        active_codex = telemetry.get("active_codex", status.get("active_codex", 0))
        persisted_phase = status.get("phase") or status.get("status")
        if status.get("status") in TERMINAL_STATUSES:
            active_codex = 0
            phase = persisted_phase
        elif persisted_phase == "cancelling":
            phase = persisted_phase
        else:
            phase = telemetry.get("phase") or persisted_phase
        return {
            "phase": phase,
            "progress": progress,
            "active_codex": int(active_codex or 0),
        }

    def compact_status(self, job_id: str) -> dict[str, Any]:
        status = self._status_unchecked(job_id)
        if not status:
            return {"job_id": job_id, "status": "not_found", "error": "unknown research job id"}
        paths = self._paths(self._job_dir(job_id))
        now = time.time()
        started = float(status.get("started_at_epoch") or status.get("created_at_epoch") or now)
        finished = float(status.get("finished_at_epoch") or now)
        artifacts: dict[str, str] = {
            "job_dir": str(self._job_dir(job_id)),
            "spec": str(paths["spec"]),
            "events": str(paths["events"]),
            "status": str(paths["status"]),
            "manifest": str(paths["manifest"]),
        }
        if paths["result"].exists():
            artifacts["result"] = str(paths["result"])
        if paths["stderr"].exists():
            artifacts["stderr"] = str(paths["stderr"])
        envelope = read_json(paths["result"], {})
        if isinstance(envelope, dict):
            body = envelope.get("result") or envelope.get("failure")
            if isinstance(body, dict) and body.get("path"):
                artifacts["report" if status.get("status") == "completed" else "failure"] = str(
                    body["path"]
                )

        compact = {
            "job_id": job_id,
            "status": status.get("status"),
            **self._telemetry(job_id, status),
            "created_at": status.get("created_at"),
            "started_at": status.get("started_at"),
            "finished_at": status.get("finished_at"),
            "elapsed_seconds": round(max(0.0, finished - started), 3),
            "timeout_seconds": status.get("timeout_seconds"),
            "artifacts": artifacts,
        }
        if status.get("error"):
            compact["error"] = status["error"]
        if status.get("status") == "queued":
            queued: list[tuple[float, str]] = []
            for candidate in self.jobs_dir.iterdir():
                candidate_status = read_json(self._paths(candidate)["status"], {}) if candidate.is_dir() else {}
                if isinstance(candidate_status, dict) and candidate_status.get("status") == "queued":
                    queued.append(
                        (float(candidate_status.get("created_at_epoch") or 0), str(candidate_status.get("job_id")))
                    )
            queued.sort()
            compact["queue_position"] = next(
                (index + 1 for index, (_, queued_id) in enumerate(queued) if queued_id == job_id),
                None,
            )
        return compact

    def _fingerprint(self, job_id: str) -> tuple[int, int, str]:
        paths = self._paths(self._job_dir(job_id))
        status_mtime = paths["status"].stat().st_mtime_ns if paths["status"].exists() else 0
        worker_mtime = (
            paths["worker_status"].stat().st_mtime_ns if paths["worker_status"].exists() else 0
        )
        state = (self._status_unchecked(job_id) or {}).get("status", "not_found")
        return status_mtime, worker_mtime, str(state)

    async def wait_status(self, job_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        self._ensure_recovered_tasks()
        status = self.compact_status(job_id)
        if wait_seconds <= 0 or status.get("status") in TERMINAL_STATUSES | {"not_found"}:
            return status
        initial = self._fingerprint(job_id)
        deadline = time.monotonic() + min(max(wait_seconds, 0), 60)
        while time.monotonic() < deadline:
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            if self._fingerprint(job_id) != initial:
                break
        return self.compact_status(job_id)

    async def wait_many(
        self, job_ids: Sequence[str], wait_seconds: float = 20
    ) -> list[dict[str, Any]]:
        self._ensure_recovered_tasks()
        unique_ids = list(dict.fromkeys(job_ids))
        initial = {
            job_id: self._fingerprint(job_id)
            for job_id in unique_ids
            if self._status_unchecked(job_id)
        }
        statuses = [self.compact_status(job_id) for job_id in unique_ids]
        if wait_seconds <= 0 or all(
            item.get("status") in TERMINAL_STATUSES | {"not_found"} for item in statuses
        ):
            return statuses
        deadline = time.monotonic() + min(max(wait_seconds, 0), 60)
        while time.monotonic() < deadline:
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            if any(self._fingerprint(job_id) != fingerprint for job_id, fingerprint in initial.items()):
                break
        return [self.compact_status(job_id) for job_id in unique_ids]

    def result(self, job_id: str, *, include_report: bool = False) -> dict[str, Any]:
        status = self.compact_status(job_id)
        if status.get("status") == "not_found":
            return status
        if status.get("status") not in TERMINAL_STATUSES:
            return {
                "job_id": job_id,
                "status": status.get("status"),
                "error": "research job has not finished",
            }
        envelope = read_json(self._paths(self._job_dir(job_id))["result"], {})
        if not isinstance(envelope, dict):
            envelope = {}
        payload = dict(envelope)
        body = payload.get("result") or payload.get("failure")
        if isinstance(body, dict) and not include_report:
            body = dict(body)
            for large_key in (
                "report",
                "evidence_items",
                "research_work_items",
                "codex_runs",
            ):
                body.pop(large_key, None)
            payload["result" if "result" in payload else "failure"] = body
        summary: dict[str, Any] = {}
        if isinstance(body, dict):
            for key in (
                "path",
                "http_sources_count",
                "work_item_count",
                "codex_initial_calls",
                "codex_total_calls",
                "active_codex_peak",
                "quality_gate_passed",
                "total_cost_usd",
                "target_date",
                "timezone",
            ):
                if key in body:
                    summary[key] = body[key]
        summary["manifest_path"] = status.get("artifacts", {}).get("manifest")
        if not payload.get("error") and status.get("error"):
            payload["error"] = status["error"]
        return {
            "job_id": job_id,
            "status": status.get("status"),
            **summary,
            **payload,
        }

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self._ensure_recovered_tasks()
        async with self._lock:
            status = self._status_unchecked(job_id)
            if not status:
                return {"job_id": job_id, "status": "not_found", "error": "unknown research job id"}
            if status.get("status") in TERMINAL_STATUSES:
                return self.compact_status(job_id)
            task = self._tasks.get(job_id)
            process = self._processes.get(job_id)
            self._cancel_requested.add(job_id)
            if status.get("status") == "queued":
                now = time.time()
                self._write_status(
                    job_id,
                    status="cancelled",
                    phase="cancelled",
                    finished_at=utc_now(),
                    finished_at_epoch=now,
                    error="research job was cancelled before it started",
                )
                self._append_event(job_id, "cancelled", reason="cancelled while queued")
                self._write_manifest(
                    job_id,
                    status="cancelled",
                    finished_at=utc_now(),
                    error="research job was cancelled before it started",
                )
                if task:
                    task.cancel()
                return self.compact_status(job_id)
            self._write_status(job_id, phase="cancelling")
            self._append_event(job_id, "cancellation_requested")

        if process:
            await self._terminate_process_group(process)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.terminate_grace_seconds + 2)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        return self.compact_status(job_id)

    async def shutdown(self) -> None:
        """Terminate live workers; primarily used by tests and graceful shutdown."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _pid_is_live(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
