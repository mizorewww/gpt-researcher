import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPEN_CODE_PROJECT = PROJECT_ROOT / "opencode" / "market-research-smoke"


def test_opencode_project_is_native_and_has_only_tool_mcps():
    config = json.loads((OPEN_CODE_PROJECT / "opencode.jsonc").read_text())

    assert set(config["mcp"]) == {"gpt-researcher-codex-long", "yfinance"}
    assert config["model"] == "deepseek/deepseek-v4-pro"
    assert not (
        PROJECT_ROOT / "gpt_researcher" / "opencode_workflow" / "runner.py"
    ).exists()
    assert not (PROJECT_ROOT / "scripts" / "research_workflow.sh").exists()
    assert not (PROJECT_ROOT / "research_workflows" / "workflow.schema.json").exists()


def test_task_context_is_separate_from_generic_orchestration():
    task_context = (OPEN_CODE_PROJECT / "AGENTS.md").read_text().lower()
    coordinator = (
        OPEN_CODE_PROJECT / ".opencode/agents/research-coordinator.md"
    ).read_text().lower()
    worker = (
        OPEN_CODE_PROJECT / ".opencode/agents/research-worker.md"
    ).read_text().lower()
    skill = (
        OPEN_CODE_PROJECT / ".opencode/skills/parallel-research/SKILL.md"
    ).read_text().lower()

    assert "美国" in task_context
    assert "大宗商品" in task_context
    for required_tool_term in (
        "gpt-researcher-codex-long",
        "research_report",
        "yfinance",
        "必须",
    ):
        assert required_tool_term in task_context
    generic_orchestration = "\n".join((coordinator, worker, skill))
    for workflow_specific_term in (
        "gpt-researcher-codex-long",
        "research_report",
        "yfinance",
        "股票",
        "市场日报",
        "nasdaq",
        "kospi",
        "expensive high-level",
    ):
        assert workflow_specific_term not in generic_orchestration
