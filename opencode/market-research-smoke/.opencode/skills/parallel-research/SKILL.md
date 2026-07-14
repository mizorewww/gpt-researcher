---
name: parallel-research
description: Use when a broad investigation should be split into independent evidence lanes, run concurrently, and synthesized into one result.
---

1. Read the task context and the complete user request before decomposing it.
2. Create the number of independent lanes required by the task. Preserve all explicit scope, date, timezone, evidence, tool-call, failure, and output requirements.
3. Dispatch all lane assignments to `research-worker` in the same turn. Do not wait for one lane before starting another.
4. Give each worker a self-contained assignment that repeats the relevant tool-call contract from the task. The generic harness must not choose tools or invent a call policy.
5. After all workers return, verify their task-required tool-use records, then reconcile dates, units, duplicate evidence, contradictions, and missing coverage. Synthesize one result; never invent evidence to conceal a failed lane.
