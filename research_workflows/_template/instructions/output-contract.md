# Output contract

Return the requested research deliverable first. It may be Markdown and may be long.

On the final line emit exactly:

```text
OPENCODE_WORKFLOW_RESULT_JSON: {"status":"completed","summary":"short audit summary","artifacts":[],"source_count":0}
```

Rules:

- `status` is `completed` only when the requested deliverable and evidence audit are complete; otherwise use `failed`.
- `summary` briefly identifies coverage and any material limitation.
- `artifacts` lists paths explicitly returned by tools in this run. Do not invent paths or read old output directories.
- `source_count` is the number of unique direct HTTP(S) sources actually used.
- Emit one marker only and no content after the JSON object.
