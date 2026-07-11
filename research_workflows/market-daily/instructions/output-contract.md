# Market workflow output contract

Summarize the durable MCP result and audit first. On the final line emit exactly one marker with a single-line JSON object:

```text
OPENCODE_WORKFLOW_RESULT_JSON: {"status":"completed","summary":"quality gate passed","artifacts":["/current/report.md","/current/manifest.json"],"source_count":25,"job_id":"...","quality_gate_passed":true,"work_item_count":3,"active_codex_peak":3,"target_date":"YYYY-MM-DD","timezone":"Asia/Singapore"}
```

Use `failed` when the job or quality gate failed. Include only paths and metrics returned by this run's MCP responses. Emit no text after the marker.
