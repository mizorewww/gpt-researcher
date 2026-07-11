import importlib.util
import json
import os
import sys
import textwrap
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = PROJECT_ROOT / "scripts" / "opencode_market_report_harness.py"
SPEC = importlib.util.spec_from_file_location("opencode_market_report_harness", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


def complete_coverage_audit() -> dict[str, object]:
    return {
        "applicable": True,
        "passed": True,
        "missing_indices": [],
        "indices_without_two_direct_sources": [],
        "invalid_or_unverified_index_rows": [],
        "missing_commodities": [],
        "commodities_without_two_direct_sources": [],
        "invalid_or_unverified_commodity_rows": [],
        "distinct_stocks": 16,
        "stock_counts_by_market": {
            "US": 4,
            "Japan": 4,
            "Korea": 4,
            "Hong Kong": 4,
        },
        "selection_mix_by_market": {
            market: {"leaders": 2, "event_movers": 2}
            for market in ("US", "Japan", "Korea", "Hong Kong")
        },
        "deficient_markets": {},
        "deficient_selection_mix": {},
        "incomplete_stock_rows": [],
        "report_http_sources_count": 25,
        "minimum_report_http_sources": 25,
    }


def synthetic_acceptance_run(
    index: int,
    job_id: str,
    *,
    offset_seconds: int = 0,
    serialize_initial: bool = False,
) -> SimpleNamespace:
    started = datetime(2026, 7, 10, tzinfo=timezone.utc) + timedelta(
        seconds=offset_seconds
    )
    finished = started + timedelta(seconds=10)
    if serialize_initial:
        initial_runs = [
            {
                "initial_work_item": True,
                "codex_pid": 10000 + index * 10 + lane,
                "codex_started_at": (started + timedelta(seconds=lane)).isoformat(),
                "codex_finished_at": (
                    started + timedelta(seconds=lane + 1)
                ).isoformat(),
            }
            for lane in range(3)
        ]
        followups = [
            {
                "initial_work_item": False,
                "codex_pid": 20000 + index * 10 + lane,
                "codex_started_at": (started + timedelta(seconds=4)).isoformat(),
                "codex_finished_at": (started + timedelta(seconds=5)).isoformat(),
            }
            for lane in range(3)
        ]
    else:
        initial_runs = [
            {
                "initial_work_item": True,
                "codex_pid": 10000 + index * 10 + lane,
                "codex_started_at": (started + timedelta(seconds=1)).isoformat(),
                "codex_finished_at": (started + timedelta(seconds=5)).isoformat(),
            }
            for lane in range(3)
        ]
        followups = []
    common_values = [
        {
            "entity": entity,
            "value": str(1000 + entity_index),
            "unit": "USD/barrel"
            if entity in {"wti", "brent"}
            else "USD/troy ounce"
            if entity == "gold"
            else "USD/pound"
            if entity == "copper"
            else "index points",
            "as_of_date": "2026-07-09",
            "raw_value": str(1000 + entity_index),
            "raw_unit": None,
            "row_sha256": f"row-{entity}",
        }
        for entity_index, entity in enumerate(harness.NUMERIC_ENTITY_ALIASES)
    ]
    result = {
        "job_id": job_id,
        "status": "completed",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_seconds": 10.0,
        "target_date": "2026-07-09",
        "artifact_verified": True,
        "marker_verified": True,
        "opencode_tool_audit": {"passed": True},
        "quality_gate_passed": True,
        "detailed_market_coverage_passed": True,
        "report_path_verified": True,
        "work_item_count": 3,
        "http_sources_count": 25,
        "source_urls_verified": True,
        "report_source_urls_verified": True,
        "codex_initial_calls": 3,
        "codex_total_calls": 6 if followups else 3,
        "codex_telemetry_complete": True,
        "initial_codex_peak": 1 if serialize_initial else 3,
        "active_codex_peak": 3,
        "codex_runs": initial_runs + followups,
        "common_market_values": common_values,
    }
    return SimpleNamespace(
        index=index,
        started_monotonic=float(index),
        exit_code=0,
        elapsed_seconds=10.0,
        result=result,
    )


def test_layout_refuses_to_reuse_a_prior_run(tmp_path: Path) -> None:
    layout = harness.make_layout(PROJECT_ROOT, "same-run", tmp_path)
    harness.initialize_layout(layout)

    with pytest.raises(FileExistsError, match="refusing to reuse"):
        harness.make_layout(PROJECT_ROOT, "same-run", tmp_path)


def test_generated_config_uses_checkout_and_bounded_concurrency(tmp_path: Path) -> None:
    layout = harness.make_layout(PROJECT_ROOT, "config-run", tmp_path)
    config = harness.build_opencode_config(layout, "/usr/local/bin/uv")
    server = config["mcp"]["gpt-researcher-codex-long"]

    assert config["permission"] == {
        "*": "deny",
        "gpt-researcher-codex-long_*": "allow",
    }
    assert server["command"] == [
        "/usr/local/bin/uv",
        "run",
        "--directory",
        str(PROJECT_ROOT),
        "gpt-researcher",
    ]
    assert server["environment"] | {
        "CODEX_SEARCH_MODE": "search",
        "CODEX_SEARCH_REASONING_EFFORT": "medium",
        "CODEX_SEARCH_SERVICE_TIER": "fast",
    } == server["environment"]
    assert server["environment"]["MCP_RESEARCH_MAX_CONCURRENT_JOBS"] == "3"
    assert server["environment"]["MCP_RESEARCH_MAX_QUEUED_JOBS"] == "9"
    assert server["environment"]["CODEX_SEARCH_RETRIEVER_CONCURRENCY"] == "3"
    assert server["environment"]["CODEX_SEARCH_GLOBAL_CONCURRENCY"] == "9"
    assert server["environment"]["SEARCH_RETRIEVER_CONCURRENCY"] == "4"
    assert server["environment"]["MAX_SCRAPER_WORKERS"] == "5"
    assert server["environment"]["RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM"] == "8"
    assert server["environment"]["RESEARCH_MIN_TOTAL_HTTP_SOURCES"] == "25"
    assert server["environment"]["MCP_RESEARCH_MIN_HTTP_SOURCES"] == "25"
    assert server["environment"]["LANGUAGE"] == "chinese"
    assert server["environment"]["TOTAL_WORDS"] == "6000"


def test_stress_prompts_submit_one_report_and_use_batch_long_poll() -> None:
    prompts = [
        harness.market_prompt("2026-07-09", "Asia/Singapore", index)
        for index in range(1, 4)
    ]

    assert len(set(prompts)) == 1
    for prompt in prompts:
        assert prompt.count("research_report_start") == 1
        assert "research_reports_status(job_ids=[job_id], wait_seconds=20)" in prompt
        assert "不得使用 research_report_status" in prompt
        assert "research_report_result(job_id, include_report=false)" in prompt
        assert "严禁读取旧报告补写" in prompt
        assert "target_date='2026-07-09'" in prompt
        assert "timezone='Asia/Singapore'" in prompt
        assert "帮我调研昨天的股票市场" in prompt


def test_extracts_marker_nested_in_opencode_json_event(tmp_path: Path) -> None:
    result = {
        "job_id": "job-1",
        "status": "completed",
        "quality_gate_passed": True,
    }
    event = {
        "type": "text",
        "part": {"text": f"done\n{harness.RESULT_MARKER}{json.dumps(result)}"},
    }
    log_path = tmp_path / "session.jsonl"
    log_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    assert harness.extract_result_marker(log_path) == result


def test_cross_report_comparison_flags_conflicting_numeric_evidence() -> None:
    manifests = [
        {
            "_harness_session_index": 1,
            "evidence_items": [
                {
                    "claim": "S&P 500 close",
                    "value": 6200.1,
                    "unit": "points",
                    "as_of_date": "2026-07-09",
                    "source_url": "https://example.com/a",
                }
            ],
        },
        {
            "_harness_session_index": 2,
            "result": {
                "evidence_items": [
                    {
                        "claim": "S&P 500 close",
                        "value": 6201.2,
                        "unit": "points",
                        "as_of_date": "2026-07-09",
                        "source_url": "https://example.com/b",
                    }
                ]
            },
        },
    ]

    conflicts = harness.compare_numeric_evidence(manifests)

    assert len(conflicts) == 1
    assert conflicts[0]["claim"] == "sp500"
    assert {item["session_index"] for item in conflicts[0]["observations"]} == {1, 2}


def test_cross_report_comparison_normalizes_numeric_formatting() -> None:
    manifests = [
        {
            "_harness_session_index": 1,
            "evidence_items": [
                {
                    "claim": "S&P 500 close",
                    "value": "6,200.10",
                    "unit": "points",
                    "as_of_date": "2026-07-09",
                }
            ],
        },
        {
            "_harness_session_index": 2,
            "evidence_items": [
                {
                    "claim": "S&P 500 close",
                    "value": 6200.1,
                    "unit": "points",
                    "as_of_date": "2026-07-09",
                }
            ],
        },
    ]

    assert harness.compare_numeric_evidence(manifests) == []


def test_structured_comparison_ignores_nonnumeric_claim_values_and_splits_hstech() -> None:
    manifests = [
        {
            "_harness_session_index": 1,
            "evidence_items": [
                {
                    "claim": "KOSPI close",
                    "value": "not independently sourced for this date",
                    "unit": "index points",
                    "as_of_date": "2026-07-09",
                },
                {
                    "claim": "Hang Seng TECH close",
                    "value": 4731.56,
                    "unit": "index points",
                    "as_of_date": "2026-07-09",
                },
            ],
        },
        {
            "_harness_session_index": 2,
            "evidence_items": [
                {
                    "claim": "Hang Seng Index close",
                    "value": 24030.18,
                    "unit": "index points",
                    "as_of_date": "2026-07-09",
                }
            ],
        },
    ]

    assert harness.compare_numeric_evidence(manifests) == []


def test_detailed_coverage_rejects_shallow_pass_and_missing_stock() -> None:
    assert not harness.detailed_market_coverage_passed(
        {"applicable": True, "passed": True}
    )
    audit = complete_coverage_audit()
    assert harness.detailed_market_coverage_passed(audit)

    audit["stock_counts_by_market"]["Hong Kong"] = 3  # type: ignore[index]
    assert not harness.detailed_market_coverage_passed(audit)


def test_acceptance_rejects_extra_job_created_by_one_session() -> None:
    job_id = str(uuid.uuid4())
    extra_job_id = str(uuid.uuid4())
    run = synthetic_acceptance_run(1, job_id)

    summary = harness.acceptance_summary(
        "single",
        [run],
        10.0,
        [],
        observed_job_ids={job_id, extra_job_id},
        job_manifests=[{"job_id": job_id}],
        conflicts=[],
    )

    assert summary["all_sessions_created_exactly_one_job"] is False
    assert harness.acceptance_passed(summary) is False


def test_acceptance_requires_initial_codex_overlap_not_followup_overlap() -> None:
    job_id = str(uuid.uuid4())
    run = synthetic_acceptance_run(1, job_id, serialize_initial=True)

    summary = harness.acceptance_summary(
        "single",
        [run],
        10.0,
        [],
        observed_job_ids={job_id},
        job_manifests=[{"job_id": job_id}],
        conflicts=[],
    )

    assert summary["all_reports_observed_three_codex_calls"] is True
    assert summary["all_reports_initial_codex_peak_three"] is False
    assert harness.acceptance_passed(summary) is False


def test_stress_acceptance_records_worker_peak_and_fails_numeric_conflicts() -> None:
    job_ids = [str(uuid.uuid4()) for _ in range(3)]
    runs = [
        synthetic_acceptance_run(index, job_id)
        for index, job_id in enumerate(job_ids, 1)
    ]
    conflict = {
        "claim": "sp500",
        "unit": "points",
        "as_of_date": "2026-07-09",
        "observations": [],
    }

    summary = harness.acceptance_summary(
        "stress",
        runs,
        15.0,
        [],
        observed_job_ids=set(job_ids),
        job_manifests=[{"job_id": job_id} for job_id in job_ids],
        conflicts=[conflict],
    )

    assert summary["global_worker_execution_peak"] == 3
    assert summary["worker_execution_peak_matches_mode"] is True
    assert summary["global_codex_execution_peak"] == 9
    assert summary["cross_report_numeric_consistency"] is False
    assert harness.acceptance_passed(summary) is False


def test_structured_evidence_conflicts_are_recorded_but_final_tables_decide_pass() -> None:
    job_ids = [str(uuid.uuid4()) for _ in range(3)]
    runs = [
        synthetic_acceptance_run(index, job_id)
        for index, job_id in enumerate(job_ids, 1)
    ]
    conflict = {"claim": "gold", "observations": []}

    summary = harness.acceptance_summary(
        "stress",
        runs,
        15.0,
        [],
        observed_job_ids=set(job_ids),
        job_manifests=[{"job_id": job_id} for job_id in job_ids],
        conflicts=[],
        structured_conflicts=[conflict],
    )

    assert summary["structured_evidence_conflicts_count"] == 1
    assert summary["final_report_conflicts_count"] == 0
    assert summary["cross_report_numeric_consistency"] is True


def test_final_report_value_comparison_detects_conflict_and_missing_entity() -> None:
    job_ids = [str(uuid.uuid4()) for _ in range(3)]
    runs = [
        synthetic_acceptance_run(index, job_id)
        for index, job_id in enumerate(job_ids, 1)
    ]
    runs[1].result["common_market_values"][0]["value"] = "6201.2"
    runs[2].result["common_market_values"] = [
        item
        for item in runs[2].result["common_market_values"]
        if item["entity"] != "copper"
    ]

    comparison = harness.compare_report_common_values(
        runs,
        target_date="2026-07-09",
        expected_sessions=3,
    )

    assert comparison["coverage_complete"] is False
    assert any(
        gap["session_index"] == 3 and gap["entity"] == "copper"
        for gap in comparison["gaps"]
    )
    sp500 = next(item for item in comparison["conflicts"] if item["claim"] == "sp500")
    assert sp500["origin"] == "final_report_table"


def test_common_value_extraction_accepts_short_and_translated_index_labels() -> None:
    report = "\n".join(
        (
            "| Dow | 52,487.41 | +0.2% | 2026-07-09 |",
            "| Nasdaq | 26,206.89 | +1.3% | 2026-07-09 |",
            "| 东证指数 | 4,020.37 | +0.3% | 2026-07-09 |",
            "| Hang Seng | 24,030.18 | -0.7% | 2026-07-09 |",
            "| Hang Seng TECH | 4,731.56 | +0.01% | 2026-07-09 |",
        )
    )

    values = harness.extract_common_market_values(report, "2026-07-09")

    self_by_entity = {item["entity"]: item["value"] for item in values}
    assert self_by_entity == {
        "dow": "52487.41",
        "nasdaq": "26206.89",
        "topix": "4020.37",
        "hangseng": "24030.18",
        "hangsengtech": "4731.56",
    }


def test_report_source_extraction_handles_url_labels_and_cjk_prose() -> None:
    url = "https://apnews.com/article/example?b=2&a=1"
    report = f"事实见 [{url}]({url}))。另见 [AP]({url})。"

    sources = harness.report_http_sources(report)

    assert sources == ["https://apnews.com/article/example?a=1&b=2"]


def test_revalidation_keeps_source_immutable_and_uses_final_table_policy(
    tmp_path: Path,
) -> None:
    job_ids = [str(uuid.uuid4()) for _ in range(3)]
    runs = [
        synthetic_acceptance_run(index, job_id)
        for index, job_id in enumerate(job_ids, 1)
    ]
    sessions = []
    job_manifests = []
    for run in runs:
        report_path = tmp_path / f"report-{run.index}.md"
        rows = []
        for item in run.result["common_market_values"]:
            label = harness.NUMERIC_ENTITY_ALIASES[item["entity"]][0]
            if item["entity"] in {"wti", "brent", "gold", "copper"}:
                rows.append(
                    f"| {label} | {item['value']} | {item['unit']} | 2026-07-09 |"
                )
            else:
                rows.append(f"| {label} | {item['value']} | 2026-07-09 |")
        report_path.write_text("\n".join(rows), encoding="utf-8")
        run.result["report_path"] = str(report_path)
        sessions.append(
            {
                "session_index": run.index,
                "started_at": run.result["started_at"],
                "finished_at": run.result["finished_at"],
                "elapsed_seconds": run.elapsed_seconds,
                "exit_code": run.exit_code,
                "result": run.result,
            }
        )
        job_manifests.append(
            {
                "job_id": run.result["job_id"],
                "worker_pid": 30000 + run.index,
                "worker_interval": {
                    "started_at": run.result["started_at"],
                    "finished_at": run.result["finished_at"],
                },
            }
        )
    source = {
        "schema_version": 1,
        "run_id": "immutable-stress",
        "mode": "stress",
        "status": "failed",
        "target_date": "2026-07-09",
        "wall_seconds": 15.0,
        "sessions": sessions,
        "job_manifests": job_manifests,
        "structured_evidence_conflicts": [{"claim": "gold", "observations": []}],
        "acceptance": {"orphan_pids": []},
    }
    source_path = tmp_path / "manifest.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    original = source_path.read_bytes()
    output_path = tmp_path / "revalidation.json"

    exit_code = harness.revalidate_existing_manifest(source_path, output_path)
    revalidated = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert source_path.read_bytes() == original
    assert revalidated["status"] == "completed"
    assert revalidated["acceptance"]["structured_evidence_conflicts_count"] == 1
    assert revalidated["acceptance"]["final_report_conflicts_count"] == 0
    assert revalidated["acceptance"]["cross_report_numeric_consistency"] is True
    assert len(revalidated["report_hashes"]) == 3


def test_reconciliation_rejects_report_path_outside_current_job(tmp_path: Path) -> None:
    layout = harness.make_layout(PROJECT_ROOT, "old-report", tmp_path)
    harness.initialize_layout(layout)
    event_log = harness.EventLog(layout.event_log_path)
    job_id = str(uuid.uuid4())
    job_dir = layout.jobs_dir / job_id
    job_dir.mkdir()
    old_report = tmp_path / "prior-run-report.md"
    old_report.write_text("old report", encoding="utf-8")
    started = datetime(2026, 7, 10, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=10)
    sources = [f"https://source.test/{index}" for index in range(25)]
    codex_runs = [
        {
            "initial_work_item": True,
            "codex_pid": 30000 + lane,
            "codex_started_at": (started + timedelta(seconds=1)).isoformat(),
            "codex_finished_at": (started + timedelta(seconds=5)).isoformat(),
        }
        for lane in range(3)
    ]
    coverage = complete_coverage_audit()
    body = {
        "path": str(old_report),
        "http_sources_count": 25,
        "source_urls": sources,
        "total_cost_usd": 0.01,
        "work_item_count": 3,
        "codex_initial_calls": 3,
        "codex_total_calls": 3,
        "active_codex_peak": 3,
        "quality_gate_passed": True,
        "coverage_audit": coverage,
        "target_date": "2026-07-09",
        "timezone": "Asia/Singapore",
        "codex_pids": [item["codex_pid"] for item in codex_runs],
        "codex_runs": codex_runs,
    }
    spec = {
        "job_id": job_id,
        "query": harness.MARKET_QUERY_TEMPLATE,
        "target_date": "2026-07-09",
        "timezone": "Asia/Singapore",
    }
    status = {
        "job_id": job_id,
        "status": "completed",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "started_at_epoch": started.timestamp(),
        "finished_at_epoch": finished.timestamp(),
    }
    manifest = {
        "job_id": job_id,
        "status": "completed",
        "prompt_sha256": harness.sha256_text(harness.MARKET_QUERY_TEMPLATE),
        "target_date": "2026-07-09",
        "timezone": "Asia/Singapore",
        "report_path": str(old_report),
        "http_sources_count": 25,
        "sources": sources,
        "total_cost_usd": 0.01,
        "work_item_count": 3,
        "codex_initial_calls": 3,
        "codex_total_calls": 3,
        "active_codex_peak": 3,
        "quality_gate_passed": True,
        "coverage_audit": coverage,
        "codex_pids": [item["codex_pid"] for item in codex_runs],
        "codex_runs": codex_runs,
        "evidence_items": [],
    }
    artifacts = {
        "spec.json": spec,
        "status.json": status,
        "result.json": {"status": "completed", "result": body},
        "manifest.json": manifest,
    }
    for name, payload in artifacts.items():
        (job_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    marker = {
        "job_id": job_id,
        "status": "completed",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_seconds": 10.0,
        "report_path": str(old_report),
        "manifest_path": str(job_dir / "manifest.json"),
        "http_sources_count": 25,
        "cost": 0.01,
        "work_item_count": 3,
        "active_codex_peak": 3,
        "quality_gate_passed": True,
    }
    run = SimpleNamespace(index=1, marker_result=marker, result=marker)

    harness.reconcile_job_artifacts(
        [run],
        layout,
        event_log,
        target_date="2026-07-09",
        timezone_name="Asia/Singapore",
    )

    assert run.result["report_path_verified"] is False
    assert run.result["artifact_verified"] is False
    assert "report_path" in run.result["artifact_mismatches"]


def test_checked_in_mcp_profile_uses_uv_run_directory() -> None:
    config = json.loads((PROJECT_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["gpt-researcher-codex-long"]

    assert server["command"] == "uv"
    assert server["args"][:2] == ["run", "--directory"]
    assert "uvx" not in json.dumps(server)
    assert server["env"]["CODEX_SEARCH_MODE"] == "search"
    assert server["env"]["CODEX_SEARCH_REASONING_EFFORT"] == "medium"
    assert server["env"]["CODEX_SEARCH_SERVICE_TIER"] == "fast"


@pytest.mark.skipif(harness.psutil is None, reason="process cleanup requires psutil")
def test_fake_stress_run_uses_one_server_and_three_attached_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_opencode = tmp_path / "fake-opencode"
    fake_opencode.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import hashlib
            import json
            import os
            import signal
            import socket
            import sys
            import time
            import uuid
            from datetime import datetime, timezone
            from pathlib import Path

            args = sys.argv[1:]
            if args[:2] == ["debug", "config"]:
                raise SystemExit(0)
            if args and args[0] == "serve":
                port = int(args[args.index("--port") + 1])
                server = socket.socket()
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", port))
                server.listen()
                signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
                while True:
                    connection, _ = server.accept()
                    connection.close()
            if args and args[0] == "run":
                now = datetime.now(timezone.utc).isoformat()
                epoch = time.time()
                job_id = str(uuid.uuid4())
                job_dir = Path(os.environ["GPT_RESEARCHER_HARNESS_JOBS_DIR"]) / job_id
                job_dir.mkdir(parents=True)
                report_path = job_dir / "report.md"
                finished = datetime.fromtimestamp(epoch + 10, timezone.utc).isoformat()
                sources = [f"https://source.test/{{job_id}}/{{index}}" for index in range(25)]
                common_rows = [
                    "| S&P 500 | 6200.10 | 2026-07-09 |",
                    "| Dow Jones | 45000.20 | 2026-07-09 |",
                    "| Nasdaq Composite | 21000.30 | 2026-07-09 |",
                    "| Russell 2000 | 2300.40 | 2026-07-09 |",
                    "| Nikkei 225 | 41000.50 | 2026-07-09 |",
                    "| TOPIX | 2900.60 | 2026-07-09 |",
                    "| KOSPI | 3200.70 | 2026-07-09 |",
                    "| KOSDAQ | 900.80 | 2026-07-09 |",
                    "| Hang Seng Index | 25000.90 | 2026-07-09 |",
                    "| Hang Seng Tech | 5500.10 | 2026-07-09 |",
                    "| WTI | 74.20 | USD/barrel | 2026-07-09 |",
                    "| Brent | 78.30 | USD/barrel | 2026-07-09 |",
                    "| Gold | 2400.40 | USD/troy ounce | 2026-07-09 |",
                    "| Copper | 4.50 | USD/pound | 2026-07-09 |",
                ]
                report_path.write_text(
                    "fake report\\n" + "\\n".join(common_rows + sources)
                )
                coverage_audit = {{
                    "applicable": True,
                    "passed": True,
                    "missing_indices": [],
                    "indices_without_two_direct_sources": [],
                    "invalid_or_unverified_index_rows": [],
                    "missing_commodities": [],
                    "commodities_without_two_direct_sources": [],
                    "invalid_or_unverified_commodity_rows": [],
                    "distinct_stocks": 16,
                    "stock_counts_by_market": {{
                        "US": 4, "Japan": 4, "Korea": 4, "Hong Kong": 4
                    }},
                    "selection_mix_by_market": {{
                        market: {{"leaders": 2, "event_movers": 2}}
                        for market in ("US", "Japan", "Korea", "Hong Kong")
                    }},
                    "deficient_markets": {{}},
                    "deficient_selection_mix": {{}},
                    "incomplete_stock_rows": [],
                    "report_http_sources_count": 25,
                    "minimum_report_http_sources": 25,
                }}
                codex_runs = [
                    {{
                        "helper_pid": 800000 + (os.getpid() % 1000) * 10 + index,
                        "codex_pid": 900000 + (os.getpid() % 1000) * 10 + index,
                        "started_at": now,
                        "codex_started_at": now,
                        "codex_finished_at": finished,
                        "slot": index,
                        "initial_work_item": True,
                    }}
                    for index in range(3)
                ]
                result = {{
                    "job_id": job_id,
                    "status": "completed",
                    "started_at": now,
                    "finished_at": finished,
                    "elapsed_seconds": 10,
                    "report_path": str(report_path),
                    "manifest_path": str(job_dir / "manifest.json"),
                    "sources_count": 25,
                    "http_sources_count": 25,
                    "cost": 0.01,
                    "work_item_count": 3,
                    "active_codex_peak": 3,
                    "quality_gate_passed": True,
                    "coverage_audit": coverage_audit,
                }}
                body = {{
                    "path": str(report_path),
                    "http_sources_count": 25,
                    "source_urls": sources,
                    "total_cost_usd": 0.01,
                    "work_item_count": 3,
                    "codex_initial_calls": 3,
                    "codex_total_calls": 3,
                    "active_codex_peak": 3,
                    "quality_gate_passed": True,
                    "coverage_audit": coverage_audit,
                    "target_date": "2026-07-09",
                    "timezone": "Asia/Singapore",
                    "codex_pids": [item["codex_pid"] for item in codex_runs],
                    "codex_runs": codex_runs,
                }}
                (job_dir / "spec.json").write_text(json.dumps({{
                    "job_id": job_id,
                    "query": {json.dumps(harness.MARKET_QUERY_TEMPLATE)},
                    "target_date": "2026-07-09",
                    "timezone": "Asia/Singapore",
                }}))
                (job_dir / "status.json").write_text(json.dumps({{
                    "job_id": job_id,
                    "status": "completed",
                    "started_at": now,
                    "finished_at": finished,
                    "created_at_epoch": epoch,
                    "started_at_epoch": epoch,
                    "finished_at_epoch": epoch + 10,
                }}))
                (job_dir / "result.json").write_text(json.dumps({{"status": "completed", "result": body}}))
                (job_dir / "manifest.json").write_text(json.dumps({{
                    "version": 1,
                    "job_id": job_id,
                    "status": "completed",
                    "prompt_sha256": "{harness.sha256_text(harness.MARKET_QUERY_TEMPLATE)}",
                    "target_date": "2026-07-09",
                    "timezone": "Asia/Singapore",
                    "worker_pid": os.getpid(),
                    "worker_interval": {{"started_at": now, "finished_at": finished}},
                    "report_path": str(report_path),
                    "http_sources_count": 25,
                    "sources": sources,
                    "total_cost_usd": 0.01,
                    "work_item_count": 3,
                    "codex_initial_calls": 3,
                    "codex_total_calls": 3,
                    "active_codex_peak": 3,
                    "quality_gate_passed": True,
                    "coverage_audit": coverage_audit,
                    "codex_pids": [item["codex_pid"] for item in codex_runs],
                    "codex_runs": codex_runs,
                    "evidence_items": [],
                }}))
                session_id = f"fake-session-{{os.getpid()}}"
                def emit_tool(name, tool_input, output):
                    print(json.dumps({{
                        "type": "tool_use",
                        "sessionID": session_id,
                        "part": {{
                            "type": "tool",
                            "tool": "gpt-researcher-codex-long_" + name,
                            "state": {{
                                "status": "completed",
                                "input": tool_input,
                                "output": output,
                            }},
                        }},
                    }}), flush=True)
                emit_tool("profile_info", {{}}, {{
                    "CODEX_SEARCH_MODE": "search",
                    "CODEX_SEARCH_REASONING_EFFORT": "medium",
                    "CODEX_SEARCH_SERVICE_TIER": "fast",
                    "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "3",
                    "CODEX_SEARCH_GLOBAL_CONCURRENCY": "9",
                }})
                emit_tool("research_report_start", {{
                    "query": {json.dumps(harness.MARKET_QUERY_TEMPLATE)},
                    "target_date": "2026-07-09",
                    "timezone": "Asia/Singapore",
                }}, {{"job_id": job_id, "status": "queued"}})
                emit_tool("research_reports_status", {{
                    "job_ids": [job_id],
                    "wait_seconds": 20,
                }}, {{"jobs": [{{"job_id": job_id, "status": "completed"}}]}})
                emit_tool("research_report_result", {{
                    "job_id": job_id,
                    "include_report": False,
                }}, body)
                event = {{"type": "text", "part": {{"text": "{harness.RESULT_MARKER}" + json.dumps(result)}}}}
                print(json.dumps(event), flush=True)
                raise SystemExit(0)
            raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    fake_opencode.chmod(0o755)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-for-local-harness-test")
    args = harness.parse_args(
        [
            "--mode",
            "stress",
            "--project-root",
            str(PROJECT_ROOT),
            "--base-dir",
            str(tmp_path / "artifacts"),
            "--run-id",
            "fake-stress",
            "--target-date",
            "2026-07-09",
            "--timeout",
            "20",
            "--opencode-bin",
            str(fake_opencode),
            "--uv-bin",
            os.devnull,
        ]
    )

    assert harness.run_harness(args) == 0
    manifest_path = (
        tmp_path / "artifacts" / "outputs" / "stability" / "fake-stress" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert len(manifest["sessions"]) == 3
    assert manifest["acceptance"]["all_reports_completed"] is True
    assert manifest["acceptance"]["job_start_span_seconds"] <= 10
    assert manifest["acceptance"]["sum_job_elapsed_over_wall"] >= 2
    assert manifest["acceptance"]["no_orphan_processes"] is True
