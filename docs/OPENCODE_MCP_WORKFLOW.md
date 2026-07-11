# OpenCode 调用 GPT Researcher 与其他 MCP

这里没有额外的 workflow runner。OpenCode 原生负责理解任务、调用 subagent 和并发；GPT Researcher 只是一个深度调查 MCP，Yahoo Finance 只是一个结构化行情 MCP。

示例项目位于 [`opencode/market-research-smoke`](../opencode/market-research-smoke/)。市场日报只是用来验证并发和工具组合是否稳定，所有市场需求只写在该项目的 `AGENTS.md` 中。

## 目录边界

```text
opencode/market-research-smoke/
├── AGENTS.md                         # 可替换的任务需求
├── opencode.jsonc                    # 模型、权限和 MCP 接入
└── .opencode/
    ├── agents/
    │   ├── research-coordinator.md   # 通用编排
    │   └── research-worker.md        # 通用调查 worker
    ├── commands/research.md          # 通用入口
    └── skills/parallel-research/
        └── SKILL.md                  # 通用并行调查方法
```

GPT Researcher 本身仍由本仓库的 `gpt-researcher` MCP 入口提供。OpenCode 不进入它的内部规划、检索或 job 管理；worker 只把它当作一个高层调查工具调用。Yahoo Finance MCP 与它并列，OpenCode 根据工作项选择工具。

## 直接运行

准备 `.env` 中原有的 `DEEPSEEK_API_KEY`、`TAVILY_API_KEY` 和 Codex 登录，然后先检查两个 MCP：

```bash
cd opencode/market-research-smoke
set -a
source ../../.env
set +a
opencode mcp list --pure
```

执行一次调查：

```bash
opencode run --pure \
  --command research \
  --agent research-coordinator \
  '目标日期为 2026-07-10，时区 Asia/Singapore。生成完整市场日报。'
```

协调员读取 `AGENTS.md`，在同一个 turn 中发出三个独立 worker 任务。每个 worker 可以调用 GPT Researcher；需要结构化金融数据时再调用 Yahoo Finance。最终结果由协调员汇总。

从仓库根目录运行时，可使用 `--dir`：

```bash
opencode run --pure \
  --dir "$PWD/opencode/market-research-smoke" \
  --command research \
  --agent research-coordinator \
  '目标日期为 2026-07-10，时区 Asia/Singapore。生成完整市场日报。'
```

## 快速创建另一种调查任务

复制这个原生 OpenCode 项目即可，不需要生成器或 Python harness：

```bash
cp -R opencode/market-research-smoke opencode/company-research
```

然后：

1. 只改 `AGENTS.md`，写新的任务背景、覆盖范围、证据要求和完成标准。
2. 如需不同数据源，只改 `opencode.jsonc` 中的 MCP 和 worker 的工具权限。
3. 通常无需修改 coordinator、worker、command 或 `parallel-research` skill。
4. 用 `opencode mcp list --pure` 检查工具，再用同一条 `opencode run` 命令执行。

若任务不要求三路并发，可在新的 `AGENTS.md` 中指定需要的独立方向数量；通用 skill 会遵循任务要求。不要把某个测试任务的指数、股票、调用顺序或结果 validator 放进通用编排层。

## 模型与故障行为

OpenCode 模型保持 `deepseek/deepseek-v4-pro`。配置不会因余额或调用失败自动切换模型。GPT Researcher 自己的检索并发、超时、取消和证据审计仍属于 MCP 服务内部实现，与 OpenCode 项目结构解耦。
