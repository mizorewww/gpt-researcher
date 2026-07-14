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
  '生成昨天的完整市场日报。'
```

协调员读取 `AGENTS.md`，在同一个 turn 中发出三个独立 worker 任务，并把各方向对应的工具调用要求完整传给 worker。本示例明确要求每个方向调用 GPT Researcher，并调用 Yahoo Finance 核验相应结构化行情；未完成必需调用的方向不能被当作成功结果。最终结果由协调员汇总。

从仓库根目录运行时，可使用 `--dir`：

```bash
opencode run --pure \
  --dir "$PWD/opencode/market-research-smoke" \
  --command research \
  --agent research-coordinator \
  '生成昨天的完整市场日报。'
```

## 快速创建另一种调查任务

仓库提供一个只负责脚手架、查看和启动界面的轻量 CLI。它不执行工作流、不规定 MCP，也不验证业务结果。

从空白通用模板创建：

```bash
uv run opencode-workflow new company-research
```

生成的目录位于 `opencode/company-research`。接下来只需：

1. 在 `AGENTS.md` 写任务、工具调用契约和完成标准。
2. 在 `opencode.jsonc` 添加可用 MCP，并允许相应工具名。
3. 只有需要自定义编排时才修改 `.opencode/` 中的 agent、skill 或 command。

也可以把现有工作流当模板复制：

```bash
uv run opencode-workflow new asia-market-copy \
  --template market-research-smoke
```

列出和在终端可视化工作流：

```bash
uv run opencode-workflow list
uv run opencode-workflow show company-research
```

不传 `--root` 时，CLI 先检查当前目录的 `./opencode`；若不存在，则自动使用 checkout 或安装包自带的工作流目录。因此从其他目录执行也能看到内置的 `market-research-smoke` 案例。显式设置 `--root` 或 `OPENCODE_WORKFLOWS_DIR` 时始终以显式目录为准。

直接打开该工作流的 OpenCode TUI：

```bash
uv run opencode-workflow open company-research
```

`open` 每次停留在一个新的对话，并把工作流的入口命令预填到输入框，不会自动发送。默认要求 `.opencode/commands/` 中恰好有一个入口；例如市场案例会预填 `/research `，直接按 Enter 即可。该工作流会用 shell 读取本机日期和时区，将“昨天”解析并冻结后再启动三条调查，不要求人工填写这些运行时信息。

如果一个工作流有多个命令，显式选择要预填的入口：

```bash
uv run opencode-workflow open company-research --command research
```

CLI 只根据当前工作流自己的 command 文件生成 `/命令名 `，不读取或硬编码任何市场、GPT Researcher、Yahoo Finance 调用规则。实际任务与工具契约仍由 `AGENTS.md` 和该 command 文件定义。

只支持 TUI，不再提供 Web UI 启动选项。默认使用 OpenCode `--pure`，避免外部 plugin 改变行为；需要加载外部 plugin 时添加 `--with-plugins`。若工作流放在其他目录，可设置 `OPENCODE_WORKFLOWS_DIR`，或在任意命令前传入 `--root /path/to/workflows`。

若任务不要求三路并发，可在新的 `AGENTS.md` 中指定需要的独立方向数量；通用 skill 会遵循任务要求。不同调查需要不同 MCP 或调用顺序时，也应写入各自的 `AGENTS.md`，不能移动到通用 agent、skill 或 Python harness。

## 模型与故障行为

OpenCode 模型保持 `deepseek/deepseek-v4-pro`。配置不会因余额或调用失败自动切换模型。GPT Researcher 自己的检索并发、超时、取消和证据审计仍属于 MCP 服务内部实现，与 OpenCode 项目结构解耦。
