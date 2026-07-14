# OpenCode 调用 GPT Researcher 与其他 MCP

这里没有额外的 workflow runner。OpenCode 原生负责理解任务、调用 subagent 和并发；GPT Researcher 只是一个深度调查 MCP，Yahoo Finance 只是一个结构化行情 MCP。

示例项目位于 [`opencode/market-research-smoke`](../opencode/market-research-smoke/)。市场日报只是用来验证并发和工具组合是否稳定。市场内容以及这个工作流要求如何调用 GPT Researcher、Yahoo Finance，都只写在该项目的 `AGENTS.md` 中。

## 目录边界

```text
opencode/market-research-smoke/
├── AGENTS.md                         # 可替换的任务与工具调用契约
├── opencode.jsonc                    # 模型、权限和 MCP 接入
└── .opencode/
    ├── agents/
    │   ├── research-coordinator.md   # 通用编排
    │   └── research-worker.md        # 通用调查 worker
    ├── commands/research.md          # 通用入口
    └── skills/parallel-research/
        └── SKILL.md                  # 通用并行调查方法
```

GPT Researcher 本身仍由本仓库的 `gpt-researcher` MCP 入口提供，Yahoo Finance MCP 与它并列。`opencode.jsonc` 只负责让工具可用并配置权限；是否必须调用、由哪个方向调用、调用几次以及失败如何处理，全部由当前工作流的 `AGENTS.md` 决定。通用 agent 和 skill 不选择工具。

## “必须调用”的边界

OpenCode 原生 permission 只能决定一个工具是 `allow`、`ask` 还是 `deny`，不能声明类似 API `tool_choice=required` 的“本轮必须调用某工具”。因此单靠提示词无法从运行时层面证明 LLM 一定调用了 MCP。

本项目采用不增加 harness 的最强原生约束：

1. `AGENTS.md` 把具体 MCP 调用写成完成条件，并规定未调用或失败时该方向必须失败。
2. 通用 coordinator 必须把对应调用条件原样传给 worker，并拒绝把缺少工具结果的方向当作成功。
3. worker 除任务声明的 MCP 外没有网页搜索、shell 或文件工具可用于替代调查。
4. OpenCode session 中的真实 tool trace 才是调用证据，worker 在文本中声称“已调用”本身不是证明。

如果需要机器级硬保证，就必须增加一个读取真实 tool trace 的校验器或 OpenCode plugin。该校验器应读取每个工作流自己的调用契约；不能把 GPT Researcher、Yahoo Finance 或市场规则写死在通用层。本示例刻意没有增加这一层。

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

协调员读取 `AGENTS.md`，在同一个 turn 中发出三个独立 worker 任务，并把各方向对应的工具调用要求完整传给 worker。本示例明确要求每个方向调用 GPT Researcher，并调用 Yahoo Finance 核验相应结构化行情；未完成必需调用的方向不能被当作成功结果。最终结果由协调员汇总。

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

1. 改 `AGENTS.md`，写新的任务背景、覆盖范围、证据要求、具体工具调用契约和完成标准。
2. 如需不同数据源，只改 `opencode.jsonc` 中的 MCP 和 worker 的工具权限。
3. 通常无需修改 coordinator、worker、command 或 `parallel-research` skill。
4. 用 `opencode mcp list --pure` 检查工具，再用同一条 `opencode run` 命令执行。

若任务不要求三路并发，可在新的 `AGENTS.md` 中指定需要的独立方向数量；通用 skill 会遵循任务要求。不同调查需要不同 MCP 或调用顺序时，也应写入各自的 `AGENTS.md`，不能移动到通用 agent、skill 或 Python harness。

## 模型与故障行为

OpenCode 模型保持 `deepseek/deepseek-v4-pro`。配置不会因余额或调用失败自动切换模型。GPT Researcher 自己的检索并发、超时、取消和证据审计仍属于 MCP 服务内部实现，与 OpenCode 项目结构解耦。
