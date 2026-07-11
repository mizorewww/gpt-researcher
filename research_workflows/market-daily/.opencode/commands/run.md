---
description: Submit one frozen-date market report to GPT Researcher and audit its terminal result.
agent: market-coordinator
---

Run the market-daily workflow using this canonical input:

```json
$ARGUMENTS
```

Follow AGENTS.md exactly. Use the market skill, submit one MCP report job, long-poll it to a terminal state, fetch its lightweight result, and have the market auditor check the outcome. Do not compensate for a failed quality gate with unsourced prose.
