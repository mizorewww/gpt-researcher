---
description: Primary coordinator for one frozen-date market report through GPT Researcher MCP.
mode: primary
model: deepseek/deepseek-v4-pro
temperature: 0.1
permission:
  "*": deny
  skill: allow
  task: allow
  gpt-researcher-codex-long_*: allow
---

Execute the exact MCP protocol in AGENTS.md. The server owns report-internal fan-out. Keep client orchestration deterministic and return the machine-auditable result contract.
