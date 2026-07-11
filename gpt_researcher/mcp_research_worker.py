"""Isolated worker process used by :mod:`gpt_researcher.job_manager`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from gpt_researcher.job_manager import atomic_write_json, read_json, utc_now


_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+"
)
_OPENAI_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9/._-])sk-(?:proj-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)


def _redact(value: object) -> str:
    text = str(value)
    for key, secret in os.environ.items():
        if len(secret) >= 8 and any(
            marker in key.upper()
            for marker in ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
        ):
            text = text.replace(secret, "[REDACTED]")
    text = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1[REDACTED]", text)
    # A URL path that happens to begin with "sk-" is evidence, not an API key.
    # Requiring the token not to be adjacent to URL/path characters avoids corrupting
    # failure-audit source URLs while still redacting standalone bearer keys.
    text = _OPENAI_KEY_PATTERN.sub("[REDACTED]", text)
    return text


def _write_worker_status(job_dir: Path, **changes: Any) -> None:
    path = job_dir / "worker_status.json"
    current = read_json(path, {})
    if not isinstance(current, dict):
        current = {}
    current.update(changes)
    current["updated_at"] = utc_now()
    atomic_write_json(path, current)


async def run(job_dir: Path) -> int:
    spec = read_json(job_dir / "spec.json")
    if not isinstance(spec, dict):
        atomic_write_json(
            job_dir / "result.json",
            {"status": "failed", "error": "missing or invalid job spec"},
        )
        return 2

    os.environ.update(
        {
            "MCP_RESEARCH_JOB_ID": str(spec["job_id"]),
            "MCP_RESEARCH_JOB_DIR": str(job_dir),
            "MCP_RESEARCH_TARGET_DATE": str(spec.get("target_date") or ""),
            "MCP_RESEARCH_TIMEZONE": str(spec.get("timezone") or "UTC"),
        }
    )
    _write_worker_status(
        job_dir,
        phase="initializing",
        progress={"completed": 0, "total": 3},
        active_codex=0,
        worker_pid=os.getpid(),
    )

    # Import only inside the child so the coordinator never imports the heavy
    # research stack or shares its stdout/environment mutations.
    from gpt_researcher import mcp_profile_server

    mcp_profile_server.OUTPUT_DIR = job_dir
    try:
        result = await mcp_profile_server._run_research_report(
            query=str(spec["query"]),
            report_type=str(spec.get("report_type") or "research_report"),
            tone=str(spec.get("tone") or "objective"),
            report_source=str(spec.get("report_source") or "web"),
            target_date=str(spec.get("target_date") or "") or None,
            timezone=str(spec.get("timezone") or "UTC"),
        )
    except BaseException as exc:  # worker must persist a result even on cancellation/error
        error = f"{type(exc).__name__}: {_redact(exc)}"
        failure: dict[str, Any] | None = None
        if isinstance(exc, RuntimeError):
            try:
                parsed = json.loads(str(exc))
                if isinstance(parsed, dict):
                    failure = json.loads(_redact(json.dumps(parsed, ensure_ascii=False)))
            except json.JSONDecodeError:
                pass
        if failure:
            error = f"{type(exc).__name__}: {_redact(failure.get('reason') or 'research failed')}"
        atomic_write_json(
            job_dir / "result.json",
            {
                "status": "failed",
                "error": error,
                "failure": failure,
            },
        )
        _write_worker_status(job_dir, phase="failed", active_codex=0, error=error)
        print(error, file=sys.stderr, flush=True)
        return 1

    atomic_write_json(
        job_dir / "result.json",
        {"status": "completed", "result": result},
    )
    _write_worker_status(
        job_dir,
        phase="completed",
        progress={"completed": 3, "total": 3},
        active_codex=0,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one isolated MCP research job")
    parser.add_argument("--job-dir", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.job_dir.expanduser().resolve())))


if __name__ == "__main__":
    main()
