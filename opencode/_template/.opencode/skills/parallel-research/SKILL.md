---
name: parallel-research
description: Use when the current task requires independent research lanes to run concurrently and be synthesized.
---

1. Read the complete task, tool-call contract, failure rules, and user request.
2. Create exactly the lanes required by AGENTS.md; do not introduce a fixed lane count.
3. Give each worker a self-contained assignment containing its relevant tool requirements.
4. Dispatch all independent workers in the same turn.
5. Verify required tool results, reconcile evidence, and fail closed for incomplete lanes.
