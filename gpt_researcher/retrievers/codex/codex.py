"""Codex CLI web-search retriever.

Codex is launched through the repository helper as a cancellable subprocess.
Helpers inherit the isolated report worker's process group so report-level
cancellation reaches the full tree; a targeted call cancellation recursively
terminates only that helper's descendants. A file-lock slot pool also caps
Codex usage across independent MCP worker processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
from filelock import FileLock, Timeout as FileLockTimeout

from ...evidence import EvidenceItem, canonical_http_url, deduplicate_evidence
from ...job_manager import default_global_slot_root


_TRANSIENT_ERROR_MARKERS = (
    "connection reset",
    "connection refused",
    "network",
    "rate limit",
    "temporarily unavailable",
    '"type":"thread.started"',
    "timed out",
    "timeout",
    "try again",
)
_HELPER_ENV_KEYS = {
    "ALL_PROXY",
    "CODEX_HOME",
    "CODEX_MODEL",
    "CODEX_SEARCH_CODEX_BIN",
    "CODEX_SEARCH_CODEX_HOME",
    "CODEX_SEARCH_MODEL",
    "CODEX_SEARCH_MODEL_PROVIDER",
    "CODEX_SEARCH_PROVIDER_BASE_URL",
    "CODEX_SEARCH_REASONING_EFFORT",
    "CODEX_SEARCH_SERVICE_TIER",
    "CODEX_SEARCH_SUPPORTS_WEBSOCKETS",
    "CODEX_SEARCH_USE_USER_CONFIG",
    "CODEX_SEARCH_WORKDIR",
    "CURL_CA_BUNDLE",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "NO_PROXY",
    "OPENAI_API_KEY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


class _GlobalCodexSlot:
    """One lease from a cross-process file-lock slot pool."""

    def __init__(self, *, limit: int, directory: Path, poll_interval: float = 0.05):
        self.limit = min(9, max(1, limit))
        self.directory = directory
        self.poll_interval = poll_interval
        self.slot: int | None = None
        self._lock: FileLock | None = None

    async def __aenter__(self) -> "_GlobalCodexSlot":
        self.directory.mkdir(parents=True, exist_ok=True)
        while True:
            for slot in range(self.limit):
                lock = FileLock(str(self.directory / f"slot-{slot}.lock"), timeout=0)
                try:
                    # timeout=0 is non-blocking and safe on the event-loop thread.
                    lock.acquire(timeout=0)
                except FileLockTimeout:
                    continue
                self.slot = slot
                self._lock = lock
                return self
            await asyncio.sleep(self.poll_interval)

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._lock is not None:
            self._lock.release()
        self._lock = None
        self.slot = None


class CodexSearch:
    """Search retriever backed by local Codex CLI web search."""

    def __init__(self, query: str, query_domains=None, **kwargs: Any):
        self.query = query
        self.query_domains = query_domains or []
        self.repo_root = Path(__file__).resolve().parents[3]
        self.helper_path = Path(
            os.getenv("CODEX_SEARCH_HELPER", self.repo_root / "codex_search" / "codex_search.py")
        )
        self.timeout = min(
            300,
            max(
                1,
                int(
                    os.getenv(
                        "CODEX_SEARCH_RETRIEVER_TIMEOUT",
                        os.getenv("CODEX_SEARCH_TIMEOUT", "300"),
                    )
                ),
            ),
        )
        self.retries = min(
            1,
            max(0, int(os.getenv("CODEX_SEARCH_RETRIEVER_RETRIES", "1"))),
        )
        self.retry_delay = float(os.getenv("CODEX_SEARCH_RETRIEVER_RETRY_DELAY", "2"))
        self.mode = os.getenv(
            "CODEX_SEARCH_RETRIEVER_MODE", os.getenv("CODEX_SEARCH_MODE", "search")
        )
        self.model = os.getenv("CODEX_SEARCH_MODEL") or os.getenv("CODEX_MODEL")
        self.max_chars = int(os.getenv("CODEX_SEARCH_RETRIEVER_MAX_CHARS", "12000"))
        self.debug = os.getenv("CODEX_SEARCH_RETRIEVER_DEBUG", "").lower() in {
            "1",
            "true",
            "yes",
        }
        self.global_limit = min(
            9, max(1, int(os.getenv("CODEX_SEARCH_GLOBAL_CONCURRENCY", "9")))
        )
        self.slot_directory = Path(
            os.getenv(
                "CODEX_SEARCH_GLOBAL_SLOT_DIR",
                str(default_global_slot_root() / "codex"),
            )
        ).expanduser().resolve()
        self.active_pid: int | None = None
        self.last_pid: int | None = None
        self.run_history: list[dict[str, Any]] = []
        self.last_run_metadata: dict[str, Any] = {}
        self._last_process_telemetry: dict[str, Any] = {}

    async def search_async(self, max_results: int = 5) -> list[dict[str, Any]]:
        """Run Codex asynchronously and return source-addressable results."""

        if not self.helper_path.exists():
            return []

        prompt = self._build_query()
        try:
            configured_max_results = int(os.getenv("CODEX_SEARCH_MAX_RESULTS", "12"))
        except ValueError:
            configured_max_results = 12
        codex_max_results = min(50, max(max_results, configured_max_results))
        last_error = ""
        for attempt in range(1, self.retries + 2):
            started = time.monotonic()
            self.last_pid = None
            self._last_process_telemetry = {}
            lease = _GlobalCodexSlot(limit=self.global_limit, directory=self.slot_directory)
            slot_id: int | None = None
            slot_acquired_at: str | None = None
            slot_released_at: str | None = None
            try:
                async with lease:
                    slot_id = lease.slot
                    slot_acquired_at = datetime.now(UTC).isoformat()
                    returncode, stdout, stderr, pid = await self._run_helper(prompt)
                slot_released_at = datetime.now(UTC).isoformat()
            except asyncio.CancelledError:
                self._record_run(
                    attempt=attempt,
                    started=started,
                    pid=self.last_pid,
                    slot=slot_id,
                    exit_code=None,
                    status="cancelled",
                    error="cancelled",
                    slot_acquired_at=slot_acquired_at,
                    slot_released_at=datetime.now(UTC).isoformat(),
                    process_telemetry=self._last_process_telemetry,
                )
                raise
            except TimeoutError:
                returncode, stdout, stderr, pid = None, "", "", self.last_pid
                last_error = f"codex search timed out after {self.timeout}s"
                status = "timed_out"
            except Exception as exc:  # keep other retrievers usable on helper failure
                returncode, stdout, stderr, pid = None, "", "", self.last_pid
                last_error = self._redact(str(exc))[:1000]
                status = "failed"
            else:
                if returncode == 0 and stdout.strip():
                    results = self._parse_results(stdout, max_results=codex_max_results)
                    if results:
                        metadata = self._record_run(
                            attempt=attempt,
                            started=started,
                            pid=pid,
                            slot=slot_id,
                            exit_code=returncode,
                            status="completed",
                            error="",
                            slot_acquired_at=slot_acquired_at,
                            slot_released_at=slot_released_at,
                            process_telemetry=self._last_process_telemetry,
                        )
                        for result in results:
                            result["codex_run"] = metadata.copy()
                        if self.debug:
                            self._debug(
                                f"CodexSearch ok: mode={self.mode} elapsed={metadata['elapsed']:.1f}s "
                                f"sources={len(results)} query={self.query[:120]}"
                            )
                        return results
                    last_error = "Codex returned no valid HTTP(S) sources"
                    status = "invalid_output"
                else:
                    last_error = self._redact((stderr or stdout or "").strip())[:1000]
                    status = "failed"

            metadata = self._record_run(
                attempt=attempt,
                started=started,
                pid=pid,
                slot=slot_id,
                exit_code=returncode,
                status=status,
                error=last_error,
                slot_acquired_at=slot_acquired_at,
                slot_released_at=slot_released_at or datetime.now(UTC).isoformat(),
                process_telemetry=self._last_process_telemetry,
            )
            if attempt > self.retries or not self._is_transient(last_error, status):
                break
            metadata["retry_reason"] = last_error
            await asyncio.sleep(self.retry_delay)

        if self.debug and last_error:
            self._debug(
                f"CodexSearch failed: mode={self.mode} query={self.query[:120]} "
                f"error={last_error}"
            )
        return []

    def search(self, max_results: int = 5) -> list[dict[str, Any]]:
        """Synchronous compatibility wrapper around :meth:`search_async`."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.search_async(max_results=max_results))

        # Older integrations sometimes call search() from an async thread. Run a
        # private event loop in a helper thread rather than nesting asyncio.run().
        result: list[dict[str, Any]] = []
        error: BaseException | None = None

        def runner() -> None:
            nonlocal result, error
            try:
                result = asyncio.run(self.search_async(max_results=max_results))
            except BaseException as exc:  # re-raised in the caller thread
                error = exc

        thread = threading.Thread(target=runner, name="codex-search-sync", daemon=True)
        thread.start()
        thread.join()
        if error is not None:
            raise error
        return result

    async def _run_helper(self, prompt: str) -> tuple[int, str, str, int]:
        telemetry_root = Path(
            os.getenv("MCP_RESEARCH_JOB_DIR", str(self.slot_directory / "telemetry"))
        ) / "codex-telemetry"
        telemetry_root.mkdir(parents=True, exist_ok=True)
        telemetry_path = telemetry_root / f"{uuid4()}.json"
        self._last_process_telemetry = {}
        cmd = [
            sys.executable,
            str(self.helper_path),
            "--mode",
            self.mode,
            "--timeout",
            str(self.timeout),
            "--telemetry-file",
            str(telemetry_path),
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.debug:
            cmd.append("--show-events")
        cmd.append("-")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.repo_root),
            env=self._helper_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Inherit the report worker's process group so report-level
            # cancellation reaches helper and Codex descendants. Targeted
            # cancellation of one call uses the recursive cleanup below.
            start_new_session=False,
        )
        self.active_pid = process.pid
        self.last_pid = process.pid
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=self.timeout + 30,
            )
        except asyncio.TimeoutError as exc:
            await self._terminate_process_group(process)
            raise TimeoutError from exc
        except asyncio.CancelledError:
            await self._terminate_process_group(process)
            raise
        finally:
            self.active_pid = None
            try:
                telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
                if isinstance(telemetry, dict):
                    self._last_process_telemetry = telemetry
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                self._last_process_telemetry = {}
            telemetry_path.unlink(missing_ok=True)

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = self._redact(stderr_bytes.decode("utf-8", errors="replace"))
        return process.returncode or 0, stdout, stderr, process.pid

    async def _terminate_process_group(self, process: asyncio.subprocess.Process) -> None:
        """Terminate one helper tree without killing sibling Codex calls."""

        descendants: list[psutil.Process] = []
        root: psutil.Process | None = None
        try:
            root = psutil.Process(process.pid)
            descendants = root.children(recursive=True)
            root.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()

        for child in reversed(descendants):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                child.terminate()

        tracked = ([root] if root is not None else []) + descendants
        if tracked:
            _, alive = await asyncio.to_thread(psutil.wait_procs, tracked, timeout=3)
            for item in alive:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    item.kill()
            if alive:
                await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)

        if process.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3)

    def _parse_results(self, raw: str, *, max_results: int) -> list[dict[str, Any]]:
        if max_results <= 0:
            return []
        payload = self._parse_payload(raw)
        if payload is None:
            payload = self._markdown_fallback_payload(raw)

        sources: dict[str, dict[str, str]] = {}
        for source in payload.get("sources", []):
            if not isinstance(source, dict):
                continue
            url = canonical_http_url(str(source.get("url") or ""))
            if url is None or url in sources:
                continue
            sources[url] = {
                "url": url,
                "title": str(source.get("title") or url),
                "summary": str(source.get("summary") or "").strip(),
            }

        claims_by_source: dict[str, list[EvidenceItem]] = {url: [] for url in sources}
        for claim in payload.get("claims", []):
            if not isinstance(claim, dict):
                continue
            raw_claim = claim.get("claim")
            if not isinstance(raw_claim, str) or not raw_claim.strip():
                continue
            claim_text = raw_claim.strip()
            source_urls = claim.get("source_urls")
            if not isinstance(source_urls, list):
                continue
            for raw_url in source_urls:
                url = canonical_http_url(str(raw_url))
                if url is None:
                    continue
                if url not in sources:
                    sources[url] = {"url": url, "title": url, "summary": ""}
                    claims_by_source[url] = []
                try:
                    evidence = EvidenceItem(
                        claim=claim_text,
                        value=claim.get("value"),
                        unit=str(claim["unit"]) if claim.get("unit") is not None else None,
                        as_of_date=(
                            str(claim["as_of_date"])
                            if claim.get("as_of_date") is not None
                            else None
                        ),
                        source_url=url,
                        source_title=sources[url]["title"],
                        retriever=self.__class__.__name__,
                        summary=str(claim.get("summary") or "").strip(),
                    )
                except (TypeError, ValueError):
                    continue
                claims_by_source[url].append(evidence)

        results: list[dict[str, Any]] = []
        for url, source in sources.items():
            evidence = deduplicate_evidence(claims_by_source.get(url, []))
            if not evidence:
                summary = source["summary"] or source["title"]
                evidence = [
                    EvidenceItem(
                        claim=summary,
                        source_url=url,
                        source_title=source["title"],
                        retriever=self.__class__.__name__,
                        summary=source["summary"],
                    )
                ]
            body = self._format_source_body(source, evidence)
            results.append(
                {
                    "title": source["title"],
                    "href": url,
                    "body": body[: self.max_chars],
                    "raw_content": body[: self.max_chars],
                    "retriever": self.__class__.__name__,
                    "evidence": [item.to_dict() for item in evidence],
                }
            )
            if len(results) >= max(0, max_results):
                break
        return results

    @staticmethod
    def _parse_payload(raw: str) -> dict[str, Any] | None:
        value = raw.strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
            value = re.sub(r"\s*```$", "", value)
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            start, end = value.find("{"), value.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                payload = json.loads(value[start : end + 1])
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict):
            return None
        payload.setdefault("claims", [])
        payload.setdefault("sources", [])
        payload.setdefault("caveats", [])
        return payload

    @staticmethod
    def _markdown_fallback_payload(raw: str) -> dict[str, Any]:
        sources: list[dict[str, str]] = []
        seen: set[str] = set()
        markdown_links = re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", raw)
        for title, raw_url in markdown_links:
            url = canonical_http_url(raw_url.rstrip(".,;"))
            if url and url not in seen:
                seen.add(url)
                sources.append({"url": url, "title": title.strip() or url, "summary": ""})
        for raw_url in re.findall(r"https?://[^\s<>\])\"']+", raw):
            url = canonical_http_url(raw_url.rstrip(".,;:"))
            if url and url not in seen:
                seen.add(url)
                sources.append({"url": url, "title": url, "summary": ""})
        claim = re.sub(r"\s+", " ", raw).strip()
        claims = []
        if claim and sources:
            claims.append(
                {
                    "claim": claim[:4000],
                    "value": None,
                    "unit": None,
                    "as_of_date": None,
                    "source_urls": [source["url"] for source in sources],
                    "summary": "Parsed from a legacy Markdown Codex response.",
                }
            )
        return {"claims": claims, "sources": sources, "caveats": ["legacy_markdown"]}

    @staticmethod
    def _format_source_body(source: dict[str, str], evidence: list[EvidenceItem]) -> str:
        parts = [f"Source URL: {source['url']}"]
        if source["summary"]:
            parts.append(source["summary"])
        for item in evidence:
            detail = item.claim
            if item.value is not None:
                detail += f" Value: {item.value}"
                if item.unit:
                    detail += f" {item.unit}"
            if item.as_of_date:
                detail += f" (as of {item.as_of_date})"
            if item.summary and item.summary != source["summary"]:
                detail += f" — {item.summary}"
            parts.append(f"- {detail}")
        return "\n".join(parts).strip()

    def _build_query(self) -> str:
        domain_hint = ""
        if self.query_domains:
            domain_hint = (
                "\nRestrict or prioritize these domains when useful: "
                + ", ".join(self.query_domains)
            )

        return (
            "Research this query for GPT Researcher. Return source-backed findings that can be "
            "merged with other retrievers. Collect independent evidence, exact dates and values, "
            "and direct HTTP(S) source URLs. Do not cite search-result pages or synthetic links."
            f"{domain_hint}\n\nQuery: {self.query}"
        )

    def _helper_env(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _HELPER_ENV_KEYS
        }
        env.setdefault("PATH", os.defpath)
        env.setdefault("LANG", "C.UTF-8")
        env["PYTHONUTF8"] = "1"
        return env

    def _redact(self, text: str) -> str:
        result = text
        for key, value in os.environ.items():
            if len(value) < 8:
                continue
            if any(marker in key.upper() for marker in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
                result = result.replace(value, "[REDACTED]")
        return result

    @staticmethod
    def _is_transient(error: str, status: str) -> bool:
        if status == "timed_out":
            return True
        lowered = error.lower()
        if "usage limit" in lowered or "purchase more credits" in lowered:
            return False
        transient_http_status = bool(
            re.search(
                r"(?:\bhttp\b|\bstatus(?:\s+code)?\b|\bresponse\b)"
                r"[^\d]{0,16}(?:429|500|502|503|504)\b",
                lowered,
            )
        )
        return transient_http_status or any(
            marker in lowered for marker in _TRANSIENT_ERROR_MARKERS
        )

    def _record_run(
        self,
        *,
        attempt: int,
        started: float,
        pid: int | None,
        slot: int | None,
        exit_code: int | None,
        status: str,
        error: str,
        slot_acquired_at: str | None = None,
        slot_released_at: str | None = None,
        process_telemetry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        elapsed = time.monotonic() - started
        finished_at = datetime.now(UTC)
        telemetry = process_telemetry or {}
        metadata = {
            "pid": pid,
            "helper_pid": pid,
            "codex_pid": telemetry.get("codex_pid"),
            "attempt": attempt,
            "elapsed": elapsed,
            "started_at": (finished_at - timedelta(seconds=elapsed)).isoformat(),
            "finished_at": finished_at.isoformat(),
            "exit_code": exit_code,
            "status": status,
            "slot": slot,
            "slot_acquired_at": slot_acquired_at,
            "slot_released_at": slot_released_at,
            "codex_started_at": telemetry.get("codex_started_at"),
            "codex_finished_at": telemetry.get("codex_finished_at"),
            "codex_exit_code": telemetry.get("codex_exit_code"),
            "mode": self.mode,
            "error": self._redact(error)[:1000],
        }
        self.run_history.append(metadata)
        self.last_run_metadata = metadata
        return metadata

    @staticmethod
    def _debug(message: str) -> None:
        print(message, file=sys.stderr)
