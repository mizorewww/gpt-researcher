from __future__ import annotations

import json
import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path

import psutil
import pytest

from gpt_researcher.opencode_workflow import runner as workflow_runner
from gpt_researcher.opencode_workflow.cli import main as workflow_main
from gpt_researcher.opencode_workflow.config import load_workflow
from gpt_researcher.opencode_workflow.runner import (
    ProcessIdentity,
    _decode_marker,
    _kill_tracked_identities,
    _run_preflight_command,
    run_workflow,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "research_workflows" / "_template"
MARKET_VALIDATOR_PATH = (
    PROJECT_ROOT
    / "research_workflows/market-daily/validators/market_protocol.py"
)

FAKE_OPENCODE = r'''#!/usr/bin/env python3
import fcntl
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print(os.environ.get("FAKE_OPENCODE_VERSION", "1.17.15"))
    raise SystemExit(0)
if args[:2] == ["debug", "config"]:
    print("{}")
    raise SystemExit(0)
if args[:2] == ["debug", "skill"]:
    print(json.dumps([{"name": "evidence-triangulation", "location": "fake"}]))
    raise SystemExit(0)
if args[:2] == ["agent", "list"]:
    print("research-coordinator (primary)")
    print("source-researcher (subagent)")
    print("evidence-auditor (subagent)")
    raise SystemExit(0)
if args[:2] == ["mcp", "list"]:
    print("gpt-researcher-codex-long connected")
    raise SystemExit(0)
if args and args[0] == "serve":
    port = int(args[args.index("--port") + 1])
    state = Path(os.environ["RESEARCH_WORKFLOW_RUNTIME_DIR"]) / "fake-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "serve.pid").write_text(str(os.getpid()))
    running = True
    def stop(*_):
        global running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if os.environ.get("FAKE_SERVE_NEVER_LISTEN") == "1":
        while running:
            time.sleep(0.02)
        raise SystemExit(0)
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen()
    server.settimeout(0.05)
    while running:
        try:
            connection, _ = server.accept()
            connection.close()
        except socket.timeout:
            pass
    server.close()
    raise SystemExit(0)
if args and args[0] == "run":
    state = Path(os.environ["RESEARCH_WORKFLOW_RUNTIME_DIR"]) / "fake-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / f"env-{os.getpid()}.json").write_text(json.dumps(sorted(os.environ)))
    lock_path = state / "lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        counter_path = state / "counter.json"
        counter = json.loads(counter_path.read_text()) if counter_path.exists() else {"active": 0, "peak": 0}
        counter["active"] += 1
        counter["peak"] = max(counter["peak"], counter["active"])
        counter_path.write_text(json.dumps(counter))
        payload = json.loads(args[-1])
        with (state / "inputs.jsonl").open("a") as inputs:
            inputs.write(json.dumps(payload, sort_keys=True) + "\n")
        fcntl.flock(lock, fcntl.LOCK_UN)
    time.sleep(float(payload.get("delay", os.environ.get("FAKE_OPENCODE_DELAY", "0.2"))))
    tool = os.environ.get("FAKE_OPENCODE_TOOL", "skill")
    print(json.dumps({"type": "tool_use", "sessionID": f"fake-{os.getpid()}", "part": {"tool": tool}}))
    marker = os.environ.get("FAKE_OPENCODE_MARKER", "OPENCODE_WORKFLOW_RESULT_JSON:")
    result = {
        "status": os.environ.get("FAKE_RESULT_STATUS", "completed"),
        "summary": "fake research complete",
        "artifacts": [],
        "source_count": 3,
    }
    text = marker + " " + json.dumps(result)
    print(json.dumps({"type": "text", "sessionID": f"fake-{os.getpid()}", "part": {"text": text}}))
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        counter = json.loads(counter_path.read_text())
        counter["active"] -= 1
        counter_path.write_text(json.dumps(counter))
    raise SystemExit(int(os.environ.get("FAKE_OPENCODE_EXIT", "0")))
raise SystemExit(3)
'''


def scaffold(tmp_path: Path, name: str = "test-workflow") -> Path:
    destination = tmp_path / name
    shutil.copytree(TEMPLATE, destination)
    for path in destination.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("__WORKFLOW_NAME__", name), encoding="utf-8")
    return destination


def fake_opencode(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-opencode"
    executable.write_text(FAKE_OPENCODE, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def load_market_validator():
    module_spec = importlib.util.spec_from_file_location(
        "market_protocol_validator", MARKET_VALIDATOR_PATH
    )
    assert module_spec and module_spec.loader
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")


def require_env(workflow: Path, *names: str) -> None:
    path = workflow / "workflow.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["requires"]["env"] = sorted(
        set(metadata["requires"]["env"]) | set(names)
    )
    path.write_text(json.dumps(metadata), encoding="utf-8")


def test_init_creates_native_opencode_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    destination = tmp_path / "company-research"
    exit_code = workflow_main(
        [
            "--project-root",
            str(PROJECT_ROOT),
            "init",
            "company-research",
            "--destination",
            str(destination),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "created"
    assert (destination / "AGENTS.md").is_file()
    assert (destination / "opencode.jsonc").is_file()
    assert (destination / ".opencode/commands/run.md").is_file()
    assert (destination / ".opencode/agents/research-coordinator.md").is_file()
    assert (destination / ".opencode/skills/evidence-triangulation/SKILL.md").is_file()
    assert (destination / "schemas/workflow.schema.json").is_file()
    assert load_workflow(destination).name == "company-research"


def test_init_and_run_ids_reject_path_traversal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    escaped = tmp_path / "escaped"
    exit_code = workflow_main(
        [
            "--project-root",
            str(tmp_path),
            "init",
            "../../escaped",
        ]
    )
    assert exit_code == 2
    assert not escaped.exists()
    assert "lowercase kebab-case" in capsys.readouterr().err

    workflow = scaffold(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        run_workflow(
            workflow,
            inputs=['{"query":"reject traversal before creating paths"}'],
            run_id="../../escaped",
            project_root=PROJECT_ROOT,
            base_dir=tmp_path / "artifacts",
            opencode_bin=str(fake_opencode(tmp_path)),
            dry_run=True,
        )


def test_workflow_metadata_does_not_duplicate_model_or_mcp(tmp_path: Path):
    workflow = scaffold(tmp_path)
    data = json.loads((workflow / "workflow.json").read_text(encoding="utf-8"))

    assert "model" not in data
    assert "mcp" not in data
    assert "prompt" not in data
    config = (workflow / "opencode.jsonc").read_text(encoding="utf-8")
    assert '"model": "deepseek/deepseek-v4-pro"' in config
    assert '"mcp"' in config


def test_result_marker_must_be_unique_and_the_final_complete_line():
    marker = "OPENCODE_WORKFLOW_RESULT_JSON:"
    valid = marker + ' {"status":"completed"}'
    assert _decode_marker("report\n" + valid, marker) == {"status": "completed"}
    assert _decode_marker(valid + "\ntrailing prose", marker) is None
    assert _decode_marker(valid + " trailing", marker) is None
    assert _decode_marker(valid + "\n" + valid, marker) is None


def test_load_rejects_command_shell_and_unknown_metadata(tmp_path: Path):
    workflow = scaffold(tmp_path)
    command = workflow / ".opencode/commands/run.md"
    command.write_text(command.read_text(encoding="utf-8") + "\n!`uname -a`\n", encoding="utf-8")
    with pytest.raises(ValueError, match="shell expansion"):
        load_workflow(workflow)

    command.write_text(command.read_text(encoding="utf-8").replace("!`uname -a`", ""), encoding="utf-8")
    metadata = json.loads((workflow / "workflow.json").read_text(encoding="utf-8"))
    metadata["model"] = "not-allowed-here"
    (workflow / "workflow.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected|unknown keys"):
        load_workflow(workflow)


def test_dry_run_preflights_agents_skills_mcp_and_snapshots(tmp_path: Path):
    workflow = scaffold(tmp_path)
    binary = fake_opencode(tmp_path)
    manifest = run_workflow(
        workflow,
        inputs=['{"query":"test the workflow"}'],
        replicas=1,
        run_id="dry-run",
        project_root=PROJECT_ROOT,
        base_dir=tmp_path / "artifacts",
        opencode_bin=str(binary),
        dry_run=True,
    )

    assert manifest["status"] == "dry_run"
    assert manifest["preflight"]["entry_agent"]["mode"] == "primary"
    assert manifest["preflight"]["mcp"]["status"] == "passed"
    assert manifest["preflight"]["skills"] == ["evidence-triangulation"]
    assert manifest["workflow"]["sha256"]
    assert "AGENTS.md" in manifest["workflow"]["files"]
    assert manifest["snapshot_integrity"]["passed"] is True
    snapshot = Path(manifest["workflow"]["snapshot_path"])
    assert PROJECT_ROOT not in snapshot.parents
    assert snapshot.stat().st_mode & 0o777 == 0o500
    assert (snapshot / "AGENTS.md").stat().st_mode & 0o777 == 0o400
    assert Path(manifest["paths"]["manifest"]).stat().st_mode & 0o777 == 0o600
    serialized = json.dumps(manifest)
    assert "test-deepseek-key" not in serialized
    assert "test-tavily-key" not in serialized


def test_runtime_stays_outside_a_foreign_git_worktree(tmp_path: Path):
    workflow = scaffold(tmp_path / "workflow-source")
    foreign_repo = tmp_path / "foreign-repo"
    (foreign_repo / ".git").mkdir(parents=True)
    manifest = run_workflow(
        workflow,
        inputs=['{"query":"isolate this runtime"}'],
        run_id="foreign-base",
        project_root=PROJECT_ROOT,
        base_dir=foreign_repo,
        opencode_bin=str(fake_opencode(tmp_path)),
        dry_run=True,
    )

    runtime = Path(manifest["paths"]["runtime"])
    assert foreign_repo not in runtime.parents
    assert runtime.resolve().is_relative_to(
        Path(workflow_runner.tempfile.gettempdir()).resolve()
    )


def test_server_start_timeout_cleans_the_unreturned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workflow = scaffold(tmp_path)
    require_env(workflow, "FAKE_SERVE_NEVER_LISTEN")
    monkeypatch.setenv("FAKE_SERVE_NEVER_LISTEN", "1")
    manifest = run_workflow(
        workflow,
        inputs=['{"query":"timeout cleanup"}'],
        replicas=3,
        run_id="serve-timeout",
        project_root=PROJECT_ROOT,
        base_dir=tmp_path / "artifacts",
        opencode_bin=str(fake_opencode(tmp_path)),
        serve_start_timeout=0.05,
        timeout_seconds=2,
    )

    pid = int(
        (Path(manifest["paths"]["runtime"]) / "fake-state/serve.pid").read_text()
    )
    assert manifest["status"] == "failed"
    assert "TimeoutError" in str(manifest["failure"])
    assert not psutil.pid_exists(pid)


def test_preflight_keyboard_interrupt_cleans_its_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pid_path = tmp_path / "preflight.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import os,time; from pathlib import Path; "
            f"Path({str(pid_path)!r}).write_text(str(os.getpid())); time.sleep(60)"
        ),
    ]
    original_descendants = workflow_runner._descendant_identities
    interrupted = False

    def interrupt_once(pid: int):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            time.sleep(0.1)
            raise KeyboardInterrupt
        return original_descendants(pid)

    monkeypatch.setattr(
        workflow_runner, "_descendant_identities", interrupt_once
    )
    with pytest.raises(KeyboardInterrupt):
        _run_preflight_command(command, tmp_path, os.environ.copy(), timeout=5)

    pid = int(pid_path.read_text())
    assert not psutil.pid_exists(pid)


def test_pid_identity_prevents_recycled_pid_kill():
    current = psutil.Process()
    recycled_identity = ProcessIdentity(
        pid=current.pid,
        create_time=current.create_time() + 1,
    )

    assert _kill_tracked_identities([recycled_identity]) == []
    assert current.is_running()


def test_three_replicas_overlap_and_keep_distinct_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workflow = scaffold(tmp_path)
    binary = fake_opencode(tmp_path)
    monkeypatch.setenv("HOST_ONLY_SECRET", "must-not-reach-opencode")
    inputs = [
        json.dumps({"query": f"research lane {index}", "delay": delay})
        for index, delay in enumerate((0.05, 0.2, 0.4), 1)
    ]
    manifest = run_workflow(
        workflow,
        inputs=inputs,
        replicas=3,
        run_id="three-way",
        project_root=PROJECT_ROOT,
        base_dir=tmp_path / "artifacts",
        opencode_bin=str(binary),
        timeout_seconds=10,
    )

    assert manifest["status"] == "completed"
    assert manifest["session_execution_peak"] == 3
    assert manifest["orphan_pids"] == []
    assert len(manifest["sessions"]) == 3
    assert all(session["result"]["status"] == "completed" for session in manifest["sessions"])
    assert all(session["tool_calls"] == ["skill"] for session in manifest["sessions"])
    state_dir = Path(manifest["paths"]["runtime"]) / "fake-state"
    state = json.loads((state_dir / "counter.json").read_text())
    assert state == {"active": 0, "peak": 3}
    captured = {
        json.loads(line)["query"]
        for line in (state_dir / "inputs.jsonl").read_text().splitlines()
    }
    assert captured == {"research lane 1", "research lane 2", "research lane 3"}
    elapsed = [session["elapsed_seconds"] for session in manifest["sessions"]]
    assert max(elapsed) - min(elapsed) > 0.2
    for env_path in state_dir.glob("env-*.json"):
        names = json.loads(env_path.read_text(encoding="utf-8"))
        assert "HOST_ONLY_SECRET" not in names
        assert "DEEPSEEK_API_KEY" in names


def test_unauthorized_tool_call_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workflow = scaffold(tmp_path)
    binary = fake_opencode(tmp_path)
    require_env(workflow, "FAKE_OPENCODE_TOOL")
    monkeypatch.setenv("FAKE_OPENCODE_TOOL", "bash")

    manifest = run_workflow(
        workflow,
        inputs=['{"query":"attempt an unauthorized tool"}'],
        run_id="unauthorized",
        project_root=PROJECT_ROOT,
        base_dir=tmp_path / "artifacts",
        opencode_bin=str(binary),
        timeout_seconds=5,
    )

    assert manifest["status"] == "failed"
    assert "unauthorized tool calls" in " ".join(manifest["sessions"][0]["errors"])


def test_explicit_failed_result_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workflow = scaffold(tmp_path)
    binary = fake_opencode(tmp_path)
    require_env(workflow, "FAKE_RESULT_STATUS")
    monkeypatch.setenv("FAKE_RESULT_STATUS", "failed")

    manifest = run_workflow(
        workflow,
        inputs=['{"query":"research with an audited failure"}'],
        run_id="failed-result",
        project_root=PROJECT_ROOT,
        base_dir=tmp_path / "artifacts",
        opencode_bin=str(binary),
        timeout_seconds=5,
    )

    assert manifest["status"] == "failed"
    assert "result status is not completed" in " ".join(
        manifest["sessions"][0]["errors"]
    )


def test_input_schema_is_enforced_before_opencode_run(tmp_path: Path):
    workflow = scaffold(tmp_path)
    binary = fake_opencode(tmp_path)
    with pytest.raises(ValueError, match="input 1 failed schema validation"):
        run_workflow(
            workflow,
            inputs=['{"not_query":"missing"}'],
            run_id="bad-input",
            project_root=PROJECT_ROOT,
            base_dir=tmp_path / "artifacts",
            opencode_bin=str(binary),
            dry_run=True,
        )


def test_preflight_failure_is_written_to_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workflow = scaffold(tmp_path)
    binary = fake_opencode(tmp_path)
    base = tmp_path / "artifacts"
    require_env(workflow, "FAKE_OPENCODE_VERSION")
    monkeypatch.setenv("FAKE_OPENCODE_VERSION", "1.0.0")

    with pytest.raises(RuntimeError, match="below required"):
        run_workflow(
            workflow,
            inputs=['{"query":"preflight must fail"}'],
            run_id="old-version",
            project_root=PROJECT_ROOT,
            base_dir=base,
            opencode_bin=str(binary),
            dry_run=True,
        )

    manifest = json.loads(
        (
            base
            / "outputs/workflows/test-workflow/old-version/manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"
    assert "below required" in manifest["failure"]


def test_market_input_enforces_real_date_format(tmp_path: Path):
    with pytest.raises(ValueError, match="input 1 failed schema validation"):
        run_workflow(
            PROJECT_ROOT / "research_workflows/market-daily",
            inputs=[
                json.dumps(
                    {
                        "query": "investigate the complete market session in detail",
                        "target_date": "not-a-date",
                        "timezone": "Asia/Singapore",
                    }
                )
            ],
            run_id="bad-market-date",
            project_root=PROJECT_ROOT,
            base_dir=tmp_path / "artifacts",
            opencode_bin=str(fake_opencode(tmp_path)),
            dry_run=True,
        )


def test_market_validator_binds_marker_to_exact_mcp_protocol(tmp_path: Path):
    validator = load_market_validator()
    jobs_root = tmp_path / "jobs"
    job_id = "job-1"
    job_dir = jobs_root / job_id
    job_dir.mkdir(parents=True)
    report_path = job_dir / "report.md"
    job_manifest = job_dir / "manifest.json"
    report_path.write_text("report", encoding="utf-8")
    job_manifest.write_text("{}", encoding="utf-8")
    expected_input = {
        "query": "research the complete market session in sufficient detail",
        "target_date": "2026-07-10",
        "timezone": "Asia/Singapore",
    }
    durable = {
        "job_id": job_id,
        "status": "completed",
        "path": str(report_path),
        "manifest_path": str(job_manifest),
        "http_sources_count": 25,
        "work_item_count": 3,
        "codex_initial_calls": 3,
        "active_codex_peak": 3,
        "quality_gate_passed": True,
        "target_date": expected_input["target_date"],
        "timezone": expected_input["timezone"],
    }

    def event(tool: str, input_value: dict, output_value: dict) -> str:
        return json.dumps(
            {
                "type": "tool_use",
                "part": {
                    "tool": tool,
                    "state": {
                        "status": "completed",
                        "input": input_value,
                        "output": json.dumps(output_value),
                    },
                },
            }
        )

    log_path = tmp_path / "session.jsonl"
    log_path.write_text(
        "\n".join(
            [
                event(
                    validator.PROFILE,
                    {},
                    {
                        "CODEX_SEARCH_MODE": "search",
                        "CODEX_SEARCH_REASONING_EFFORT": "medium",
                        "CODEX_SEARCH_SERVICE_TIER": "fast",
                        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "3",
                        "CODEX_SEARCH_GLOBAL_CONCURRENCY": "9",
                        "MCP_RESEARCH_GLOBAL_CONCURRENCY": "3",
                    },
                ),
                event(
                    validator.START,
                    expected_input,
                    {"job_id": job_id, "status": "queued"},
                ),
                event(
                    validator.STATUSES,
                    {"job_ids": [job_id], "wait_seconds": 20},
                    {"jobs": [{"job_id": job_id, "status": "completed"}]},
                ),
                event(
                    validator.RESULT,
                    {"job_id": job_id, "include_report": False},
                    durable,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    marker = {
        "status": "completed",
        "summary": "passed",
        "artifacts": [str(report_path), str(job_manifest)],
        "source_count": 25,
        "job_id": job_id,
        "quality_gate_passed": True,
        "work_item_count": 3,
        "active_codex_peak": 3,
        "target_date": expected_input["target_date"],
        "timezone": expected_input["timezone"],
    }

    result = validator.validate_session(
        {"log_path": str(log_path), "result": marker}, expected_input, jobs_root
    )
    assert result == {"job_id": job_id, "http_sources_count": 25}

    empty_log = tmp_path / "empty.jsonl"
    empty_log.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one"):
        validator.validate_session(
            {"log_path": str(empty_log), "result": marker},
            expected_input,
            jobs_root,
        )
