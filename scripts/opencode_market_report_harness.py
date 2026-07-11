#!/usr/bin/env python3
"""Run one or three isolated OpenCode market-report sessions.

The harness intentionally treats OpenCode as a black-box MCP client.  In
``stress`` mode one persistent ``opencode serve`` process owns the MCP
coordinator and three ``opencode run --attach`` clients submit the same report
at once.  ``--dry-run`` creates and validates every artifact without starting
OpenCode, a model, or a research job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

try:
    import psutil
except ImportError:  # pragma: no cover - caught with a clear runtime message
    psutil = None


RESULT_MARKER = "HARNESS_RESULT_JSON:"
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "interrupted",
    "not_found",
}
MARKET_QUERY_TEMPLATE = """帮我调研昨天的股票市场,针对市场大盘(美,日,韩,港),市场对宏观经济的预期,大宗商品,以及各种重要股票,结合最近热点,写一份详尽的日报,每个股票都需要调查透彻,写的日报详尽且严肃.

并且,虽然写昨天的日报,但是需要你调查最近一段时间的信息,并且请多次调用调查工具,直到获取所有可用证据,鼓励追问调查工具."""
NUMERIC_ENTITY_ALIASES = {
    "sp500": ("s&p 500", "标普500", "标普 500"),
    "dow": ("dow", "dow jones", "djia", "道琼斯"),
    "nasdaq": ("nasdaq", "nasdaq composite", "纳斯达克综合", "纳指"),
    "russell2000": ("russell 2000", "罗素2000", "罗素 2000"),
    "nikkei225": ("nikkei 225", "日经225", "日经 225"),
    "topix": ("topix", "东证股价", "东证指数"),
    "kospi": ("kospi",),
    "kosdaq": ("kosdaq",),
    "hangseng": ("hang seng", "hang seng index", "恒生指数"),
    "hangsengtech": ("hang seng tech", "hstech", "恒生科技"),
    "wti": ("wti", "西德州", "西德克萨斯"),
    "brent": ("brent", "布伦特"),
    "gold": ("gold", "黄金"),
    "copper": ("copper", "铜"),
}
REQUIRED_RESULT_MARKER_FIELDS = {
    "job_id",
    "status",
    "started_at",
    "finished_at",
    "elapsed_seconds",
    "report_path",
    "manifest_path",
    "http_sources_count",
    "cost",
    "work_item_count",
    "active_codex_peak",
    "quality_gate_passed",
}
MARKET_KEYS = ("us", "jp", "kr", "hk")
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}
_REPORT_HTTP_URL_PATTERN = re.compile(
    r"https?://[^\s<>\[\]()|]+", re.IGNORECASE
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        payload = {"timestamp": iso_now(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


@dataclass(frozen=True)
class Layout:
    project_root: Path
    run_id: str
    runtime_dir: Path
    artifact_dir: Path
    log_dir: Path
    xdg_config: Path
    xdg_data: Path
    xdg_cache: Path
    xdg_state: Path
    jobs_dir: Path
    reports_dir: Path
    codex_slot_dir: Path
    manifest_path: Path
    event_log_path: Path
    config_path: Path


@dataclass
class ProcessRun:
    index: int
    process: subprocess.Popen[bytes]
    log_path: Path
    log_handle: Any
    started_at: str
    started_monotonic: float
    ended_at: str | None = None
    ended_monotonic: float | None = None
    exit_code: int | None = None
    result: dict[str, Any] | None = None
    marker_result: dict[str, Any] | None = None

    def finish(self) -> None:
        if self.exit_code is not None:
            return
        self.exit_code = self.process.poll()
        self.ended_at = iso_now()
        self.ended_monotonic = time.monotonic()
        self.log_handle.close()
        self.marker_result = extract_result_marker(self.log_path)
        self.result = self.marker_result

    @property
    def elapsed_seconds(self) -> float | None:
        if self.ended_monotonic is None:
            return None
        return round(self.ended_monotonic - self.started_monotonic, 3)


def make_layout(project_root: Path, run_id: str, base_dir: Path | None = None) -> Layout:
    base = base_dir or project_root
    runtime_dir = base / ".tmp" / "opencode-market" / run_id
    artifact_dir = base / "outputs" / "stability" / run_id
    log_dir = base / "run_logs" / "opencode-market" / run_id
    for run_path in (runtime_dir, artifact_dir, log_dir):
        if run_path.exists():
            raise FileExistsError(
                f"refusing to reuse a prior run directory: {run_path}"
            )
    xdg_config = runtime_dir / "xdg" / "config"
    return Layout(
        project_root=project_root,
        run_id=run_id,
        runtime_dir=runtime_dir,
        artifact_dir=artifact_dir,
        log_dir=log_dir,
        xdg_config=xdg_config,
        xdg_data=runtime_dir / "xdg" / "data",
        xdg_cache=runtime_dir / "xdg" / "cache",
        xdg_state=runtime_dir / "xdg" / "state",
        jobs_dir=runtime_dir / "research-jobs",
        reports_dir=artifact_dir / "reports",
        codex_slot_dir=runtime_dir / "codex-global-slots",
        manifest_path=artifact_dir / "manifest.json",
        event_log_path=log_dir / "harness.jsonl",
        config_path=xdg_config / "opencode" / "opencode.jsonc",
    )


def initialize_layout(layout: Layout) -> None:
    for path in (
        layout.runtime_dir,
        layout.artifact_dir,
        layout.log_dir,
        layout.xdg_config,
        layout.xdg_data,
        layout.xdg_cache,
        layout.xdg_state,
        layout.jobs_dir,
        layout.reports_dir,
        layout.codex_slot_dir,
        layout.config_path.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)


def research_environment(layout: Layout) -> dict[str, str]:
    return {
        "GPT_RESEARCHER_PROFILE_DIR": str(layout.project_root),
        "MCP_RESEARCH_JOBS_DIR": str(layout.jobs_dir),
        "MCP_RESEARCH_MAX_CONCURRENT_JOBS": "3",
        "MCP_RESEARCH_MAX_QUEUED_JOBS": "9",
        "MCP_RESEARCH_JOB_TIMEOUT": "2700",
        "MCP_RESEARCH_JOB_RETENTION_HOURS": "72",
        "RETRIEVER": "tavily,codex",
        "LANGUAGE": "chinese",
        "TOTAL_WORDS": "6000",
        "SMART_TOKEN_LIMIT": "16000",
        "TAVILY_INCLUDE_RAW_CONTENT": "true",
        "TAVILY_SEARCH_DEPTH": "advanced",
        "COMPRESSION_FALLBACK_ON_ERROR": "true",
        "MCP_RESEARCH_FALLBACK_RETRIEVER": "",
        "MCP_RESEARCH_MIN_HTTP_SOURCES": "25",
        "MCP_RESEARCH_RETRIEVAL_ATTEMPTS": "2",
        "MCP_RESEARCH_WRITER_ATTEMPTS": "2",
        "MCP_RESEARCH_JUDGE_ATTEMPTS": "2",
        "MCP_RESEARCH_RETRIEVAL_TIMEOUT": "750",
        "MCP_RESEARCH_WRITER_TIMEOUT": "450",
        "MCP_RESEARCH_JUDGE_TIMEOUT": "120",
        "SEARCH_RETRIEVER_CONCURRENCY": "4",
        "MAX_SCRAPER_WORKERS": "5",
        "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "8",
        "RESEARCH_MIN_STOCK_SOURCES_PER_MARKET": "4",
        "RESEARCH_MIN_TOTAL_HTTP_SOURCES": "25",
        "CODEX_SEARCH_MODE": "search",
        "CODEX_SEARCH_TIMEOUT": "300",
        "CODEX_SEARCH_RETRIEVER_TIMEOUT": "300",
        "CODEX_SEARCH_MAX_RESULTS": "12",
        "CODEX_SEARCH_RETRIEVER_RETRIES": "1",
        "CODEX_SEARCH_RETRIEVER_RETRY_DELAY": "2",
        "CODEX_SEARCH_RETRIEVER_DEBUG": "true",
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "3",
        "CODEX_SEARCH_GLOBAL_CONCURRENCY": "9",
        "CODEX_SEARCH_GLOBAL_SLOT_DIR": str(layout.codex_slot_dir),
        "CODEX_SEARCH_MODEL": "gpt-5.5",
        "CODEX_SEARCH_REASONING_EFFORT": "medium",
        "CODEX_SEARCH_SERVICE_TIER": "fast",
        "CODEX_SEARCH_SUPPORTS_WEBSOCKETS": "false",
    }


def build_opencode_config(layout: Layout, uv_bin: str) -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "*": "deny",
            "gpt-researcher-codex-long_*": "allow",
        },
        "mcp": {
            "gpt-researcher-codex-long": {
                "type": "local",
                "command": [
                    uv_bin,
                    "run",
                    "--directory",
                    str(layout.project_root),
                    "gpt-researcher",
                ],
                "environment": research_environment(layout),
                "timeout": 3_000_000,
                "enabled": True,
            }
        },
    }


def harness_environment(layout: Layout) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "XDG_CONFIG_HOME": str(layout.xdg_config),
            "XDG_DATA_HOME": str(layout.xdg_data),
            "XDG_CACHE_HOME": str(layout.xdg_cache),
            "XDG_STATE_HOME": str(layout.xdg_state),
            "GPT_RESEARCHER_HARNESS_JOBS_DIR": str(layout.jobs_dir),
        }
    )
    return env


def market_prompt(target_date: str, timezone_name: str, _session_index: int) -> str:
    query = MARKET_QUERY_TEMPLATE
    return f"""你正在执行 GPT Researcher MCP 的独立验收会话。只允许调用 gpt-researcher-codex-long MCP 工具；不要调用 shell、文件读取、通用网页搜索，也不要查看 outputs/、run_logs/ 或任何旧报告。

严格执行：
1. 先调用 profile_info，确认 search + medium + fast 配置。
2. 只调用一次 research_report_start，参数 query 使用下方完整原文，并显式传 target_date={target_date!r}、timezone={timezone_name!r}。不要创建额外报告；服务端会把这一份报告拆成恰好 3 个并行 work item，并自行进行一次有上限的缺口补查。
3. 已确认批量接口可用：必须且只能用 research_reports_status(job_ids=[job_id], wait_seconds=20) 长轮询。不得使用 research_report_status，不得把 wait_seconds 省略或设为 0。一直等到 completed、failed、timed_out、cancelled、interrupted 或 not_found。
4. completed 时调用 research_report_result(job_id, include_report=false)。失败或超时时也保留状态和审计路径，不要编造报告。
5. 只使用本次 start 返回的 job_id 和其工具响应；严禁读取旧报告补写。
6. 最终回复必须以单独一行输出 {RESULT_MARKER} 后紧跟一个单行 JSON 对象。对象至少包含 job_id、status、started_at、finished_at、elapsed_seconds、report_path、manifest_path、http_sources_count、cost、work_item_count、active_codex_peak、quality_gate_passed。缺失字段填 null，不能省略。不要在 marker 后输出其他文字。

调查原文（不得改写或缩短）：
{query}
"""


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _decode_marker(text: str) -> dict[str, Any] | None:
    marker_index = text.rfind(RESULT_MARKER)
    if marker_index < 0:
        return None
    remainder = text[marker_index + len(RESULT_MARKER) :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(remainder)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def extract_result_marker(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    marker: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            direct = _decode_marker(line)
            if direct is not None:
                marker = direct
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for text in _walk_strings(event):
                nested = _decode_marker(text)
                if nested is not None:
                    marker = nested
    return marker


def _tool_output_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def audit_opencode_tool_calls(
    path: Path,
    *,
    job_id: str | None,
    target_date: str,
    timezone_name: str,
) -> dict[str, Any]:
    """Verify that one OpenCode session followed the intended MCP-only workflow."""

    prefix = "gpt-researcher-codex-long_"
    allowed = {
        "profile_info",
        "research_report_start",
        "research_reports_status",
        "research_report_status",
        "research_report_result",
    }
    calls: list[dict[str, Any]] = []
    session_ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
    for line_index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        if not isinstance(part, dict) or part.get("type") != "tool":
            continue
        tool = part.get("tool")
        if not isinstance(tool, str):
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        session_id = event.get("sessionID")
        if isinstance(session_id, str):
            session_ids.add(session_id)
        calls.append(
            {
                "line": line_index,
                "tool": tool,
                "name": tool.removeprefix(prefix),
                "status": state.get("status"),
                "input": state.get("input") if isinstance(state.get("input"), dict) else {},
                "output": _tool_output_object(state.get("output")),
            }
        )
    names = [call["name"] for call in calls]
    counts = {name: names.count(name) for name in sorted(set(names))}
    errors: list[str] = []
    if len(session_ids) != 1:
        errors.append(f"expected one OpenCode session id, observed {len(session_ids)}")
    if any(not call["tool"].startswith(prefix) or call["name"] not in allowed for call in calls):
        errors.append("observed a tool outside the allowed GPT Researcher MCP workflow")
    if any(call["status"] != "completed" for call in calls):
        errors.append("one or more MCP tool calls did not complete")
    for name in ("profile_info", "research_report_start", "research_report_result"):
        if counts.get(name) != 1:
            errors.append(f"expected exactly one {name} call, observed {counts.get(name, 0)}")
    status_count = counts.get("research_reports_status", 0) + counts.get(
        "research_report_status", 0
    )
    if status_count < 1:
        errors.append("no status long-poll call was observed")
    profile_call = next((call for call in calls if call["name"] == "profile_info"), None)
    expected_profile = {
        "CODEX_SEARCH_MODE": "search",
        "CODEX_SEARCH_REASONING_EFFORT": "medium",
        "CODEX_SEARCH_SERVICE_TIER": "fast",
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "3",
        "CODEX_SEARCH_GLOBAL_CONCURRENCY": "9",
    }
    profile_observed = {
        key: (profile_call or {}).get("output", {}).get(key)
        for key in expected_profile
    }
    if profile_observed != expected_profile:
        errors.append("profile_info did not confirm search + medium + fast + 3/9")
    start_call = next(
        (call for call in calls if call["name"] == "research_report_start"), None
    )
    expected_start_input = {
        "query": MARKET_QUERY_TEMPLATE,
        "target_date": target_date,
        "timezone": timezone_name,
    }
    if start_call is None or any(
        start_call["input"].get(key) != value
        for key, value in expected_start_input.items()
    ):
        errors.append("research_report_start input did not preserve query/date/timezone")
    if start_call is not None and start_call["output"].get("job_id") != job_id:
        errors.append("research_report_start output job_id disagreed with durable artifacts")
    result_call = next(
        (call for call in calls if call["name"] == "research_report_result"), None
    )
    if result_call is None or result_call["input"].get("job_id") != job_id:
        errors.append("research_report_result used a different job_id")
    if result_call is None or result_call["input"].get("include_report") is not False:
        errors.append("research_report_result did not use include_report=false")
    for call in calls:
        if call["name"] == "research_reports_status":
            if call["input"].get("job_ids") != [job_id]:
                errors.append("batch status call polled a job outside this session")
            wait_seconds = call["input"].get("wait_seconds")
            if not isinstance(wait_seconds, (int, float)) or not 10 <= wait_seconds <= 60:
                errors.append("batch status call did not use a bounded long poll")
        elif call["name"] == "research_report_status":
            if call["input"].get("job_id") != job_id:
                errors.append("fallback status call polled a job outside this session")
            wait_seconds = call["input"].get("wait_seconds")
            if not isinstance(wait_seconds, (int, float)) or not 10 <= wait_seconds <= 60:
                errors.append("fallback status call did not use a bounded long poll")
    key_positions = [
        next((call["line"] for call in calls if call["name"] == name), None)
        for name in (
            "profile_info",
            "research_report_start",
            "research_reports_status",
            "research_report_result",
        )
    ]
    if all(position is not None for position in key_positions) and key_positions != sorted(
        key_positions
    ):
        errors.append("MCP calls were not ordered profile -> start -> status -> result")
    return {
        "passed": not errors,
        "errors": errors,
        "session_ids": sorted(session_ids),
        "tool_call_counts": counts,
        "profile": profile_observed,
        "total_tool_calls": len(calls),
    }


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_tcp(port: int, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"opencode serve exited early with status {exit_code}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"opencode serve was not ready on port {port} within {timeout}s")


def start_run(
    *,
    index: int,
    command: list[str],
    env: dict[str, str],
    log_path: Path,
) -> ProcessRun:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("wb")
    started_at = iso_now()
    started_monotonic = time.monotonic()
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    return ProcessRun(
        index=index,
        process=process,
        log_path=log_path,
        log_handle=log_handle,
        started_at=started_at,
        started_monotonic=started_monotonic,
    )


def terminate_process_tree(
    process: subprocess.Popen[bytes] | None, tracked: set[int], grace_seconds: float = 5.0
) -> None:
    if process is None or psutil is None:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=grace_seconds)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return
    try:
        root = psutil.Process(process.pid)
        tree = root.children(recursive=True) + [root]
    except psutil.NoSuchProcess:
        return
    tracked.update(item.pid for item in tree)
    for item in reversed(tree):
        try:
            item.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(tree, timeout=grace_seconds)
    for item in alive:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=grace_seconds)


def live_tracked_pids(
    tracked: Iterable[int], expected_started_at: dict[int, datetime] | None = None
) -> list[int]:
    if psutil is None:
        return []
    alive: list[int] = []
    for pid in sorted(set(tracked)):
        try:
            process = psutil.Process(pid)
            expected = (expected_started_at or {}).get(pid)
            if expected is not None:
                actual = datetime.fromtimestamp(process.create_time(), timezone.utc)
                if abs((actual - expected).total_seconds()) > 10:
                    continue
            if process.status() != psutil.STATUS_ZOMBIE:
                alive.append(pid)
        except psutil.NoSuchProcess:
            pass
    return alive


def tracked_job_processes(
    runs: list[ProcessRun], manifests: list[dict[str, Any]], tracked: set[int]
) -> dict[int, datetime]:
    """Collect durable worker/helper/Codex identities for orphan verification."""

    expected: dict[int, datetime] = {}
    for run in runs:
        started = parse_timestamp(run.started_at)
        if started:
            expected[run.process.pid] = started
        for interval in (run.result or {}).get("codex_runs", []) or []:
            if not isinstance(interval, dict):
                continue
            for pid_key, time_key in (
                ("helper_pid", "started_at"),
                ("codex_pid", "codex_started_at"),
            ):
                pid = interval.get(pid_key)
                started_at = parse_timestamp(interval.get(time_key))
                if isinstance(pid, int):
                    tracked.add(pid)
                    if started_at:
                        expected[pid] = started_at
    for manifest in manifests:
        pid = manifest.get("worker_pid")
        started_at = parse_timestamp(
            (manifest.get("worker_interval") or {}).get("started_at")
        )
        if isinstance(pid, int):
            tracked.add(pid)
            if started_at:
                expected[pid] = started_at
    return expected


def is_within(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.expanduser().resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def _canonical_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    filtered_query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _TRACKING_QUERY_KEYS
        and not key.casefold().startswith("utm_")
    ]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            path,
            urlencode(sorted(filtered_query)),
            "",
        )
    )


def _unique_http_urls(values: Iterable[Any]) -> list[str]:
    return sorted(
        {
            canonical
            for value in values
            if (canonical := _canonical_http_url(value)) is not None
        }
    )


def report_http_sources(report: str) -> list[str]:
    candidates = [
        raw.rstrip(".,;:!?)]}") for raw in _REPORT_HTTP_URL_PATTERN.findall(report)
    ]
    return _unique_http_urls(candidates)


def _normalized_market_unit(entity: str, raw_unit: str) -> str:
    if entity not in {"wti", "brent", "gold", "copper"}:
        return "index points"
    normalized = re.sub(r"[\s*_`]", "", raw_unit.casefold())
    currency = "USD" if any(
        marker in normalized for marker in ("usd", "us$", "$", "\u7f8e\u5143")
    ) else normalized
    if any(marker in normalized for marker in ("barrel", "bbl", "\u6876")):
        basis = "barrel"
    elif any(marker in normalized for marker in ("troyounce", "ounce", "oz", "\u76ce\u53f8")):
        basis = "troy ounce"
    elif any(marker in normalized for marker in ("pound", "lb", "\u78c5")):
        basis = "pound"
    elif any(marker in normalized for marker in ("metricton", "tonne", "\u5428")):
        basis = "metric ton"
    else:
        basis = normalized
    return f"{currency}/{basis}" if currency and basis else normalized


def extract_common_market_values(
    report: str, target_date: str
) -> list[dict[str, Any]]:
    """Extract the ten index and four commodity values from report tables."""

    observations: list[dict[str, Any]] = []
    for raw_line in report.splitlines():
        if "|" not in raw_line or target_date not in raw_line:
            continue
        cells = [cell.strip() for cell in raw_line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        label = re.sub(r"[*_`]", "", cells[0]).casefold()
        tech_aliases = NUMERIC_ENTITY_ALIASES["hangsengtech"]
        if any(alias.casefold() in label for alias in tech_aliases):
            entity = "hangsengtech"
        else:
            entity = next(
                (
                    name
                    for name, aliases in NUMERIC_ENTITY_ALIASES.items()
                    if name != "hangsengtech"
                    and any(alias.casefold() in label for alias in aliases)
                ),
                None,
            )
        if entity is None:
            continue
        match = re.search(r"[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)", cells[1])
        if match is None:
            continue
        raw_value = match.group(0).replace(",", "")
        try:
            value = format(Decimal(raw_value).normalize(), "f")
        except InvalidOperation:
            continue
        raw_unit = cells[2] if entity in {"wti", "brent", "gold", "copper"} else ""
        observations.append(
            {
                "entity": entity,
                "value": value,
                "unit": _normalized_market_unit(entity, raw_unit),
                "as_of_date": target_date,
                "raw_value": cells[1],
                "raw_unit": raw_unit or None,
                "row_sha256": sha256_text(raw_line.strip()),
            }
        )
    return observations


def manifest_http_sources(manifest: dict[str, Any]) -> list[str]:
    """Return unique durable HTTP(S) evidence URLs from a worker manifest."""

    candidates: list[Any] = []
    direct = manifest.get("sources")
    if isinstance(direct, list):
        candidates.extend(direct)
    for evidence in _find_evidence_items(manifest):
        candidates.append(evidence.get("source_url") or evidence.get("url"))
    return _unique_http_urls(candidates)


def detailed_market_coverage_passed(audit: Any) -> bool:
    """Reject a shallow ``passed: true`` without the deterministic audit detail."""

    if not isinstance(audit, dict):
        return False
    empty_list_fields = (
        "missing_indices",
        "indices_without_two_direct_sources",
        "invalid_or_unverified_index_rows",
        "missing_commodities",
        "commodities_without_two_direct_sources",
        "invalid_or_unverified_commodity_rows",
        "incomplete_stock_rows",
    )
    if audit.get("applicable") is not True or audit.get("passed") is not True:
        return False
    if any(audit.get(field) != [] for field in empty_list_fields):
        return False
    if audit.get("deficient_markets") != {} or audit.get("deficient_selection_mix") != {}:
        return False
    if not isinstance(audit.get("distinct_stocks"), int) or audit["distinct_stocks"] < 16:
        return False
    counts = audit.get("stock_counts_by_market")
    mix = audit.get("selection_mix_by_market")
    if not isinstance(counts, dict) or not isinstance(mix, dict):
        return False
    for market in ("US", "Japan", "Korea", "Hong Kong"):
        if not isinstance(counts.get(market), int) or counts[market] < 4:
            return False
        market_mix = mix.get(market)
        if not isinstance(market_mix, dict):
            return False
        if int(market_mix.get("leaders", 0) or 0) < 2:
            return False
        if int(market_mix.get("event_movers", 0) or 0) < 2:
            return False
    report_sources = audit.get("report_http_sources_count")
    minimum_sources = audit.get("minimum_report_http_sources")
    return (
        isinstance(report_sources, int)
        and isinstance(minimum_sources, int)
        and minimum_sources >= 25
        and report_sources >= minimum_sources
    )


def current_job_ids(layout: Layout) -> set[str]:
    """Discover only UUID job directories created inside this harness run."""

    found: set[str] = set()
    if not layout.jobs_dir.is_dir():
        return found
    for child in layout.jobs_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            found.add(str(uuid.UUID(child.name)))
        except ValueError:
            continue
    return found


def reconcile_job_artifacts(
    runs: list[ProcessRun],
    layout: Layout,
    event_log: EventLog,
    *,
    target_date: str | None = None,
    timezone_name: str | None = None,
) -> None:
    """Replace model-transcribed marker metrics with durable job truth."""

    for run in runs:
        marker = run.marker_result or run.result or {}
        raw_job_id = marker.get("job_id")
        try:
            job_id = str(uuid.UUID(str(raw_job_id)))
        except (ValueError, TypeError, AttributeError):
            run.result = {
                **marker,
                "artifact_verified": False,
                "artifact_error": "marker did not contain a valid UUID job_id",
            }
            continue

        job_dir = layout.jobs_dir / job_id
        if not is_within(job_dir, (layout.jobs_dir,)):
            run.result = {
                **marker,
                "artifact_verified": False,
                "artifact_error": "job directory escaped the current run",
            }
            continue
        paths = {
            "spec": job_dir / "spec.json",
            "status": job_dir / "status.json",
            "result": job_dir / "result.json",
            "manifest": job_dir / "manifest.json",
        }
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not all(
            path.is_file() for path in paths.values()
        ):
            time.sleep(0.05)
        try:
            spec = json.loads(paths["spec"].read_text(encoding="utf-8"))
            status = json.loads(paths["status"].read_text(encoding="utf-8"))
            envelope = json.loads(paths["result"].read_text(encoding="utf-8"))
            manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            run.result = {
                **marker,
                "job_id": job_id,
                "artifact_verified": False,
                "artifact_error": f"missing or invalid job artifact: {type(exc).__name__}: {exc}",
            }
            continue
        if not all(
            isinstance(item, dict) for item in (spec, status, envelope, manifest)
        ):
            run.result = {
                **marker,
                "job_id": job_id,
                "artifact_verified": False,
                "artifact_error": "spec/status/result/manifest must all be JSON objects",
            }
            continue

        body = envelope.get("result")
        if not isinstance(body, dict):
            body = envelope.get("failure")
        if not isinstance(body, dict):
            body = {}
        codex_runs = body.get("codex_runs", []) or manifest.get("codex_runs", []) or []
        if not isinstance(codex_runs, list):
            codex_runs = []
        initial_codex_runs = [
            item
            for item in codex_runs
            if isinstance(item, dict) and item.get("initial_work_item") is True
        ]
        actual_codex_peak = codex_execution_peak(codex_runs)
        initial_codex_peak = codex_execution_peak(initial_codex_runs)
        interval_codex_pids = sorted(
            {
                item.get("codex_pid")
                for item in codex_runs
                if isinstance(item, dict) and isinstance(item.get("codex_pid"), int)
            }
        )
        interval_helper_pids = sorted(
            {
                item.get("helper_pid")
                for item in codex_runs
                if isinstance(item, dict) and isinstance(item.get("helper_pid"), int)
            }
        )
        body_codex_pids = sorted(
            pid
            for pid in (body.get("codex_pids", []) or [])
            if isinstance(pid, int)
        )
        manifest_codex_pids = sorted(
            pid
            for pid in (manifest.get("codex_pids", []) or [])
            if isinstance(pid, int)
        )
        source_urls = manifest_http_sources(manifest)
        body_source_urls = _unique_http_urls(body.get("source_urls", []) or [])
        coverage_audit = body.get("coverage_audit") or manifest.get("coverage_audit")
        report_path_value = body.get("path")
        report_path: Path | None = None
        report_path_verified = False
        report_source_urls: list[str] = []
        common_market_values: list[dict[str, Any]] = []
        report_read_error: str | None = None
        if isinstance(report_path_value, str) and report_path_value:
            report_path = Path(report_path_value).expanduser()
            if not report_path.is_absolute():
                report_path = job_dir / report_path
            report_path_verified = report_path.is_file() and is_within(
                report_path, (job_dir,)
            )
            if report_path_verified:
                try:
                    report_text = report_path.read_text(encoding="utf-8")
                    report_source_urls = report_http_sources(report_text)
                    common_market_values = extract_common_market_values(
                        report_text,
                        str(target_date or body.get("target_date") or ""),
                    )
                except (OSError, UnicodeError) as exc:
                    report_read_error = f"{type(exc).__name__}: {exc}"
        audited_report_source_count = (
            coverage_audit.get("report_http_sources_count")
            if isinstance(coverage_audit, dict)
            else None
        )
        report_source_urls_verified = (
            report_read_error is None
            and len(report_source_urls) >= 25
            and audited_report_source_count == len(report_source_urls)
            and set(report_source_urls).issubset(source_urls)
        )
        canonical = {
            "job_id": job_id,
            "status": status.get("status"),
            "started_at": status.get("started_at"),
            "finished_at": status.get("finished_at"),
            "elapsed_seconds": round(
                max(
                    0.0,
                    float(status.get("finished_at_epoch") or time.time())
                    - float(status.get("started_at_epoch") or status.get("created_at_epoch") or time.time()),
                ),
                3,
            ),
            "report_path": str(report_path) if report_path is not None else None,
            "report_path_verified": report_path_verified,
            "manifest_path": str(paths["manifest"]),
            "http_sources_count": body.get("http_sources_count"),
            "source_urls": source_urls,
            "source_urls_verified": bool(source_urls)
            and source_urls == body_source_urls,
            "report_http_sources_count": len(report_source_urls),
            "report_source_urls_verified": report_source_urls_verified,
            "common_market_values": common_market_values,
            "cost": body.get("total_cost_usd"),
            "work_item_count": body.get("work_item_count"),
            "codex_initial_calls": body.get("codex_initial_calls"),
            "codex_total_calls": body.get("codex_total_calls"),
            "active_codex_peak": actual_codex_peak,
            "initial_codex_peak": initial_codex_peak,
            "quality_gate_passed": body.get("quality_gate_passed"),
            "coverage_audit": coverage_audit,
            "detailed_market_coverage_passed": detailed_market_coverage_passed(
                coverage_audit
            ),
            "target_date": body.get("target_date") or manifest.get("target_date"),
            "timezone": body.get("timezone") or manifest.get("timezone"),
            "codex_pids": body_codex_pids,
            "codex_runs": codex_runs,
        }
        canonical["codex_telemetry_complete"] = (
            isinstance(canonical["codex_total_calls"], int)
            and len(codex_runs) >= canonical["codex_total_calls"]
            and len(interval_codex_pids) >= canonical["codex_total_calls"]
            and len(interval_helper_pids) >= canonical["codex_total_calls"]
            and body_codex_pids == interval_codex_pids
        )
        mismatches: dict[str, dict[str, Any]] = {}
        job_ids = {
            "spec": spec.get("job_id"),
            "status": status.get("job_id"),
            "manifest": manifest.get("job_id"),
        }
        if any(value != job_id for value in job_ids.values()):
            mismatches["job_id"] = {
                **job_ids,
                "expected": job_id,
            }
        if envelope.get("status") != canonical["status"]:
            mismatches["status"] = {
                "status_file": canonical["status"],
                "result_file": envelope.get("status"),
            }
        expected_prompt_hash = sha256_text(str(spec.get("query") or ""))
        if spec.get("query") != MARKET_QUERY_TEMPLATE:
            mismatches["spec_query"] = {
                "expected_sha256": sha256_text(MARKET_QUERY_TEMPLATE),
                "actual_sha256": expected_prompt_hash,
            }
        if manifest.get("prompt_sha256") != expected_prompt_hash:
            mismatches["prompt_sha256"] = {
                "manifest": manifest.get("prompt_sha256"),
                "computed_from_spec": expected_prompt_hash,
            }
        expected_target_date = target_date or spec.get("target_date")
        expected_timezone = timezone_name or spec.get("timezone")
        for key, expected_value in (
            ("target_date", expected_target_date),
            ("timezone", expected_timezone),
        ):
            observed = {
                "spec": spec.get(key),
                "result": canonical[key],
                "manifest": manifest.get(key),
            }
            if expected_value is None or any(
                value != expected_value for value in observed.values()
            ):
                mismatches[key] = {**observed, "expected": expected_value}
        if canonical["status"] == "completed" and not report_path_verified:
            mismatches["report_path"] = {
                "result": report_path_value,
                "reason": "completed report must exist inside its current UUID job directory",
            }
        if canonical["status"] == "completed" and not report_source_urls_verified:
            mismatches["report_source_urls"] = {
                "report_count": len(report_source_urls),
                "audited_count": audited_report_source_count,
                "manifest_count": len(source_urls),
                "unsupported": sorted(set(report_source_urls) - set(source_urls))[:10],
                "read_error": report_read_error,
            }
        manifest_report_path = manifest.get("report_path")
        if manifest_report_path != canonical["report_path"]:
            mismatches["manifest_report_path"] = {
                "result": canonical["report_path"],
                "manifest": manifest_report_path,
            }
        source_count = len(source_urls)
        if canonical["http_sources_count"] != source_count:
            mismatches["computed_http_sources_count"] = {
                "result": canonical["http_sources_count"],
                "computed_from_manifest_sources": source_count,
            }
        if manifest.get("http_sources_count") != source_count:
            mismatches["manifest_http_sources_count"] = {
                "manifest": manifest.get("http_sources_count"),
                "computed_from_manifest_sources": source_count,
            }
        if body_source_urls != source_urls:
            mismatches["source_urls"] = {
                "result_count": len(body_source_urls),
                "manifest_count": source_count,
                "missing_from_result": sorted(set(source_urls) - set(body_source_urls))[:10],
                "missing_from_manifest": sorted(set(body_source_urls) - set(source_urls))[:10],
            }
        if body.get("active_codex_peak") != actual_codex_peak:
            mismatches["computed_active_codex_peak"] = {
                "result": body.get("active_codex_peak"),
                "computed_from_intervals": actual_codex_peak,
            }
        if manifest_codex_pids != interval_codex_pids:
            mismatches["codex_pids"] = {
                "manifest": manifest_codex_pids,
                "result": body_codex_pids,
                "computed_from_intervals": interval_codex_pids,
            }
        for key in (
            "http_sources_count",
            "work_item_count",
            "codex_initial_calls",
            "codex_total_calls",
            "active_codex_peak",
            "quality_gate_passed",
            "target_date",
            "timezone",
        ):
            manifest_value = manifest.get(key)
            if manifest_value != canonical[key]:
                mismatches[key] = {
                    "result": canonical[key],
                    "manifest": manifest_value,
                }
        if manifest.get("coverage_audit") != coverage_audit:
            mismatches["coverage_audit"] = {
                "result": coverage_audit,
                "manifest": manifest.get("coverage_audit"),
            }
        marker_mismatches: dict[str, Any] = {}
        missing_marker_fields = sorted(REQUIRED_RESULT_MARKER_FIELDS - marker.keys())
        if missing_marker_fields:
            marker_mismatches["missing_fields"] = missing_marker_fields
        marker_to_canonical = {
            "status": "status",
            "report_path": "report_path",
            "manifest_path": "manifest_path",
            "http_sources_count": "http_sources_count",
            "cost": "cost",
            "work_item_count": "work_item_count",
            "active_codex_peak": "active_codex_peak",
            "quality_gate_passed": "quality_gate_passed",
        }
        for marker_key, canonical_key in marker_to_canonical.items():
            marker_value = marker.get(marker_key)
            if marker_key in marker and marker_value != canonical[canonical_key]:
                marker_mismatches[marker_key] = {
                    "marker": marker_value,
                    "artifact": canonical[canonical_key],
                }
        if marker.get("job_id") != job_id:
            marker_mismatches["job_id"] = {
                "marker": marker.get("job_id"),
                "artifact": job_id,
            }
        canonical["artifact_verified"] = not mismatches
        canonical["artifact_mismatches"] = mismatches
        canonical["marker_verified"] = not marker_mismatches
        canonical["marker_mismatches"] = marker_mismatches
        canonical["marker_result"] = marker
        run.result = canonical
        event_log.emit(
            "job_artifacts_reconciled",
            session_index=run.index,
            job_id=job_id,
            verified=not mismatches,
            marker_verified=not marker_mismatches,
            mismatches=mismatches,
            marker_mismatches=marker_mismatches,
        )


def load_current_job_manifests(
    runs: list[ProcessRun], layout: Layout, event_log: EventLog
) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    allowed_roots = (layout.jobs_dir, layout.reports_dir, layout.artifact_dir)
    for run in runs:
        raw_path = (run.result or {}).get("manifest_path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = layout.project_root / path
        if not is_within(path, allowed_roots):
            event_log.emit(
                "job_manifest_rejected",
                session_index=run.index,
                reason="outside_current_run",
                path=str(path),
            )
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            event_log.emit(
                "job_manifest_unreadable",
                session_index=run.index,
                path=str(path),
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        if isinstance(payload, dict):
            payload["_harness_session_index"] = run.index
            manifests.append(payload)
    return manifests


def _find_evidence_items(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        direct = value.get("evidence_items")
        if isinstance(direct, list):
            found.extend(item for item in direct if isinstance(item, dict))
        for key, item in value.items():
            if key != "evidence_items":
                found.extend(_find_evidence_items(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_evidence_items(item))
    return found


def _normalized_numeric_value(value: int | float | str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return format(Decimal(str(value)).normalize(), "f")
        except InvalidOperation:
            return str(value)
    normalized = re.sub(r"\s+", "", str(value).strip().casefold()).replace(",", "")
    normalized = re.sub(
        r"^(?:usd|us\$|hk\$|jpy|krw|cny|rmb|eur|gbp|\$|\u00a5|\u20a9|\u20ac|\u00a3)",
        "",
        normalized,
    )
    normalized = normalized.removesuffix("%")
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", normalized):
        try:
            return format(Decimal(normalized).normalize(), "f")
        except InvalidOperation:
            pass
    return normalized


def compare_numeric_evidence(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for manifest in manifests:
        session_index = manifest.get("_harness_session_index")
        for evidence in _find_evidence_items(manifest):
            value = evidence.get("value")
            if not isinstance(value, (int, float, str)) or isinstance(value, bool):
                continue
            normalized_value = _normalized_numeric_value(value)
            if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", normalized_value) is None:
                continue
            claim = re.sub(r"\s+", " ", str(evidence.get("claim", "")).strip().casefold())
            unit = str(evidence.get("unit") or "").strip().casefold()
            as_of = str(evidence.get("as_of_date") or evidence.get("as_of") or "").strip()
            if not claim:
                continue
            if any(
                alias in claim
                for alias in NUMERIC_ENTITY_ALIASES["hangsengtech"]
            ):
                entity = "hangsengtech"
            else:
                entity = next(
                    (
                        name
                        for name, aliases in NUMERIC_ENTITY_ALIASES.items()
                        if name != "hangsengtech"
                        and any(alias in claim for alias in aliases)
                    ),
                    claim,
                )
            key = (entity, unit, as_of)
            grouped.setdefault(key, []).append(
                {
                    "session_index": session_index,
                    "value": value,
                    "source_url": evidence.get("source_url") or evidence.get("url"),
                }
            )
    conflicts: list[dict[str, Any]] = []
    for (claim, unit, as_of), observations in grouped.items():
        distinct = {_normalized_numeric_value(item["value"]) for item in observations}
        sessions = {item["session_index"] for item in observations}
        if len(distinct) > 1 and len(sessions) > 1:
            conflicts.append(
                {
                    "claim": claim,
                    "unit": unit or None,
                    "as_of_date": as_of or None,
                    "observations": observations,
                }
            )
    return conflicts


def compare_report_common_values(
    runs: list[ProcessRun], *, target_date: str, expected_sessions: int
) -> dict[str, Any]:
    """Compare final-table index/commodity values and expose extraction gaps."""

    expected_entities = tuple(NUMERIC_ENTITY_ALIASES)
    selected: dict[str, list[dict[str, Any]]] = {
        entity: [] for entity in expected_entities
    }
    gaps: list[dict[str, Any]] = []
    for run in runs:
        by_entity: dict[str, list[dict[str, Any]]] = {}
        values = (run.result or {}).get("common_market_values", []) or []
        for item in values:
            if not isinstance(item, dict) or item.get("entity") not in selected:
                continue
            by_entity.setdefault(str(item["entity"]), []).append(item)
        for entity in expected_entities:
            observations = by_entity.get(entity, [])
            distinct = {
                (
                    str(item.get("value")),
                    str(item.get("unit")),
                    str(item.get("as_of_date")),
                )
                for item in observations
            }
            if len(distinct) != 1:
                gaps.append(
                    {
                        "session_index": run.index,
                        "entity": entity,
                        "reason": "missing" if not distinct else "ambiguous",
                        "observed": sorted(distinct),
                    }
                )
                continue
            item = observations[0]
            selected[entity].append(
                {
                    "session_index": run.index,
                    "job_id": (run.result or {}).get("job_id"),
                    "report_path": (run.result or {}).get("report_path"),
                    "value": item.get("value"),
                    "unit": item.get("unit"),
                    "as_of_date": item.get("as_of_date"),
                    "raw_value": item.get("raw_value"),
                    "raw_unit": item.get("raw_unit"),
                    "row_sha256": item.get("row_sha256"),
                }
            )
    conflicts: list[dict[str, Any]] = []
    entities_compared = 0
    for entity, observations in selected.items():
        if len(observations) != expected_sessions:
            continue
        entities_compared += 1
        distinct = {
            (
                str(item.get("value")),
                str(item.get("unit")),
                str(item.get("as_of_date")),
            )
            for item in observations
        }
        if len(distinct) > 1:
            conflicts.append(
                {
                    "origin": "final_report_table",
                    "claim": entity,
                    "as_of_date": target_date,
                    "observations": observations,
                }
            )
    coverage_complete = (
        len(runs) == expected_sessions
        and not gaps
        and entities_compared == len(expected_entities)
    )
    return {
        "target_date": target_date,
        "expected_sessions": expected_sessions,
        "expected_entities": list(expected_entities),
        "entities_compared": entities_compared,
        "coverage_complete": coverage_complete,
        "gaps": gaps,
        "conflicts": conflicts,
    }


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def codex_execution_peak(intervals: Iterable[dict[str, Any]]) -> int:
    events: list[tuple[datetime, int]] = []
    for interval in intervals:
        if not isinstance(interval, dict):
            continue
        started = parse_timestamp(
            interval.get("codex_started_at") or interval.get("slot_acquired_at")
        )
        finished = parse_timestamp(
            interval.get("codex_finished_at") or interval.get("slot_released_at")
        )
        if started and finished:
            events.extend(((started, 1), (finished, -1)))
    active = 0
    peak = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def serialize_run(run: ProcessRun) -> dict[str, Any]:
    return {
        "session_index": run.index,
        "pid": run.process.pid,
        "started_at": run.started_at,
        "finished_at": run.ended_at,
        "elapsed_seconds": run.elapsed_seconds,
        "exit_code": run.exit_code,
        "log_path": str(run.log_path),
        "result": run.result,
    }


def acceptance_summary(
    mode: str,
    runs: list[ProcessRun],
    wall_seconds: float,
    orphan_pids: list[int],
    *,
    observed_job_ids: set[str] | None = None,
    job_manifests: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    structured_conflicts: list[dict[str, Any]] | None = None,
    report_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected = 1 if mode == "single" else 3
    manifests = job_manifests or []
    numeric_conflicts = conflicts or []
    evidence_conflicts = structured_conflicts or []
    comparison_target_date = next(
        (
            str((run.result or {}).get("target_date"))
            for run in runs
            if (run.result or {}).get("target_date")
        ),
        "",
    )
    table_comparison = report_comparison or compare_report_common_values(
        runs,
        target_date=comparison_target_date,
        expected_sessions=expected,
    )
    process_starts = [run.started_monotonic for run in runs]
    process_start_span = max(process_starts) - min(process_starts) if process_starts else None
    job_intervals = [
        (started, finished)
        for run in runs
        if (started := parse_timestamp((run.result or {}).get("started_at")))
        and (finished := parse_timestamp((run.result or {}).get("finished_at")))
        and finished >= started
    ]
    job_starts = [started for started, _ in job_intervals]
    job_start_span = (
        (max(job_starts) - min(job_starts)).total_seconds()
        if len(job_starts) == expected
        else None
    )
    job_wall_seconds = (
        (max(finished for _, finished in job_intervals) - min(job_starts)).total_seconds()
        if len(job_intervals) == expected
        else None
    )
    job_elapsed = [
        float((run.result or {}).get("elapsed_seconds"))
        for run in runs
        if isinstance((run.result or {}).get("elapsed_seconds"), (int, float))
    ]
    if len(job_elapsed) != expected:
        job_elapsed = [run.elapsed_seconds or 0.0 for run in runs]
    concurrency_ratio = (
        sum(job_elapsed) / job_wall_seconds
        if job_wall_seconds is not None and job_wall_seconds > 0
        else 0.0
    )
    harness_wall_ratio = sum(job_elapsed) / wall_seconds if wall_seconds > 0 else 0.0
    statuses = [(run.result or {}).get("status") for run in runs]
    quality = [(run.result or {}).get("quality_gate_passed") for run in runs]
    work_item_counts = [(run.result or {}).get("work_item_count") for run in runs]
    source_counts = [
        (run.result or {}).get("http_sources_count", (run.result or {}).get("sources_count"))
        for run in runs
    ]
    codex_peaks = [(run.result or {}).get("active_codex_peak") for run in runs]
    initial_codex_peaks = [
        (run.result or {}).get("initial_codex_peak") for run in runs
    ]
    initial_codex_calls = [
        (run.result or {}).get("codex_initial_calls") for run in runs
    ]
    total_codex_calls = [(run.result or {}).get("codex_total_calls") for run in runs]
    codex_telemetry_complete = [
        (run.result or {}).get("codex_telemetry_complete") for run in runs
    ]
    artifact_verified = [(run.result or {}).get("artifact_verified") for run in runs]
    marker_verified = [(run.result or {}).get("marker_verified") for run in runs]
    opencode_tools_verified = [
        ((run.result or {}).get("opencode_tool_audit") or {}).get("passed")
        for run in runs
    ]
    coverage = [
        (run.result or {}).get("detailed_market_coverage_passed")
        for run in runs
    ]
    source_lists_verified = [
        (run.result or {}).get("source_urls_verified") for run in runs
    ]
    report_source_lists_verified = [
        (run.result or {}).get("report_source_urls_verified") for run in runs
    ]
    report_paths_verified = [
        (run.result or {}).get("report_path_verified") for run in runs
    ]
    result_job_ids = [
        str(job_id)
        for run in runs
        if isinstance((job_id := (run.result or {}).get("job_id")), str)
    ]
    durable_job_ids = observed_job_ids if observed_job_ids is not None else set(result_job_ids)
    manifest_job_ids = {
        str(item.get("job_id"))
        for item in manifests
        if isinstance(item.get("job_id"), str)
    }
    initial_pid_intervals_complete: list[bool] = []
    for run in runs:
        initial_runs = [
            item
            for item in ((run.result or {}).get("codex_runs", []) or [])
            if isinstance(item, dict) and item.get("initial_work_item") is True
        ]
        pids = {
            item.get("codex_pid")
            for item in initial_runs
            if isinstance(item.get("codex_pid"), int)
        }
        initial_pid_intervals_complete.append(
            len(initial_runs) >= 3
            and len(pids) >= 3
            and all(
                isinstance(item.get("codex_pid"), int)
                and parse_timestamp(item.get("codex_started_at")) is not None
                and parse_timestamp(item.get("codex_finished_at")) is not None
                for item in initial_runs
            )
        )
    codex_events: list[tuple[datetime, int]] = []
    for run in runs:
        for interval in (run.result or {}).get("codex_runs", []) or []:
            if not isinstance(interval, dict):
                continue
            started = parse_timestamp(
                interval.get("codex_started_at") or interval.get("slot_acquired_at")
            )
            finished = parse_timestamp(
                interval.get("codex_finished_at") or interval.get("slot_released_at")
            )
            if started and finished:
                codex_events.extend(((started, 1), (finished, -1)))
    active_codex = 0
    global_codex_peak = 0
    for _, delta in sorted(codex_events, key=lambda item: (item[0], item[1])):
        active_codex += delta
        global_codex_peak = max(global_codex_peak, active_codex)
    job_events = [
        event
        for started, finished in job_intervals
        for event in ((started, 1), (finished, -1))
    ]
    active_jobs = 0
    report_execution_peak = 0
    for _, delta in sorted(job_events, key=lambda item: (item[0], item[1])):
        active_jobs += delta
        report_execution_peak = max(report_execution_peak, active_jobs)
    stress_timing_ok = mode != "stress" or (
        job_start_span is not None
        and job_start_span <= 10.0
        and concurrency_ratio >= 2.0
        and report_execution_peak == 3
    )
    worker_peak_matches_mode = report_execution_peak == expected
    exact_jobs = (
        len(result_job_ids) == expected
        and len(set(result_job_ids)) == expected
        and durable_job_ids == set(result_job_ids)
    )
    manifests_complete = (
        len(manifests) == expected
        and manifest_job_ids == set(result_job_ids)
    )
    worker_manifest_intervals_complete = len(manifests) == expected and all(
        isinstance(item.get("worker_pid"), int)
        and isinstance(item.get("worker_interval"), dict)
        and parse_timestamp(item["worker_interval"].get("started_at")) is not None
        and parse_timestamp(item["worker_interval"].get("finished_at")) is not None
        for item in manifests
    )
    report_table_coverage_complete = table_comparison.get("coverage_complete") is True
    cross_report_comparison_performed = mode != "stress" or (
        manifests_complete and report_table_coverage_complete
    )
    cross_report_consistent = mode != "stress" or (
        cross_report_comparison_performed and not numeric_conflicts
    )
    return {
        "expected_sessions": expected,
        "actual_sessions": len(runs),
        "all_processes_exited_zero": len(runs) == expected
        and all(run.exit_code == 0 for run in runs),
        "all_results_auditable": len(runs) == expected
        and all(run.result is not None for run in runs),
        "all_sessions_created_exactly_one_job": exact_jobs,
        "current_run_job_ids": sorted(durable_job_ids),
        "all_job_manifests_present": manifests_complete,
        "all_worker_pid_intervals_recorded": worker_manifest_intervals_complete,
        "all_job_artifacts_verified": len(runs) == expected
        and all(item is True for item in artifact_verified),
        "all_marker_payloads_verified": len(runs) == expected
        and all(item is True for item in marker_verified),
        "all_opencode_sessions_followed_mcp_workflow": len(runs) == expected
        and all(item is True for item in opencode_tools_verified),
        "completed_reports": sum(status == "completed" for status in statuses),
        "all_reports_completed": len(runs) == expected
        and all(status == "completed" for status in statuses),
        "all_quality_gates_passed": len(runs) == expected
        and all(item is True for item in quality),
        "all_market_coverage_gates_passed": len(runs) == expected
        and all(item is True for item in coverage),
        "all_report_paths_current_and_present": len(runs) == expected
        and all(item is True for item in report_paths_verified),
        "all_reports_have_three_work_items": len(runs) == expected
        and all(item == 3 for item in work_item_counts),
        "all_reports_have_25_http_sources": len(runs) == expected
        and all(isinstance(item, (int, float)) and item >= 25 for item in source_counts),
        "all_report_source_lists_verified": len(runs) == expected
        and all(item is True for item in source_lists_verified),
        "all_report_citations_verified": len(runs) == expected
        and all(item is True for item in report_source_lists_verified),
        "all_reports_have_three_initial_codex_calls": len(runs) == expected
        and all(item == 3 for item in initial_codex_calls),
        "all_reports_codex_call_budget_respected": len(runs) == expected
        and all(isinstance(item, int) and 3 <= item <= 6 for item in total_codex_calls),
        "all_reports_initial_codex_peak_three": len(runs) == expected
        and all(item == 3 for item in initial_codex_peaks),
        "all_reports_codex_pid_intervals_complete": len(runs) == expected
        and all(initial_pid_intervals_complete),
        "all_codex_call_telemetry_complete": len(runs) == expected
        and all(item is True for item in codex_telemetry_complete),
        "all_reports_observed_three_codex_calls": len(runs) == expected
        and all(item == 3 for item in codex_peaks),
        "global_codex_execution_peak": global_codex_peak,
        "global_codex_peak_within_nine": bool(codex_events)
        and global_codex_peak <= 9,
        "process_start_span_seconds": round(process_start_span, 3)
        if process_start_span is not None
        else None,
        "job_start_span_seconds": round(job_start_span, 3)
        if job_start_span is not None
        else None,
        "job_wall_seconds": round(job_wall_seconds, 3)
        if job_wall_seconds is not None
        else None,
        "harness_wall_seconds": wall_seconds,
        "sum_job_elapsed_over_wall": round(concurrency_ratio, 3),
        "sum_job_elapsed_over_harness_wall": round(harness_wall_ratio, 3),
        "report_execution_peak": report_execution_peak,
        "global_worker_execution_peak": report_execution_peak,
        "worker_execution_peak_matches_mode": worker_peak_matches_mode,
        "stress_timing_passed": stress_timing_ok,
        "cross_report_comparison_performed": cross_report_comparison_performed,
        "final_report_common_value_coverage_complete": report_table_coverage_complete,
        "final_report_common_entities_compared": table_comparison.get(
            "entities_compared"
        ),
        "final_report_common_value_gaps": table_comparison.get("gaps", []),
        "cross_report_conflicts_count": len(numeric_conflicts),
        "structured_evidence_conflicts_count": len(evidence_conflicts),
        "final_report_conflicts_count": len(numeric_conflicts),
        "cross_report_numeric_consistency": cross_report_consistent,
        "orphan_pids": orphan_pids,
        "no_orphan_processes": not orphan_pids,
    }


def acceptance_passed(summary: dict[str, Any]) -> bool:
    return all(
        summary[key]
        for key in (
            "all_processes_exited_zero",
            "all_results_auditable",
            "all_sessions_created_exactly_one_job",
            "all_job_manifests_present",
            "all_worker_pid_intervals_recorded",
            "all_job_artifacts_verified",
            "all_marker_payloads_verified",
            "all_opencode_sessions_followed_mcp_workflow",
            "all_reports_completed",
            "all_quality_gates_passed",
            "all_market_coverage_gates_passed",
            "all_report_paths_current_and_present",
            "all_reports_have_three_work_items",
            "all_reports_have_25_http_sources",
            "all_report_source_lists_verified",
            "all_report_citations_verified",
            "all_reports_have_three_initial_codex_calls",
            "all_reports_codex_call_budget_respected",
            "all_reports_initial_codex_peak_three",
            "all_reports_codex_pid_intervals_complete",
            "all_codex_call_telemetry_complete",
            "all_reports_observed_three_codex_calls",
            "global_codex_peak_within_nine",
            "worker_execution_peak_matches_mode",
            "stress_timing_passed",
            "cross_report_comparison_performed",
            "final_report_common_value_coverage_complete",
            "cross_report_numeric_consistency",
            "no_orphan_processes",
        )
    )


def build_manifest(
    *,
    layout: Layout,
    mode: str,
    model: str,
    target_date: str,
    timezone_name: str,
    prompts: list[str],
    status: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": layout.run_id,
        "mode": mode,
        "status": status,
        "created_at": iso_now(),
        "project_root": str(layout.project_root),
        "target_date": target_date,
        "timezone": timezone_name,
        "model": model,
        "prompt_sha256": [sha256_text(prompt) for prompt in prompts],
        "profile": {
            "retriever": "tavily,codex",
            "language": "chinese",
            "total_words": 6000,
            "smart_token_limit": 16000,
            "codex_mode": "search",
            "codex_model": "gpt-5.5",
            "codex_reasoning_effort": "medium",
            "codex_service_tier": "fast",
            "codex_max_results_per_call": 12,
            "codex_transient_retries": 1,
            "codex_retry_delay_seconds": 2,
            "max_concurrent_reports": 3,
            "max_queued_reports": 9,
            "codex_initial_calls_per_report": 3,
            "codex_max_calls_per_report": 6,
            "codex_simultaneous_per_report": 3,
            "codex_global_simultaneous": 9,
            "search_retriever_per_worker": 4,
            "scraper_per_worker": 5,
            "min_http_sources_per_work_item": 8,
            "min_total_http_sources": 25,
            "job_timeout_seconds": 2700,
            "retrieval_timeout_seconds": 750,
            "writer_timeout_seconds": 450,
            "judge_timeout_seconds": 120,
            "stage_attempts": {"retrieval": 2, "writer": 2, "judge": 2},
        },
        "paths": {
            "runtime_dir": str(layout.runtime_dir),
            "artifact_dir": str(layout.artifact_dir),
            "log_dir": str(layout.log_dir),
            "jobs_dir": str(layout.jobs_dir),
            "reports_dir": str(layout.reports_dir),
            "opencode_config": str(layout.config_path),
            "event_log": str(layout.event_log_path),
        },
        "sessions": [],
        "job_manifests": [],
        "cost": {
            "total": None,
            "per_report": [],
            "scope": "GPT Researcher LLM callbacks; Codex CLI cost unavailable",
        },
        "cross_report_conflicts": [],
        "acceptance": None,
    }


def revalidate_existing_manifest(
    source_path: Path, output_path: Path | None = None
) -> int:
    """Re-evaluate an immutable completed run under the current gate policy."""

    source_path = source_path.expanduser().resolve()
    raw = source_path.read_text(encoding="utf-8")
    source = json.loads(raw)
    if not isinstance(source, dict):
        raise ValueError("source manifest must be a JSON object")
    mode = str(source.get("mode") or "")
    if mode not in {"single", "stress"}:
        raise ValueError("source manifest mode must be single or stress")
    sessions = source.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ValueError("source manifest has no sessions")

    runs: list[SimpleNamespace] = []
    report_hashes: list[dict[str, Any]] = []
    for position, session in enumerate(sessions, 1):
        if not isinstance(session, dict) or not isinstance(session.get("result"), dict):
            raise ValueError("source manifest session is not auditable")
        result = dict(session["result"])
        started = parse_timestamp(session.get("started_at"))
        if started is None:
            raise ValueError("source manifest session start is invalid")
        runs.append(
            SimpleNamespace(
                index=int(session.get("session_index") or position),
                started_monotonic=started.timestamp(),
                exit_code=session.get("exit_code"),
                elapsed_seconds=session.get("elapsed_seconds"),
                result=result,
            )
        )
        report_path_value = result.get("report_path")
        if not isinstance(report_path_value, str) or not report_path_value:
            raise ValueError("source manifest session has no report path")
        report_path = Path(report_path_value).expanduser().resolve()
        report_raw = report_path.read_text(encoding="utf-8")
        report_hashes.append(
            {
                "session_index": runs[-1].index,
                "job_id": result.get("job_id"),
                "path": str(report_path),
                "sha256": sha256_text(report_raw),
            }
        )

    job_manifests = source.get("job_manifests")
    if not isinstance(job_manifests, list):
        raise ValueError("source manifest job_manifests is invalid")
    observed_job_ids = {
        str(run.result.get("job_id"))
        for run in runs
        if isinstance(run.result.get("job_id"), str)
    }
    target_date = str(source.get("target_date") or "")
    report_comparison = compare_report_common_values(
        runs,
        target_date=target_date,
        expected_sessions=1 if mode == "single" else 3,
    )
    structured_conflicts = source.get("structured_evidence_conflicts") or []
    if not isinstance(structured_conflicts, list):
        structured_conflicts = []
    final_conflicts = report_comparison.get("conflicts", []) or []
    source_acceptance = source.get("acceptance") or {}
    orphan_pids = source_acceptance.get("orphan_pids", [])
    if not isinstance(orphan_pids, list):
        orphan_pids = []
    acceptance = acceptance_summary(
        mode,
        runs,
        float(source.get("wall_seconds") or 0.0),
        [pid for pid in orphan_pids if isinstance(pid, int)],
        observed_job_ids=observed_job_ids,
        job_manifests=job_manifests,
        conflicts=final_conflicts,
        structured_conflicts=structured_conflicts,
        report_comparison=report_comparison,
    )
    status = "completed" if acceptance_passed(acceptance) else "failed"
    payload = {
        "schema_version": 1,
        "status": status,
        "revalidated_at": iso_now(),
        "policy": (
            "Structured evidence conflicts remain audit records; final report "
            "table conflicts govern cross-report acceptance."
        ),
        "source_manifest": {
            "path": str(source_path),
            "sha256": sha256_text(raw),
            "original_status": source.get("status"),
            "run_id": source.get("run_id"),
            "mode": mode,
            "target_date": target_date,
        },
        "report_hashes": report_hashes,
        "structured_evidence_conflicts": structured_conflicts,
        "final_report_value_comparison": report_comparison,
        "acceptance": acceptance,
    }
    destination = (
        output_path.expanduser().resolve()
        if output_path is not None
        else source_path.with_name("revalidation.json")
    )
    atomic_write_json(destination, payload)
    print(
        json.dumps(
            {
                "status": status,
                "revalidation_path": str(destination),
                "source_manifest_sha256": payload["source_manifest"]["sha256"],
                "acceptance": acceptance,
            },
            ensure_ascii=False,
        )
    )
    return 0 if status == "completed" else 1


def static_validate_config(layout: Layout, env: dict[str, str], opencode_bin: str | None) -> dict[str, Any]:
    config = json.loads(layout.config_path.read_text(encoding="utf-8"))
    command = config["mcp"]["gpt-researcher-codex-long"]["command"]
    expected_prefix = ["run", "--directory", str(layout.project_root)]
    if command[1:4] != expected_prefix:
        raise ValueError("MCP command must use uv run --directory <checkout>")
    profile = config["mcp"]["gpt-researcher-codex-long"]["environment"]
    expected = {
        "CODEX_SEARCH_MODE": "search",
        "CODEX_SEARCH_REASONING_EFFORT": "medium",
        "CODEX_SEARCH_SERVICE_TIER": "fast",
        "CODEX_SEARCH_MAX_RESULTS": "12",
        "CODEX_SEARCH_RETRIEVER_RETRIES": "1",
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "3",
        "CODEX_SEARCH_GLOBAL_CONCURRENCY": "9",
        "MCP_RESEARCH_MAX_CONCURRENT_JOBS": "3",
        "MCP_RESEARCH_MAX_QUEUED_JOBS": "9",
        "RESEARCH_MIN_STOCK_SOURCES_PER_MARKET": "4",
        "LANGUAGE": "chinese",
        "TOTAL_WORDS": "6000",
    }
    mismatches = {
        key: {"expected": value, "actual": profile.get(key)}
        for key, value in expected.items()
        if profile.get(key) != value
    }
    if mismatches:
        raise ValueError(f"profile mismatch: {mismatches}")
    result: dict[str, Any] = {"json": "passed", "profile": "passed", "opencode": "skipped"}
    if opencode_bin:
        completed = subprocess.run(
            [opencode_bin, "debug", "config"],
            cwd=layout.project_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        result["opencode"] = "passed" if completed.returncode == 0 else "failed"
        result["opencode_exit_code"] = completed.returncode
        if completed.returncode != 0:
            result["opencode_output_tail"] = completed.stdout.decode(
                "utf-8", errors="replace"
            )[-2000:]
            raise RuntimeError("opencode rejected the generated config")
    return result


def run_harness(args: argparse.Namespace) -> int:
    if args.revalidate_manifest:
        output = Path(args.revalidation_output) if args.revalidation_output else None
        return revalidate_existing_manifest(Path(args.revalidate_manifest), output)
    if psutil is None and not args.dry_run:
        raise RuntimeError("psutil is required for process-tree cleanup; run through uv")
    project_root = Path(args.project_root).expanduser().resolve()
    if not (project_root / "pyproject.toml").is_file():
        raise FileNotFoundError(f"not a project root: {project_root}")
    if args.model.startswith("deepseek/") and not os.environ.get("DEEPSEEK_API_KEY"):
        deepseek_key = dotenv_values(project_root / ".env").get("DEEPSEEK_API_KEY")
        if deepseek_key:
            os.environ["DEEPSEEK_API_KEY"] = str(deepseek_key)
    run_id = args.run_id or f"{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    base_dir = Path(args.base_dir).expanduser().resolve() if args.base_dir else None
    layout = make_layout(project_root, run_id, base_dir)
    initialize_layout(layout)
    event_log = EventLog(layout.event_log_path)
    uv_bin = args.uv_bin or shutil.which("uv")
    opencode_bin = args.opencode_bin or shutil.which("opencode")
    if not uv_bin:
        raise FileNotFoundError("uv was not found on PATH")
    if not args.dry_run and not opencode_bin:
        raise FileNotFoundError("opencode was not found on PATH")
    if (
        not args.dry_run
        and args.model.startswith("deepseek/")
        and not os.environ.get("DEEPSEEK_API_KEY")
    ):
        raise RuntimeError("DEEPSEEK_API_KEY is required for the default OpenCode model")

    tz = ZoneInfo(args.timezone)
    current_date = datetime.now(tz).date()
    target_date = args.target_date or str(current_date - timedelta(days=1))
    session_count = 1 if args.mode == "single" else 3
    prompts = [market_prompt(target_date, args.timezone, index) for index in range(1, session_count + 1)]
    config = build_opencode_config(layout, uv_bin)
    atomic_write_json(layout.config_path, config)
    for index, prompt in enumerate(prompts, 1):
        (layout.runtime_dir / f"session-{index}.prompt.md").write_text(
            prompt, encoding="utf-8"
        )

    env = harness_environment(layout)
    manifest = build_manifest(
        layout=layout,
        mode=args.mode,
        model=args.model,
        target_date=target_date,
        timezone_name=args.timezone,
        prompts=prompts,
        status="dry_run" if args.dry_run else "running",
    )
    manifest["current_date"] = str(current_date)
    atomic_write_json(layout.manifest_path, manifest)
    event_log.emit("harness_initialized", run_id=run_id, mode=args.mode, dry_run=args.dry_run)
    validation = static_validate_config(layout, env, opencode_bin)
    manifest["static_validation"] = validation
    atomic_write_json(layout.manifest_path, manifest)
    if args.dry_run:
        event_log.emit("dry_run_completed", manifest_path=str(layout.manifest_path))
        print(json.dumps({"status": "dry_run", "manifest_path": str(layout.manifest_path)}))
        return 0

    serve_process: subprocess.Popen[bytes] | None = None
    serve_log_handle: Any | None = None
    runs: list[ProcessRun] = []
    tracked_pids: set[int] = set()
    wall_started = time.monotonic()
    failure: str | None = None
    try:
        attach_url: str | None = None
        if args.mode == "stress":
            port = args.port or free_tcp_port()
            serve_log_path = layout.log_dir / "opencode-serve.log"
            serve_log_handle = serve_log_path.open("wb")
            serve_process = subprocess.Popen(
                [
                    opencode_bin,
                    "serve",
                    "--pure",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--print-logs",
                    "--log-level",
                    "INFO",
                ],
                cwd=project_root,
                stdin=subprocess.DEVNULL,
                stdout=serve_log_handle,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            tracked_pids.add(serve_process.pid)
            event_log.emit("serve_started", pid=serve_process.pid, port=port)
            wait_for_tcp(port, serve_process, args.serve_start_timeout)
            attach_url = f"http://127.0.0.1:{port}"
            event_log.emit("serve_ready", pid=serve_process.pid, url=attach_url)

        for index, prompt in enumerate(prompts, 1):
            command = [
                opencode_bin,
                "run",
                "--pure",
                "--model",
                args.model,
                "--format",
                "json",
                "--title",
                f"GPT Researcher market acceptance {run_id} #{index}",
                "--dir",
                str(project_root),
            ]
            if attach_url:
                command.extend(["--attach", attach_url])
            command.append(prompt)
            run = start_run(
                index=index,
                command=command,
                env=env,
                log_path=layout.log_dir / f"session-{index}.jsonl",
            )
            runs.append(run)
            tracked_pids.add(run.process.pid)
            event_log.emit("session_started", session_index=index, pid=run.process.pid)

        deadline = wall_started + args.timeout
        while any(run.process.poll() is None for run in runs):
            if serve_process is not None and serve_process.poll() is not None:
                raise RuntimeError(
                    f"persistent opencode server exited with status {serve_process.returncode}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"hard harness timeout reached after {args.timeout}s")
            time.sleep(0.25)
        for run in runs:
            run.finish()
            event_log.emit(
                "session_finished",
                session_index=run.index,
                exit_code=run.exit_code,
                status=(run.result or {}).get("status"),
            )
    except (Exception, KeyboardInterrupt) as exc:
        failure = f"{type(exc).__name__}: {exc}"
        event_log.emit("harness_failure", error=failure)
    finally:
        for run in runs:
            if run.process.poll() is None:
                terminate_process_tree(run.process, tracked_pids)
            if not run.log_handle.closed:
                run.exit_code = run.process.poll()
                run.ended_at = iso_now()
                run.ended_monotonic = time.monotonic()
                run.log_handle.close()
                run.marker_result = extract_result_marker(run.log_path)
                run.result = run.marker_result
        terminate_process_tree(serve_process, tracked_pids)
        if serve_log_handle is not None:
            serve_log_handle.close()

    wall_seconds = round(time.monotonic() - wall_started, 3)
    reconcile_job_artifacts(
        runs,
        layout,
        event_log,
        target_date=target_date,
        timezone_name=args.timezone,
    )
    for run in runs:
        tool_audit = audit_opencode_tool_calls(
            run.log_path,
            job_id=(run.result or {}).get("job_id"),
            target_date=target_date,
            timezone_name=args.timezone,
        )
        if run.result is not None:
            run.result["opencode_tool_audit"] = tool_audit
        event_log.emit(
            "opencode_tool_calls_audited",
            session_index=run.index,
            passed=tool_audit.get("passed"),
            errors=tool_audit.get("errors", []),
        )
    job_manifests = load_current_job_manifests(runs, layout, event_log)
    expected_process_starts = tracked_job_processes(runs, job_manifests, tracked_pids)
    orphan_pids = live_tracked_pids(tracked_pids, expected_process_starts)
    evidence_conflicts = compare_numeric_evidence(job_manifests)
    report_comparison = compare_report_common_values(
        runs,
        target_date=target_date,
        expected_sessions=session_count,
    )
    report_conflicts = report_comparison["conflicts"]
    conflicts = evidence_conflicts + report_conflicts
    observed_job_ids = current_job_ids(layout)
    acceptance = acceptance_summary(
        args.mode,
        runs,
        wall_seconds,
        orphan_pids,
        observed_job_ids=observed_job_ids,
        job_manifests=job_manifests,
        conflicts=report_conflicts,
        structured_conflicts=evidence_conflicts,
        report_comparison=report_comparison,
    )
    result_by_session = {run.index: run.result or {} for run in runs}
    manifest.update(
        {
            "status": "completed" if failure is None and acceptance_passed(acceptance) else "failed",
            "finished_at": iso_now(),
            "wall_seconds": wall_seconds,
            "failure": failure,
            "serve_pid": serve_process.pid if serve_process is not None else None,
            "sessions": [serialize_run(run) for run in runs],
            "job_manifests": [
                {
                    "session_index": item.get("_harness_session_index"),
                    "job_id": item.get("job_id"),
                    "path": result_by_session.get(
                        int(item.get("_harness_session_index", 0) or 0), {}
                    ).get("manifest_path"),
                    "prompt_sha256": item.get("prompt_sha256"),
                    "target_date": item.get("target_date"),
                    "timezone": item.get("timezone"),
                    "worker_pid": item.get("worker_pid"),
                    "worker_interval": item.get("worker_interval"),
                    "report_path": item.get("report_path"),
                    "codex_pids": item.get("codex_pids"),
                    "codex_runs": item.get("codex_runs"),
                    "codex_initial_calls": item.get("codex_initial_calls"),
                    "codex_total_calls": item.get("codex_total_calls"),
                    "active_codex_peak": item.get("active_codex_peak"),
                    "http_sources_count": item.get("http_sources_count"),
                    "sources": item.get("sources"),
                    "total_cost_usd": item.get("total_cost_usd"),
                    "work_item_count": item.get("work_item_count"),
                    "work_items": item.get("work_items"),
                    "stage_attempts": item.get("stage_attempts"),
                    "quality_gate_passed": item.get("quality_gate_passed"),
                    "coverage_audit": item.get("coverage_audit"),
                    "evidence_conflicts": item.get("evidence_conflicts"),
                }
                for item in job_manifests
            ],
            "cost": {
                "total": sum(
                    float((run.result or {}).get("cost"))
                    for run in runs
                    if isinstance((run.result or {}).get("cost"), (int, float))
                )
                if any(isinstance((run.result or {}).get("cost"), (int, float)) for run in runs)
                else None,
                "per_report": [(run.result or {}).get("cost") for run in runs],
                "scope": "GPT Researcher LLM callbacks; Codex CLI cost unavailable",
            },
            "cross_report_conflicts": conflicts,
            "structured_evidence_conflicts": evidence_conflicts,
            "final_report_value_comparison": report_comparison,
            "cross_report_comparison_performed": acceptance[
                "cross_report_comparison_performed"
            ],
            "acceptance": acceptance,
        }
    )
    atomic_write_json(layout.manifest_path, manifest)
    event_log.emit(
        "harness_finished",
        status=manifest["status"],
        manifest_path=str(layout.manifest_path),
        orphan_pids=orphan_pids,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "manifest_path": str(layout.manifest_path),
                "acceptance": acceptance,
            },
            ensure_ascii=False,
        )
    )
    return 0 if manifest["status"] == "completed" else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("single", "stress"), default="single")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project-root", default=str(project_root))
    parser.add_argument("--base-dir", help="Override the root used for generated artifacts")
    parser.add_argument("--run-id")
    parser.add_argument("--model", default="deepseek/deepseek-v4-pro")
    parser.add_argument("--timezone", default="Asia/Singapore")
    parser.add_argument("--target-date", help="YYYY-MM-DD; defaults to yesterday in --timezone")
    parser.add_argument("--timeout", type=float, default=3000.0)
    parser.add_argument("--serve-start-timeout", type=float, default=30.0)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--uv-bin")
    parser.add_argument("--opencode-bin")
    parser.add_argument(
        "--revalidate-manifest",
        help="Read-only revalidation of an existing immutable harness manifest",
    )
    parser.add_argument("--revalidation-output")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.serve_start_timeout <= 0:
        parser.error("timeouts must be positive")
    return args


def main() -> int:
    try:
        return run_harness(parse_args())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
