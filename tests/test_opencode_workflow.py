from __future__ import annotations

import json
import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from gpt_researcher.opencode_workflow import runner as workflow_runner
from gpt_researcher.opencode_workflow.cli import main as workflow_main
from gpt_researcher.opencode_workflow.config import load_workflow
from gpt_researcher.opencode_workflow.runner import (
    ProcessIdentity,
    ToolPermissionLogMonitor,
    _decode_marker,
    _kill_tracked_identities,
    _opencode_tool_audit,
    _run_preflight_command,
    _tool_budget_violations,
    run_workflow,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "research_workflows" / "_template"
MARKET_VALIDATOR_PATH = (
    PROJECT_ROOT / "research_workflows/market-daily/validators/market_report.py"
)

FAKE_OPENCODE = r"""#!/usr/bin/env python3
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
    audit_calls = int(os.environ.get("FAKE_TOOL_AUDIT_CALLS", "0"))
    if audit_calls:
        audit_tool = os.environ.get("FAKE_TOOL_AUDIT_NAME", "skill")
        audit_path = Path(os.environ["XDG_DATA_HOME"]) / "opencode/log/opencode.log"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a") as audit:
            for _ in range(audit_calls):
                audit.write(f"evaluated permission={audit_tool} pattern=*\n")
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
"""


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
        "market_report_validator", MARKET_VALIDATOR_PATH
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
    metadata["requires"]["env"] = sorted(set(metadata["requires"]["env"]) | set(names))
    path.write_text(json.dumps(metadata), encoding="utf-8")


def test_init_creates_native_opencode_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
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


def test_template_agents_contains_task_context_not_harness_protocol():
    agents_text = (TEMPLATE / "AGENTS.md").read_text(encoding="utf-8").casefold()

    for forbidden in (
        "profile_info",
        "research_report_",
        "poll",
        "subagent",
        "parallel",
        "tool call",
        "process tree",
        "final marker",
    ):
        assert forbidden not in agents_text


def test_generic_runner_contains_no_market_or_provider_special_cases():
    generic_source = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in (
            PROJECT_ROOT / "gpt_researcher/opencode_workflow/runner.py",
            PROJECT_ROOT / "gpt_researcher/opencode_workflow/config.py",
        )
    )

    for forbidden in ("market-daily", "yfinance_", "tavily_", "kospi", "^gspc"):
        assert forbidden not in generic_source


def test_result_marker_must_be_unique_and_the_final_complete_line():
    marker = "OPENCODE_WORKFLOW_RESULT_JSON:"
    valid = marker + ' {"status":"completed"}'
    assert _decode_marker("report\n" + valid, marker) == {"status": "completed"}
    assert _decode_marker(valid + "\ntrailing prose", marker) is None
    assert _decode_marker(valid + " trailing", marker) is None
    assert _decode_marker(valid + "\n" + valid, marker) is None


def test_nested_agent_tool_audit_uses_isolated_opencode_log(tmp_path: Path):
    log_path = tmp_path / "opencode/log/opencode.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "evaluated permission=yfinance_batch_download pattern=* "
        "action.permission=yfinance_*\n"
        "evaluated permission=tavily_tavily_search pattern=*\n",
        encoding="utf-8",
    )
    layout = SimpleNamespace(xdg_data=tmp_path)
    runs = [SimpleNamespace(tool_calls=("skill", "task", "yfinance_batch_download"))]

    audit = _opencode_tool_audit(
        layout,
        runs,
        ["skill", "task", "yfinance_*", "tavily_*"],
    )

    assert audit["passed"] is True
    assert audit["unauthorized"] == []
    assert audit["observed"] == [
        "skill",
        "task",
        "tavily_tavily_search",
        "yfinance_batch_download",
    ]
    assert audit["counts"] == {
        "tavily_tavily_search": 1,
        "yfinance_batch_download": 1,
    }


def test_tool_permission_monitor_fails_closed_on_rotation_and_truncation(
    tmp_path: Path,
):
    log_path = tmp_path / "opencode.log"
    log_path.write_text("preflight\n", encoding="utf-8")
    monitor = ToolPermissionLogMonitor(log_path)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("evaluated permission=first_tool pattern=*\n")
    rotated = tmp_path / "opencode.log.1"
    log_path.rename(rotated)
    log_path.write_text(
        "evaluated permission=second_tool pattern=*\n", encoding="utf-8"
    )

    counts = monitor.poll(final=True)

    assert counts == {"first_tool": 1, "second_tool": 1}
    assert monitor.coverage == "incomplete"
    assert "rotated" in str(monitor.error)

    truncate_path = tmp_path / "truncate.log"
    truncate_path.write_text("baseline padding " * 20, encoding="utf-8")
    truncated = ToolPermissionLogMonitor(truncate_path)
    with truncate_path.open("a", encoding="utf-8") as handle:
        handle.write("\nevaluated permission=before_truncate pattern=*\n")
    assert truncated.poll() == {"before_truncate": 1}
    truncate_path.write_text(
        "evaluated permission=after_truncate pattern=*\n", encoding="utf-8"
    )

    truncated_counts = truncated.poll(final=True)

    assert truncated_counts == {
        "after_truncate": 1,
        "before_truncate": 1,
    }
    assert truncated.coverage == "incomplete"
    assert "truncated" in str(truncated.error)


def test_tool_permission_monitor_deletion_and_missing_root_calls_are_incomplete(
    tmp_path: Path,
):
    log_path = tmp_path / "opencode.log"
    log_path.write_text("", encoding="utf-8")
    monitor = ToolPermissionLogMonitor(log_path)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("evaluated permission=skill pattern=*")
    assert monitor.poll(final=True) == {"skill": 1}

    deleted_path = tmp_path / "deleted.log"
    deleted_path.write_text("", encoding="utf-8")
    deleted = ToolPermissionLogMonitor(deleted_path)
    deleted_path.unlink()
    deleted.poll(final=True)
    assert deleted.coverage == "incomplete"
    assert "disappeared" in str(deleted.error)

    layout = SimpleNamespace(xdg_data=tmp_path)
    audit = _opencode_tool_audit(
        layout,
        [SimpleNamespace(tool_calls=("skill",))],
        ["skill"],
        {"skill": 2},
        1,
        {},
        "complete",
        None,
    )
    assert audit["passed"] is False
    assert audit["budgets"]["coverage"] == "incomplete"
    assert audit["budgets"]["missing_root_calls"] == {"skill": 1}

    untrusted = _opencode_tool_audit(
        layout,
        [SimpleNamespace(tool_calls=("skill",))],
        ["skill"],
        {"skill": 2},
        1,
        {"skill": 3},
        "incomplete",
        "permission log was truncated",
    )
    assert untrusted["budget_violations"] == []
    assert untrusted["untrusted_budget_violations"] == [
        {
            "pattern": "skill",
            "count": 3,
            "configured_per_replica": 2,
            "effective_limit": 2,
        }
    ]


def test_tool_budgets_are_per_replica_and_match_patterns():
    counts = {
        "task": 6,
        "yfinance_get_fast_info": 120,
        "yfinance_get_price_history": 61,
    }

    assert _tool_budget_violations(
        counts, {"task": 3, "yfinance_*": 90}, replicas=2
    ) == [
        {
            "pattern": "yfinance_*",
            "count": 181,
            "configured_per_replica": 90,
            "effective_limit": 180,
        }
    ]


def test_tool_budget_configuration_is_optional_and_validated(tmp_path: Path):
    workflow = scaffold(tmp_path)
    assert load_workflow(workflow).tool_call_budgets == {}

    metadata_path = workflow / "workflow.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["security"]["toolCallBudgets"] = {"skill": 4}
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert load_workflow(workflow).tool_call_budgets == {"skill": 4}

    metadata["security"]["toolCallBudgets"] = {"not-allowed_*": 4}
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="must also appear"):
        load_workflow(workflow)


def test_configured_budget_without_nested_audit_fails_closed(tmp_path: Path):
    workflow = scaffold(tmp_path)
    metadata_path = workflow / "workflow.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["security"]["toolCallBudgets"] = {"skill": 2}
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    manifest = run_workflow(
        workflow,
        inputs=['{"query":"require complete nested tool accounting"}'],
        run_id="missing-budget-audit",
        project_root=PROJECT_ROOT,
        base_dir=tmp_path / "artifacts",
        opencode_bin=str(fake_opencode(tmp_path)),
        timeout_seconds=5,
    )

    assert manifest["status"] == "failed"
    assert manifest["failure"] == "tool call budget audit unavailable"
    assert manifest["tool_audit"]["budgets"]["coverage"] == "unavailable"
    assert manifest["tool_audit"]["count_source"] == "root_session_jsonl"


def test_runtime_tool_budget_fails_closed_and_records_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workflow = scaffold(tmp_path)
    metadata_path = workflow / "workflow.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["security"]["toolCallBudgets"] = {"skill": 2}
    metadata["requires"]["env"].extend(
        ["FAKE_TOOL_AUDIT_CALLS", "FAKE_TOOL_AUDIT_NAME"]
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setenv("FAKE_TOOL_AUDIT_CALLS", "3")
    monkeypatch.setenv("FAKE_TOOL_AUDIT_NAME", "skill")

    manifest = run_workflow(
        workflow,
        inputs=['{"query":"exercise the generic tool budget","delay":2}'],
        run_id="tool-budget",
        project_root=PROJECT_ROOT,
        base_dir=tmp_path / "artifacts",
        opencode_bin=str(fake_opencode(tmp_path)),
        timeout_seconds=5,
    )

    assert manifest["status"] == "failed"
    assert "tool call budget exceeded" in manifest["failure"]
    assert manifest["tool_audit"]["count_source"] == "opencode_permission_log"
    assert manifest["tool_audit"]["counts"] == {"skill": 3}
    assert manifest["tool_audit"]["budget_violations"] == [
        {
            "pattern": "skill",
            "count": 3,
            "configured_per_replica": 2,
            "effective_limit": 2,
        }
    ]
    assert manifest["orphan_pids_detected"] == []


def test_load_rejects_command_shell_and_unknown_metadata(tmp_path: Path):
    workflow = scaffold(tmp_path)
    command = workflow / ".opencode/commands/run.md"
    command.write_text(
        command.read_text(encoding="utf-8") + "\n!`uname -a`\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="shell expansion"):
        load_workflow(workflow)

    command.write_text(
        command.read_text(encoding="utf-8").replace("!`uname -a`", ""), encoding="utf-8"
    )
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

    pid = int((Path(manifest["paths"]["runtime"]) / "fake-state/serve.pid").read_text())
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

    monkeypatch.setattr(workflow_runner, "_descendant_identities", interrupt_once)
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
    assert all(
        session["result"]["status"] == "completed" for session in manifest["sessions"]
    )
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
        (base / "outputs/workflows/test-workflow/old-version/manifest.json").read_text(
            encoding="utf-8"
        )
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


def test_market_workflow_is_task_context_plus_generic_mcp_capabilities():
    workflow = PROJECT_ROOT / "research_workflows/market-daily"
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in workflow.rglob("*")
        if path.is_file() and path.suffix in {".md", ".json", ".jsonc", ".py"}
    )
    metadata = json.loads((workflow / "workflow.json").read_text(encoding="utf-8"))

    assert "research_report_start" not in combined
    assert "research_reports_status" not in combined
    assert metadata["requires"]["mcp"] == ["yfinance", "tavily"]
    assert "yfinance_*" in metadata["security"]["allowedToolPatterns"]
    assert "tavily_*" in metadata["security"]["allowedToolPatterns"]
    assert not (PROJECT_ROOT / "scripts/opencode_market_report_harness.py").exists()
    assert not (PROJECT_ROOT / "scripts/opencode_stability_market_report.sh").exists()


def test_market_validator_checks_report_quality_not_tool_sequence(tmp_path: Path):
    validator = load_market_validator()
    report_path = tmp_path / "report.md"
    expected_input = {
        "query": "research the complete market session in sufficient detail",
        "target_date": "2026-07-10",
        "timezone": "Asia/Singapore",
    }
    coverage = " ".join(aliases[0] for aliases in validator.REQUIRED_COVERAGE.values())
    urls = "\n".join(
        f"- [source {index}](https://example.com/source-{index})"
        for index in range(1, 26)
    )
    report_path.write_text(
        "# 严肃市场日报\n"
        + coverage
        + "\n"
        + ("完整的价格、日期、单位、催化、基本面和风险分析。" * 250)
        + "\n"
        + urls,
        encoding="utf-8",
    )
    marker = {
        "status": "completed",
        "summary": "passed",
        "artifacts": [],
        "quality_gate_passed": True,
        "target_date": expected_input["target_date"],
        "timezone": expected_input["timezone"],
        "markets": ["US", "Japan", "Korea", "Hong Kong"],
        "stock_count": 16,
    }

    result = validator.validate_session(
        {
            "response_path": str(report_path),
            "result": marker,
            "tool_calls": ["skill", "yfinance_batch_download", "tavily_tavily_search"],
        },
        expected_input,
        ["skill", "task", "yfinance_batch_download", "tavily_tavily_search"],
    )
    assert result["source_count"] == 25
    assert result["evidence_classes"] == {
        "structured_market_data": True,
        "independent_web_evidence": True,
    }

    with pytest.raises(ValueError, match="both configured evidence classes"):
        validator.validate_session(
            {
                "response_path": str(report_path),
                "result": marker,
                "tool_calls": ["yfinance_batch_download"],
            },
            expected_input,
            ["task", "yfinance_batch_download"],
        )
