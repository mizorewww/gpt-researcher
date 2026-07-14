import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from gpt_researcher.opencode_cli import (
    BUNDLED_TEMPLATE,
    PROJECT_ROOT,
    WorkflowError,
    _prefill_tui,
    create_workflow,
    discover_workflows,
    load_config,
    main,
    workflow_entry_prompt,
    workflow_summary,
    workflows_root,
)


@pytest.fixture
def workflows(tmp_path: Path) -> Path:
    root = tmp_path / "opencode"
    shutil.copytree(BUNDLED_TEMPLATE, root / "_template")
    return root


def test_new_workflow_copies_template_and_replaces_tokens(workflows: Path):
    created = create_workflow(workflows, "company-research", "_template")

    assert created == workflows / "company-research"
    assert "Company Research" in (created / "AGENTS.md").read_text()
    assert not list((created / ".opencode/agents").glob("*.md"))
    assert not list((created / ".opencode/skills").glob("*/SKILL.md"))
    assert load_config(created / "opencode.jsonc")["mcp"] == {}
    assert discover_workflows(workflows) == [created]

    with pytest.raises(WorkflowError, match="already exists"):
        create_workflow(workflows, "company-research", "_template")


def test_default_template_works_for_an_empty_custom_root(tmp_path: Path):
    custom_root = tmp_path / "personal-workflows"

    created = create_workflow(custom_root, "new-topic", "_template")

    assert created == custom_root / "new-topic"
    assert (created / "AGENTS.md").is_file()


def test_default_root_falls_back_to_bundled_workflows_outside_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)

    root = workflows_root()

    assert root == (PROJECT_ROOT / "opencode").resolve()
    assert "market-research-smoke" in {path.name for path in discover_workflows(root)}


def test_show_visualizes_prompt_mcps_and_optional_harness(workflows: Path):
    created = create_workflow(workflows, "vendor-review", "_template")
    config_path = created / "opencode.jsonc"
    config_path.write_text(
        """
        {
          // Workflow-owned tools
          "model": "provider/model",
          "mcp": {
            "deep-research": {"enabled": true},
          },
        }
        """,
        encoding="utf-8",
    )

    summary = workflow_summary(created)

    assert "Prompt: AGENTS.md" in summary
    assert "deep-research (enabled)" in summary
    assert "agents: none" in summary
    assert "skills: none" in summary


def test_cli_lists_and_open_starts_fresh_tui_with_prefilled_entry_command(
    workflows: Path, capsys: pytest.CaptureFixture[str]
):
    created = create_workflow(workflows, "policy-research", "_template")
    assert main(["--root", str(workflows), "list"]) == 0
    assert capsys.readouterr().out.strip() == "policy-research"

    with (
        patch("gpt_researcher.opencode_cli.shutil.which", return_value="/bin/opencode"),
        patch("gpt_researcher.opencode_cli._free_local_port", return_value=4567),
        patch("gpt_researcher.opencode_cli.os.chdir") as chdir,
        patch("gpt_researcher.opencode_cli.threading.Thread") as thread,
        patch("gpt_researcher.opencode_cli.subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        assert main(["--root", str(workflows), "open", "policy-research"]) == 0

    chdir.assert_called_once_with(created)
    saved_prompt = "在这里写打开工作流时需要自动填入输入框的完整调查问题。"
    thread.assert_called_once_with(
        target=_prefill_tui,
        args=(4567, created, saved_prompt),
        daemon=True,
    )
    thread.return_value.start.assert_called_once_with()
    run.assert_called_once_with(
        [
            "opencode",
            str(created),
            "--hostname",
            "127.0.0.1",
            "--port",
            "4567",
            "--pure",
        ],
        check=False,
    )


def test_workflow_entry_prompt_is_generic_and_requires_a_choice(workflows: Path):
    created = create_workflow(workflows, "policy-research", "_template")
    assert workflow_entry_prompt(created) == (
        "在这里写打开工作流时需要自动填入输入框的完整调查问题。"
    )

    commands = created / ".opencode/commands"
    (commands / "audit.md").write_text("Audit the request.", encoding="utf-8")
    with pytest.raises(WorkflowError, match="multiple entry prompts"):
        workflow_entry_prompt(created)
    assert workflow_entry_prompt(created, "audit") == "Audit the request."


def test_cli_rejects_path_traversal(workflows: Path):
    assert main(["--root", str(workflows), "show", "../escape"]) == 2
