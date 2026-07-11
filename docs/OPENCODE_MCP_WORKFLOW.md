# OpenCode 调度 GPT Researcher：可重复运行手册

> 本文是市场日报 acceptance harness 的运维手册，不是通用工作流接口。要通过每个目录自己的 `AGENTS.md`、agents、skills 和 MCP 创建任意调查流程，请使用 [`GENERIC_RESEARCH_WORKFLOWS.md`](GENERIC_RESEARCH_WORKFLOWS.md) 与 `scripts/research_workflow.sh`。

这套入口用于重复执行已经验收过的 OpenCode → MCP → 隔离报告 worker → 三路 Codex/Tavily 调查链路。日常生成一份报告用 `single`；验证整机满载并发用 `stress`。两者都会创建全新的运行目录，不会读取旧报告补写。

## 1. 一次性准备

在项目根目录执行：

```bash
uv sync --group dev
codex login
opencode --version
```

复制 `.env.example` 为 `.env`，至少配置当前模型和检索器需要的密钥：

```dotenv
DEEPSEEK_API_KEY=...
TAVILY_API_KEY=...
```

不要把密钥写进命令、OpenCode prompt 或提交到 Git。Codex CLI 继续使用 `codex login` 保存的认证。默认稳定配置已经固定为 `search + medium + fast`。

先做完全离线的配置检查：

```bash
scripts/opencode_stability_market_report.sh dry-single
scripts/opencode_stability_market_report.sh dry-stress
```

## 2. 最短调用

生成一份昨日市场日报：

```bash
scripts/opencode_stability_market_report.sh single
```

显式冻结日期，保证以后重跑仍调查同一个交易日：

```bash
TARGET_DATE=2026-07-10 \
REPORT_TIMEZONE=Asia/Singapore \
scripts/opencode_stability_market_report.sh single
```

启动一个持久 `opencode serve`，并行附着三个独立 OpenCode 会话；每份报告内部再并行三个 work item：

```bash
TARGET_DATE=2026-07-10 \
RUN_ID=market-2026-07-10-stress \
scripts/opencode_stability_market_report.sh stress
```

`RUN_ID` 必须唯一。省略时脚本会自动生成。所有可用命令和环境变量可随时查看：

```bash
scripts/opencode_stability_market_report.sh --help
```

## 3. 并发模型

| 入口 | OpenCode 会话 | 报告 worker | 每份初始 Codex | 整机 Codex 峰值 |
|---|---:|---:|---:|---:|
| `single` | 1 | 1 | 3 | 3 |
| `stress` | 3 | 3 | 3 | 9 |

规划器对每份报告生成恰好三个结构化 work item。三个初始检索同时运行；覆盖或矛盾检查后只允许一轮、最多三条并行补查。服务端而不是 OpenCode prompt 负责并发上限，所以即使客户端没有正确并行工具调用，也不会突破三 worker / 九 Codex 的机器上限。每个 worker 内普通 retriever 上限为 4、scraper 上限为 5。

## 4. OpenCode 实际调用协议

如果需要自己编写 OpenCode prompt，保持以下工具顺序：

1. `profile_info`：确认运行配置。
2. `research_report_start(query, target_date, timezone)`：每份报告只提交一次。
3. `research_reports_status(job_ids=[job_id], wait_seconds=20)`：使用批量长轮询直到终态。
4. `research_report_result(job_id, include_report=false)`：只取得路径、指标和审计信息。
5. 需要中止时调用 `research_report_cancel(job_id)`，由服务端终止 worker 及整个子进程组。

不要高频调用单任务 `research_report_status`，不要让 OpenCode 读取 `outputs/` 或 `run_logs/` 的旧文件，也不要让客户端自己拆成多个报告任务。报告内部三路拆解、缺口补查和证据质量门都由项目规划器完成。

可复用的 prompt 骨架：

```text
只使用 gpt-researcher-codex-long MCP 工具。
先调用 profile_info；然后只调用一次 research_report_start，并显式传入
target_date=<YYYY-MM-DD>、timezone=Asia/Singapore 和完整调查问题。
使用 research_reports_status(job_ids=[job_id], wait_seconds=20) 长轮询到终态。
completed 后调用 research_report_result(job_id, include_report=false)。
不要读取任何旧报告，不要自行创建后续报告；服务端会在单报告内部完成三路并发调查和一次有上限的缺口补查。
```

当前 harness 内置的是严格市场日报任务，并额外检查 10 个指数、4 种商品、四地至少 16 只股票、至少 25 个 HTTP(S) 来源以及三份报告的 14 个共同数值。若要跑任意主题，可直接复用上面的 MCP 协议；市场专用 harness 的质量门不应被当作通用主题验证器。

## 5. 产物与验收

默认产物位于：

```text
outputs/stability/<run-id>/manifest.json
outputs/stability/<run-id>/reports/*.md
run_logs/opencode-market/<run-id>/harness.jsonl
.tmp/opencode-market/<run-id>/research-jobs/<job-id>/
```

快速查看结果：

```bash
jq '{status, acceptance, sessions}' outputs/stability/<run-id>/manifest.json
jq -r '.sessions[].report_copy_path // empty' outputs/stability/<run-id>/manifest.json
```

成功的 `stress` 必须满足 3/3 报告完成、三个 worker 重叠、每份三个初始 Codex 调用重叠、整机 Codex 峰值不超过 9、任务启动跨度不超过 10 秒、并行比率至少 2.0、共同表格数值无未解释冲突，并且清理后没有残留子进程。

验收策略修复后，可以只读复验旧产物。该操作会记录源 manifest 和报告的 SHA-256，不修改原文件：

```bash
scripts/opencode_stability_market_report.sh revalidate \
  outputs/stability/<run-id>/manifest.json
```

指定复验输出路径：

```bash
scripts/opencode_stability_market_report.sh revalidate \
  outputs/stability/<run-id>/manifest.json \
  outputs/stability/<run-id>/revalidation-v2.json
```

## 6. 常见失败

- OpenCode 返回 `402 Insufficient Balance`：协调模型账户余额不足，报告尚未进入 MCP；充值或更换 `MODEL` 后用新的 `RUN_ID` 重跑。
- `failed` 且证据不足：查看 manifest 的质量门和 job 目录中的 `events.jsonl`、`stderr.log`、`result.json`；不要把失败产物当成功日报发布。
- Codex 超时或临时网络错误：单次调用最多 300 秒，只对瞬态错误重试一次；检查结构化调用遥测和全局槽位目录。
- 路径已存在：这是防止旧结果污染的保护。换一个 `RUN_ID`，不要删除或复用正在审计的目录。
- 需要立即停止：在 MCP 客户端调用 `research_report_cancel`。直接中断 harness 时，它也会 TERM 后 KILL 已跟踪的整个进程组并检查孤儿进程。

## 7. 每次运行的推荐检查单

```bash
# 1) 离线预检
scripts/opencode_stability_market_report.sh dry-stress

# 2) 用明确日期运行；日常用 single，容量验证用 stress
TARGET_DATE=YYYY-MM-DD scripts/opencode_stability_market_report.sh single

# 3) 检查总状态、质量门和最终报告路径
jq '.status, .acceptance' outputs/stability/<run-id>/manifest.json

# 4) 在策略变更后只读复验，不回写原报告
scripts/opencode_stability_market_report.sh revalidate \
  outputs/stability/<run-id>/manifest.json
```
