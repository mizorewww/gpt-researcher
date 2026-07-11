from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


PREFIX = "gpt-researcher-codex-long_"
PROFILE = PREFIX + "profile_info"
START = PREFIX + "research_report_start"
STATUSES = PREFIX + "research_reports_status"
RESULT = PREFIX + "research_report_result"
FORBIDDEN_STATUS = PREFIX + "research_report_status"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def decode_tool_output(state: dict[str, Any], label: str) -> dict[str, Any]:
    if state.get("status") != "completed":
        raise ValueError(f"{label} did not complete")
    raw = state.get("output")
    if not isinstance(raw, str):
        raise ValueError(f"{label} output is not JSON text")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} output is not an object")
    return value


def tool_calls(path: Path) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "tool_use":
                continue
            part = event.get("part")
            if not isinstance(part, dict) or not isinstance(part.get("tool"), str):
                continue
            state = part.get("state")
            calls.append(
                {
                    "tool": part["tool"],
                    "state": state if isinstance(state, dict) else {},
                    "line": line_number,
                }
            )
    return calls


def one(calls: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    matches = [call for call in calls if call["tool"] == tool]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {tool} call, observed {len(matches)}")
    return matches[0]


def validate_session(
    session: dict[str, Any], expected_input: dict[str, Any], jobs_root: Path
) -> dict[str, Any]:
    calls = tool_calls(Path(session["log_path"]))
    tools = [call["tool"] for call in calls]
    if FORBIDDEN_STATUS in tools:
        raise ValueError("single-job research_report_status is forbidden")
    unexpected_mcp = sorted(
        {
            tool
            for tool in tools
            if tool.startswith(PREFIX)
            and tool not in {PROFILE, START, STATUSES, RESULT}
        }
    )
    if unexpected_mcp:
        raise ValueError(f"unexpected market workflow MCP calls: {unexpected_mcp}")
    profile_call = one(calls, PROFILE)
    start_call = one(calls, START)
    result_call = one(calls, RESULT)
    status_calls = [call for call in calls if call["tool"] == STATUSES]
    if not status_calls:
        raise ValueError("research_reports_status long polling was not observed")
    if not (
        profile_call["line"]
        < start_call["line"]
        < status_calls[0]["line"]
        <= status_calls[-1]["line"]
        < result_call["line"]
    ):
        raise ValueError("MCP tool order does not match the required protocol")

    profile = decode_tool_output(profile_call["state"], PROFILE)
    expected_profile = {
        "CODEX_SEARCH_MODE": "search",
        "CODEX_SEARCH_REASONING_EFFORT": "medium",
        "CODEX_SEARCH_SERVICE_TIER": "fast",
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "3",
        "CODEX_SEARCH_GLOBAL_CONCURRENCY": "9",
        "MCP_RESEARCH_GLOBAL_CONCURRENCY": "3",
    }
    mismatches = {
        key: {"expected": value, "actual": profile.get(key)}
        for key, value in expected_profile.items()
        if profile.get(key) != value
    }
    if mismatches:
        raise ValueError(f"profile mismatch: {mismatches}")

    start_input = start_call["state"].get("input")
    if not isinstance(start_input, dict):
        raise ValueError("research_report_start input is missing")
    for key in ("query", "target_date", "timezone"):
        if start_input.get(key) != expected_input.get(key):
            raise ValueError(f"research_report_start {key} does not match canonical input")
    start_output = decode_tool_output(start_call["state"], START)
    job_id = start_output.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("research_report_start returned no job_id")

    final_status: dict[str, Any] | None = None
    for call in status_calls:
        status_input = call["state"].get("input")
        if not isinstance(status_input, dict):
            raise ValueError("research_reports_status input is missing")
        if status_input.get("job_ids") != [job_id] or status_input.get("wait_seconds") != 20:
            raise ValueError("research_reports_status must long-poll only the current job")
        status_output = decode_tool_output(call["state"], STATUSES)
        jobs = status_output.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError("research_reports_status output has no jobs array")
        matched = next(
            (
                value
                for value in jobs
                if isinstance(value, dict) and value.get("job_id") == job_id
            ),
            None,
        )
        if matched is None:
            raise ValueError("research_reports_status omitted the current job")
        final_status = matched
    if not isinstance(final_status, dict) or final_status.get("status") != "completed":
        raise ValueError("the final long-poll response is not completed")

    result_input = result_call["state"].get("input")
    if result_input != {"job_id": job_id, "include_report": False}:
        raise ValueError("research_report_result input is not the current lightweight result")
    durable = decode_tool_output(result_call["state"], RESULT)
    required_values = {
        "job_id": job_id,
        "status": "completed",
        "target_date": expected_input["target_date"],
        "timezone": expected_input["timezone"],
        "work_item_count": 3,
        "codex_initial_calls": 3,
        "active_codex_peak": 3,
        "quality_gate_passed": True,
    }
    for key, expected in required_values.items():
        if durable.get(key) != expected:
            raise ValueError(
                f"durable result mismatch for {key}: expected {expected!r}, got {durable.get(key)!r}"
            )
    sources = durable.get("http_sources_count")
    if not isinstance(sources, int) or sources < 25:
        raise ValueError("durable result has fewer than 25 HTTP sources")
    report_path = Path(str(durable.get("path", ""))).expanduser().resolve()
    job_manifest = Path(str(durable.get("manifest_path", ""))).expanduser().resolve()
    for path in (report_path, job_manifest):
        try:
            path.relative_to(jobs_root)
        except ValueError as exc:
            raise ValueError(f"durable artifact escapes this workflow run: {path}") from exc
        if not path.is_file():
            raise ValueError(f"durable artifact does not exist: {path}")

    marker = session.get("result")
    if not isinstance(marker, dict):
        raise ValueError("session marker result is missing")
    marker_values = {
        "status": "completed",
        "job_id": job_id,
        "source_count": sources,
        "quality_gate_passed": True,
        "work_item_count": 3,
        "active_codex_peak": 3,
        "target_date": expected_input["target_date"],
        "timezone": expected_input["timezone"],
    }
    for key, expected in marker_values.items():
        if marker.get(key) != expected:
            raise ValueError(f"marker value {key} is not bound to the durable result")
    artifacts = marker.get("artifacts")
    if not isinstance(artifacts, list) or not all(
        isinstance(item, str) for item in artifacts
    ):
        raise ValueError("marker artifacts must be path strings")
    artifact_paths = {str(Path(item).expanduser().resolve()) for item in artifacts}
    if artifact_paths != {
        str(report_path),
        str(job_manifest),
    }:
        raise ValueError("marker artifacts do not exactly match durable current-run paths")
    return {"job_id": job_id, "http_sources_count": sources}


def main() -> int:
    manifest_path = Path(os.environ["RESEARCH_WORKFLOW_MANIFEST"]).resolve()
    jobs_root = Path(os.environ["RESEARCH_WORKFLOW_JOBS_DIR"]).resolve()
    manifest = load_json(manifest_path)
    sessions = manifest.get("sessions")
    inputs = manifest.get("inputs")
    if not isinstance(sessions, list) or not isinstance(inputs, list):
        raise ValueError("runner manifest has no sessions or canonical inputs")
    if len(sessions) != len(inputs) or not sessions:
        raise ValueError("session/input cardinality mismatch")
    results = []
    for session, input_record in zip(sessions, inputs):
        if not isinstance(session, dict) or not isinstance(input_record, dict):
            raise ValueError("invalid session/input manifest entry")
        expected_input = load_json(Path(input_record["path"]))
        results.append(validate_session(session, expected_input, jobs_root))
    print(json.dumps({"status": "passed", "sessions": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
