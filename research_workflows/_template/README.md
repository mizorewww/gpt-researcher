# __WORKFLOW_NAME__

This directory is a self-contained OpenCode research workflow.

- `AGENTS.md`: always-on orchestration and evidence invariants.
- `.opencode/agents/`: primary and subagent roles, models, and permissions.
- `.opencode/skills/`: domain methods loaded on demand.
- `.opencode/commands/run.md`: native OpenCode entry prompt.
- `opencode.jsonc`: MCP servers and project configuration.
- `workflow.json`: runner-only execution, security, and result metadata.
- `schemas/`: machine-validated input and result contracts.

Validate and run from the repository root:

```bash
scripts/research_workflow.sh validate research_workflows/__WORKFLOW_NAME__
scripts/research_workflow.sh run research_workflows/__WORKFLOW_NAME__ \
  --input 'What should be investigated?'
```
