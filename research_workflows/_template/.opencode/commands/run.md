---
description: Run this workflow's evidence-first research process from canonical JSON input.
agent: research-coordinator
---

Execute the complete research workflow defined by this project.

Canonical input JSON:

```json
$ARGUMENTS
```

Validate the intent against `schemas/input.schema.json`, load the relevant skills, use the configured MCP servers, parallelize independent work, synthesize the deliverable, and ask the evidence auditor to perform the final quality check. Follow the output contract exactly.
