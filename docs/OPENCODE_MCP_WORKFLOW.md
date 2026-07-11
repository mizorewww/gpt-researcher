# OpenCode 市场调查工作流

市场日报现在只是通用 `gptr-workflow` runner 上的一个 OpenCode 项目：[`research_workflows/market-daily`](../research_workflows/market-daily/)。不存在市场专用 Python harness，也不存在固定的 MCP 调用剧本。

直接 GPT Researcher MCP API 是另一条可选集成路径。它暴露的 `profile_info`、`research_report_start`、`research_reports_status` 和 `research_report_result` 不由本 workflow 调用，也不是这里的 prompt 或 `AGENTS.md` 协议。市场 workflow 直接组合自己声明的 MCP，统一交给通用 runner 执行。

职责边界：

- `gpt_researcher/opencode_workflow/`：通用 process/session 调度、并发、隔离、权限、工具预算、超时、Schema、日志、manifest、validator 和进程清理；不了解市场任务。
- `market-daily/AGENTS.md`：只保存市场日报的任务上下文、覆盖范围、证据规则和完成条件。
- `.opencode/agents/`：协调员、结构化数据研究员、网页证据研究员和审计员的角色与权限。
- `.opencode/skills/`：日期、行情、单位、选股、证据对账和停止条件等领域方法。
- `opencode.jsonc`：组合社区、非官方 `yfinance-market-mcp` 与 Tavily MCP。
- `workflow.json`：声明副本容量、工具白名单和每副本工具预算基数。
- `validators/market_report.py`：验证最终报告质量，不规定工具顺序。

## MCP 组合

结构化行情使用固定版本：

```text
uvx --from yfinance-market-mcp==0.3.3 yfinance-mcp
```

这是社区 MCP，通过非官方 `yfinance` 获取 Yahoo Finance 数据；无需 Yahoo API key，但不应视作 Yahoo 官方软件或交易级行情源。

网页、央行、交易所、公司 IR 和新闻证据使用固定版本 `tavily-mcp@0.2.21`。密钥只通过 `TAVILY_API_KEY` 环境变量传入。工作流 deny-by-default，只允许 `yfinance_*`、`tavily_*`、`skill` 和允许的 `task` subagent。

## 工具调用预算

市场 workflow 在 `workflow.json` 声明通用 `security.toolCallBudgets`：每个 replica 的预算基数为 `skill: 12`、`task: 12`、`yfinance_*: 180`、`tavily_*: 40`。单副本运行的有效上限就是这些数值；三个副本时，整个 run 的聚合上限分别是 36、36、540 和 120。

Runner 从隔离 OpenCode permission log 聚合 primary 与所有 nested agent 的真实调用，每 0.25 秒检查一次；超限会终止进程树并失败，配置预算但无法取得完整审计也会 fail closed。这个机制按 replica 数扩展整体上限，不保证三副本各自平均分配额度。实际工具名、计数、预算基数、聚合上限和违规项都写入 manifest；这些限制不依赖 `AGENTS.md`、模型自报或市场专用代码。

## 准备

```bash
uv sync --group dev
opencode --version
```

`.env` 至少包含：

```dotenv
DEEPSEEK_API_KEY=...
TAVILY_API_KEY=...
```

模型保持 `deepseek/deepseek-v4-pro`。Runner 不自动切换模型；余额不足会失败并保留审计。

## 预检

预检会验证 resolved config、primary/subagents、skill、两个 MCP、权限和工具预算，不调用模型：

```bash
scripts/research_workflow.sh validate research_workflows/market-daily \
  --input '{"query":"生成完整市场日报","target_date":"2026-07-10","timezone":"Asia/Singapore"}'
```

## 运行

一份报告：

```bash
scripts/research_workflow.sh run research_workflows/market-daily \
  --input '{"query":"生成完整市场日报","target_date":"2026-07-10","timezone":"Asia/Singapore"}'
```

三份独立 OpenCode session 并发：

```bash
scripts/research_workflow.sh load-test research_workflows/market-daily \
  --input '{"query":"生成完整市场日报","target_date":"2026-07-10","timezone":"Asia/Singapore"}' \
  --replicas 3
```

## 产物

```text
outputs/workflows/market-daily/<run-id>/manifest.json
outputs/workflows/market-daily/<run-id>/responses/session-*.md
run_logs/opencode-workflows/market-daily/<run-id>/session-*.jsonl
${TMPDIR}/gptr-opencode-workflows/.../market-daily/<run-id>/workflow/
```

查看结果：

```bash
jq '{status, session_execution_peak, security, tool_audit, sessions, validators, orphan_pids}' \
  outputs/workflows/market-daily/<run-id>/manifest.json
```

成功运行必须通过通用 runner 检查和市场 workflow validator：日期一致、十个指数、四种商品、四地至少 16 只股票、至少 25 个实际写入报告的不同 HTTP(S) URL、Yahoo 与网页两类证据、最终 marker、快照完整性以及无孤儿进程。

## 修改调查方式

不要修改 runner。根据需要修改：

- 任务覆盖：`research_workflows/market-daily/AGENTS.md`
- 调查方法：`.opencode/skills/market-daily-method/SKILL.md`
- 角色与模型：`.opencode/agents/*.md`
- MCP：`opencode.jsonc`，并同步更新 `workflow.json` 的 required MCP 和工具权限
- 工具预算：`workflow.json` 的 `security.toolCallBudgets`；每个 pattern 必须也在 `allowedToolPatterns`
- 质量门：`schemas/` 与 `validators/market_report.py`

通用工作流创建与安全模型见 [`GENERIC_RESEARCH_WORKFLOWS.md`](GENERIC_RESEARCH_WORKFLOWS.md)。
