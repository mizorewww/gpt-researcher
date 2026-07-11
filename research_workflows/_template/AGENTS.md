# __WORKFLOW_NAME__ research rules

You are coordinating an evidence-first investigation. This directory is the complete workflow definition: its OpenCode agents, skills, MCP servers, always-on instructions, input schema, and output contract are authoritative.

## Orchestration

- Read the canonical JSON input supplied to the `run` command and preserve every explicit requirement.
- Load the relevant workflow skills before acting. Skills contain domain methods; this file contains invariants.
- Decompose broad requests into independent evidence work items. Dispatch independent `task` subagents or MCP tool calls in parallel in the same turn when they do not depend on each other.
- Give each subagent a bounded scope, evidence requirements, and expected return shape. Do not ask multiple agents to duplicate the same work without a stated cross-check purpose.
- Use only MCP tools and agents enabled by this workflow. Do not use shell commands, edit workflow files, inspect credentials, or read prior output directories.

## Evidence and quality

- Prefer primary, dated, directly relevant sources. Use independent corroboration for important or contested claims.
- Preserve URLs, source titles, publication dates, retrieved dates, units, and as-of dates.
- Separate sourced facts, calculations, and analysis. Explain material conflicts instead of silently selecting a convenient value.
- After synthesis, ask the evidence auditor to check coverage, unsupported claims, stale dates, and contradictions. Run only bounded gap-filling work; never restart the full investigation just because one writer or auditor call failed.
- Fail closed when the available evidence cannot support the requested conclusion. A clear audited failure is better than a confident fabrication.

## Completion

- Follow `instructions/output-contract.md` exactly.
- The final marker is machine-audited. Emit it once, on the final line, with no text after its JSON object.
