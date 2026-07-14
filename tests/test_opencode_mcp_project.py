import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPEN_CODE_PROJECT = PROJECT_ROOT / "opencode" / "market-research-smoke"


def test_opencode_project_is_native_and_has_only_tool_mcps():
    config = json.loads((OPEN_CODE_PROJECT / "opencode.jsonc").read_text())

    assert set(config["mcp"]) == {
        "time",
        "gpt-researcher-codex-long",
        "yfinance",
    }
    assert config["model"] == "deepseek/deepseek-v4-pro"
    assert config["permission"]["time_*"] == "allow"
    assert "bash" not in config["permission"]
    assert not (
        PROJECT_ROOT / "gpt_researcher" / "opencode_workflow" / "runner.py"
    ).exists()
    assert not (PROJECT_ROOT / "scripts" / "research_workflow.sh").exists()
    assert not (PROJECT_ROOT / "research_workflows" / "workflow.schema.json").exists()


def test_saved_question_is_separate_from_generic_research_instructions():
    instructions = (OPEN_CODE_PROJECT / "AGENTS.md").read_text().lower()
    saved_question = (
        (OPEN_CODE_PROJECT / ".opencode/commands/research.md").read_text().lower()
    )

    assert "美国" in saved_question
    assert "大宗商品" in saved_question
    assert "昨天" in saved_question
    assert "三个" not in instructions
    assert not list((OPEN_CODE_PROJECT / ".opencode/agents").glob("*.md"))
    assert not list((OPEN_CODE_PROJECT / ".opencode/skills").glob("*/SKILL.md"))
    for required_tool_term in (
        "gpt-researcher-codex-long",
        "research_report",
        "yfinance",
        "time",
    ):
        assert required_tool_term in instructions
