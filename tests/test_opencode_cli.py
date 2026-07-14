import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from gpt_researcher.opencode_cli import (
    BUNDLED_TEMPLATE,
    WorkflowError,
    create_workflow,
    discover_workflows,
    load_config,
    main,
    open_workflow,
    workflow_summary,
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
    assert (created / ".opencode/agents/research-coordinator.md").is_file()
    assert load_config(created / "opencode.jsonc")["mcp"] == {}
    assert discover_workflows(workflows) == [created]

    with pytest.raises(WorkflowError, match="already exists"):
        create_workflow(workflows, "company-research", "_template")


def test_default_template_works_for_an_empty_custom_root(tmp_path: Path):
    custom_root = tmp_path / "personal-workflows"

    created = create_workflow(custom_root, "new-topic", "_template")

    assert created == custom_root / "new-topic"
    assert (created / "AGENTS.md").is_file()


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
    assert "research-coordinator" in summary
    assert "parallel-research" in summary


def test_cli_lists_and_open_execs_opencode_in_workflow_directory(
    workflows: Path, capsys: pytest.CaptureFixture[str]
):
    created = create_workflow(workflows, "policy-research", "_template")
    assert main(["--root", str(workflows), "list"]) == 0
    assert capsys.readouterr().out.strip() == "policy-research"

    with (
        patch("gpt_researcher.opencode_cli.shutil.which", return_value="/bin/opencode"),
        patch("gpt_researcher.opencode_cli.os.chdir") as chdir,
        patch("gpt_researcher.opencode_cli.os.execvp") as execvp,
    ):
        open_workflow(created, web=True, pure=True)

    chdir.assert_called_once_with(created)
    execvp.assert_called_once_with("opencode", ["opencode", "web", "--pure"])


def test_cli_rejects_path_traversal(workflows: Path):
    assert main(["--root", str(workflows), "show", "../escape"]) == 2
