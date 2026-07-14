# OpenCode 通用调查工作流

OpenCode 负责理解保存的问题并调用工具；GPT Researcher、Yahoo Finance 和 time 都只是普通 MCP。项目没有额外的 workflow runner，也没有把业务规则写进 Python harness。GPT Researcher MCP 只暴露 `research_report(query)`：一次调用等待完整报告后直接返回；多份独立调查由 OpenCode 同时发出多个调用，服务端负责真正并发，不向模型暴露 job、轮询或结果获取协议。

内置案例位于 [`opencode/market-research-smoke`](../opencode/market-research-smoke/)：

```text
opencode/market-research-smoke/
├── AGENTS.md                         # 通用投资调查方法
├── opencode.jsonc                    # 模型、权限和 MCP 接入
└── .opencode/commands/research.md    # 保存的具体市场调查问题
```

三个文件职责分离：

- `AGENTS.md` 提供工具分工和调查原则。它没有固定调查方向、agent 数量、调用次数、并发拆解或报告八股。
- `opencode.jsonc` 只让三个 MCP 可用并配置权限。
- `research.md` 保存本案例要执行的完整市场问题。换一种调查任务时只需替换这里的问题；需要不同工具时再修改 `opencode.jsonc` 和 `AGENTS.md`。

## 打开完整问题

先把项目 `.env` 中的密钥导入当前 shell：

```bash
set -a
source .env
set +a
```

从仓库根目录运行：

```bash
uv run opencode-workflow open market-research-smoke
```

CLI 会打开一个新的 OpenCode TUI，并读取 `research.md`：去掉 YAML frontmatter 和 `$ARGUMENTS` 后，把保存的完整市场调查问题直接填入输入框。你看到的是完整问题，不是 `/research` 占位符；直接按 Enter 即可。

CLI 不解析问题、不计算日期、不选择 MCP，也不决定串行、并行或如何拆解。模型根据 `AGENTS.md`、time MCP 的结果和保存问题中的自然语言自行处理。

如果工作流有多个 command，可选择要填入的文件：

```bash
uv run opencode-workflow open my-workflow --command audit
```

也可以不进入 TUI，直接执行保存的 command：

```bash
opencode run --pure \
  --dir "$PWD/opencode/market-research-smoke" \
  --command research
```

## 创建新工作流

```bash
uv run opencode-workflow new company-research
uv run opencode-workflow show company-research
uv run opencode-workflow open company-research
```

生成目录后：

1. 在 `.opencode/commands/research.md` 写需要自动填入的完整问题。
2. 在 `AGENTS.md` 只写该类调查长期有效的背景和必要原则。
3. 在 `opencode.jsonc` 配置该工作流可以调用的 MCP。
4. 只有确实需要额外编排时才添加 `.opencode/agents` 或 `.opencode/skills`；它们不是必需结构。

也可以复制市场案例作为起点：

```bash
uv run opencode-workflow new another-investigation \
  --template market-research-smoke
```

列出工作流：

```bash
uv run opencode-workflow list
```

不传 `--root` 时，CLI 先使用当前目录的 `./opencode`；不存在时使用安装包内置的工作流。也可以通过 `--root` 或 `OPENCODE_WORKFLOWS_DIR` 指向其他工作流目录。

OpenCode 模型仍为 `deepseek/deepseek-v4-pro`，没有余额失败后的模型切换逻辑。
