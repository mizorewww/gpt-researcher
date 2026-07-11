# 通用 OpenCode 调查工作流

`gptr-workflow` 把每一种调查需求表示为一个独立的 OpenCode 项目目录。Runner 不理解金融、法律、论文或竞品分析；它只负责 process/session 调度、不可变快照、隔离配置、并发、超时、进程树清理、工具权限与预算审计、输入/结果 Schema、日志和 manifest。

这套 OpenCode workflow 与直接调用 GPT Researcher MCP API 是两条独立路径。`profile_info`、`research_report_start`、`research_reports_status` 和 `research_report_result` 是选择该 backend 时可用的 API，不是通用 workflow 的固定协议，也不应写进通用 `AGENTS.md`。只有 workflow 主动配置并选择 GPT Researcher MCP 时，相关工具才属于它的能力面。

调查方法由目录中的 OpenCode 原生文件决定：

```text
research_workflows/<name>/
├── workflow.json                 # 仅 runner 元数据、容量、安全边界和 Schema 路径
├── AGENTS.md                     # 领域任务上下文、覆盖范围和完成条件
├── opencode.jsonc                # 模型、instructions 和任意 local/remote MCP
├── instructions/                 # 必须始终加载的证据/输出约束
├── schemas/                      # 输入与最终 marker JSON Schema
└── .opencode/
    ├── commands/run.md           # 原生 OpenCode 入口，接收 $ARGUMENTS
    ├── agents/*.md               # primary coordinator 与专业 subagents
    └── skills/*/SKILL.md         # 按需加载的领域调查方法
```

市场日报只是 [`research_workflows/market-daily`](../research_workflows/market-daily/) 里的一个示例。它与其他 workflow 使用同一个 runner；市场覆盖规则只存在于该目录的任务上下文、skill、Schema 和 validator 中。

## 一分钟创建

```bash
# 1. 从安全的通用模板创建
scripts/research_workflow.sh init company-intelligence

# 2. 编辑工作流本身
$EDITOR research_workflows/company-intelligence/AGENTS.md
$EDITOR research_workflows/company-intelligence/opencode.jsonc
$EDITOR research_workflows/company-intelligence/.opencode/commands/run.md

# 3. 添加或修改 agent 与 skill
$EDITOR research_workflows/company-intelligence/.opencode/agents/research-coordinator.md
$EDITOR research_workflows/company-intelligence/.opencode/skills/evidence-triangulation/SKILL.md

# 4. 不调用模型地检查 config、primary agent、skills、MCP 与 Schema
scripts/research_workflow.sh validate research_workflows/company-intelligence

# 5. 执行
scripts/research_workflow.sh run research_workflows/company-intelligence \
  --input '调查目标公司的产品、客户、竞争格局、财务风险和最近一年重大事件'
```

也可以克隆已有 workflow 作为起点；CLI 会改写 `workflow.json` 的名称：

```bash
scripts/research_workflow.sh init asia-market-monitor \
  --template research_workflows/market-daily
```

## 每个文件应该放什么

### `AGENTS.md`

这里只放该类任务始终成立的领域上下文，例如：

- 调查对象、术语和边界；
- 必须覆盖的市场、地区、实体或证据类型；
- 日期、单位、来源质量和矛盾处理要求；
- 报告必须包含的内容与 fail-closed 完成条件。

不要把某一次调查问题、MCP 工具名、固定工具调用顺序、轮询协议、并发数、重试次数或进程控制写进 `AGENTS.md`。具体问题通过 `--input` 或 `--input-json` 传入；任务拆解和工具选择属于 agents/skills，session 容量、超时、工具预算、权限与清理属于通用 runner。

### `.opencode/agents/*.md`

`entryAgent` 必须是 `mode: primary`。专业调查员和审计员用 `mode: subagent`。任务相关的拆解、委派和工具选择写在 agents/skills 中；模型属于 agent 配置，不属于 `workflow.json`：

```markdown
---
description: Primary coordinator for product and company due diligence.
mode: primary
model: deepseek/deepseek-v4-pro
permission:
  "*": deny
  skill: allow
  task: allow
  company-data_*: allow
---
```

Runner 会在启动前调用 OpenCode 的 agent discovery。如果 agent 名拼错、未发现，或 entry agent 不是 primary，运行直接失败，避免 OpenCode 静默回退到默认 agent。

### `.opencode/skills/<name>/SKILL.md`

Skill 放按需加载的领域方法，不放必须始终执行的安全条件：

```markdown
---
name: clinical-literature-review
description: Use for clinical evidence, trials, systematic reviews, endpoints, safety signals, and medical literature comparisons.
---

# Clinical literature review

...领域方法、数据库优先级、证据分级、冲突处理和停止条件...
```

目录名、`name` 与 OpenCode 的技能命名规则必须一致。`validate` 会确认技能能够被 OpenCode 发现。

### `opencode.jsonc`

这里选择模型、常驻 instructions 和 MCP。密钥只使用 `{env:VAR}`，不要写明文：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "deepseek/deepseek-v4-pro",
  "default_agent": "research-coordinator",
  "mcp": {
    "company-data": {
      "type": "remote",
      "url": "https://research.example.com/mcp",
      "headers": {
        "Authorization": "Bearer {env:COMPANY_DATA_TOKEN}"
      },
      "enabled": true
    },
    "local-database": {
      "type": "local",
      "command": ["uv", "run", "python", "server.py"],
      "environment": {"DATABASE_URL": "{env:DATABASE_URL}"},
      "enabled": true
    }
  }
}
```

新增 MCP 后同步更新：

- `workflow.json` 的 `requires.env`；
- `security.allowedToolPatterns`，例如 `company-data_*`；
- 可选的 `security.toolCallBudgets`，为同一个已允许 pattern 声明每副本预算基数；
- `security.agentToolPatterns`，为每个 agent 设置不超过全局白名单的运行时上限；
- 需要调用它的 agent 的 `permission`。

Runner 会通过最高优先级的运行时安全 overlay，再次对 primary 和允许的 subagent 注入 deny-by-default 工具白名单。OpenCode project config 不能在运行时绕过这一层。

只有 `requires.env` 声明的业务变量会从宿主环境或项目 `.env` 传给 OpenCode；`OPENCODE_*`、`PYTHONPATH`、动态链接器变量等控制变量禁止继承。这样新增 MCP 时需要显式声明凭证，同时不会把整个宿主环境交给任意 local MCP。

### `workflow.json` 工具预算

`security.toolCallBudgets` 是通用 runner 的硬限制，不是给模型看的提示词。键必须同时出现在 `security.allowedToolPatterns` 中，值是每个 root session/replica 的预算基数：

```json
{
  "security": {
    "allowedToolPatterns": ["skill", "task", "company-data_*"],
    "toolCallBudgets": {
      "task": 12,
      "company-data_*": 80
    }
  }
}
```

Runner 从隔离 OpenCode permission log 统计 primary 与所有 nested agent 的真实调用。一次运行的有效聚合上限是 `configured_per_replica × replicas`；例如 `company-data_*: 80` 在三个副本时允许整个 run 合计最多 240 次匹配调用。它是按副本数扩展的聚合上限，并不保证每个副本各自恰好只能使用 80 次。Runner 运行中持续检查总量，超限后终止进程树并将运行标为失败；只要配置了预算，permission audit 不可用也会 fail closed。Manifest 的 `security.tool_call_budgets_per_replica`、`security.effective_tool_call_budgets` 和 `tool_audit` 会记录基数、有效上限、实际计数与违规项。

### `.opencode/commands/run.md`

这是 prompt 的真正入口。Runner 传入的是符合输入 Schema 的单行规范 JSON：

````markdown
---
description: Run the complete investigation.
agent: research-coordinator
---

Canonical input:

```json
$ARGUMENTS
```
````

默认禁止 OpenCode command 的 `` !`shell command` `` 展开。若确实需要，必须在 `workflow.json` 显式设置 `allowCommandShell`；不建议调查工作流开启。

## 输入

纯文本自动包装为：

```json
{"query":"用户输入"}
```

复杂需求推荐显式 JSON：

```json
{
  "query": "比较三个供应商并给出采购建议",
  "target_date": "2026-07-10",
  "timezone": "Asia/Singapore",
  "constraints": {
    "regions": ["US", "Japan"],
    "minimum_primary_sources": 12
  }
}
```

```bash
scripts/research_workflow.sh run research_workflows/vendor-review \
  --input-json requests/vendor-review.json
```

输入在启动 OpenCode 前由 `schemas/input.schema.json` 校验。

## 并发

同一问题运行三份独立副本：

```bash
scripts/research_workflow.sh load-test research_workflows/company-intelligence \
  --input '完整调查问题' \
  --replicas 3
```

三个不同问题并行：

```bash
scripts/research_workflow.sh run research_workflows/company-intelligence \
  --input '调查公司 A' \
  --input '调查公司 B' \
  --input '调查公司 C'
```

多副本共用一个该 workflow 专属的持久 `opencode serve`，每个 `run --attach` 创建独立 session。副本数量由 `workflow.json` 的 `maxReplicas` 限制。任务相关的 subagent/MCP fan-out 由 agents/skills 决定；通用 runner 强制 session 数量、总 deadline、工具权限、聚合工具预算和进程树清理。MCP 后端仍必须实现自己的机器级并发配额、队列、超时和取消，不能依赖 prompt 或客户端工具计数代替服务端容量保护。

## 安全、隔离与审计

每次运行都会：

1. 严格校验 workflow name 与 `run_id`，拒绝路径穿越和复用既有目录；
2. 拒绝 workflow 中的 symlink；
3. 把 workflow 复制到父 Git worktree 之外，设为只读，记录每个文件的 SHA-256，并在结束后复算；
4. 使用隔离 XDG，禁用用户目录的外部 skills、外部 plugins 和宿主 OpenCode 控制配置；
5. 预检 resolved config、entry primary agent、allowed agents、skills 和 MCP；
6. 并行启动 session，并在超时/中断时 TERM 后 KILL 进程组；
7. 审计整个 agent tree 的工具名，任何不在白名单的调用都会令运行失败；
8. 按每副本预算基数计算本次运行的聚合工具上限，实时超限或审计不可用时 fail closed；
9. 提取最终 marker JSON，并用 `schemas/result.schema.json` 校验；
10. 用每个 session 的真实开始/结束区间计算并发峰值，清理检测到的孤儿进程；
11. 保存响应、日志、进程、hash、错误、工具计数与预算、validator 结果和孤儿进程检查。

默认产物：

```text
outputs/workflows/<name>/<run-id>/manifest.json
outputs/workflows/<name>/<run-id>/responses/session-*.md
run_logs/opencode-workflows/<name>/<run-id>/session-*.jsonl
${TMPDIR}/gptr-opencode-workflows/<project-hash>/<artifact-root-hash>/<name>/<run-id>/workflow/
```

实际 runtime 绝对路径始终记录在 manifest。原始 session JSONL 和 MCP tool input/output 对审计很重要，也可能包含私有调查内容；runner 将运行目录、日志、manifest、响应和输入设为仅当前用户可读（目录 `0700`、文件 `0600`）。请按数据保留政策清理历史 run，不要把 `run_logs/` 或 `outputs/` 公开上传。

Workflow 目录本身属于可信可执行配置：local MCP 和可选 validator 都可以启动本地程序。不要运行来源不可信的 workflow；权限白名单保护 agent 工具面，不是操作系统级恶意代码沙箱。

当某个 workflow 选择 GPT Researcher MCP 时，report worker 通过跨进程共享的文件锁保持最多 3 个，Codex 通过另一组共享槽位保持最多 9 个。所有 checkout 和已安装 CLI 默认共用 `~/.gpt-researcher/slots`；如需改位置，设置 `GPT_RESEARCHER_GLOBAL_SLOT_ROOT`，并确保本机所有 coordinator 使用同一个可写目录。只使用其他 MCP 的 workflow 不会被错误套用这组领域无关的容量说明，其上限由各 MCP 和 `maxReplicas` 决定。

快速检查：

```bash
jq '{status, workflow, replicas, session_execution_peak, sessions, orphan_pids}' \
  outputs/workflows/<name>/<run-id>/manifest.json
```

## 模型与余额

Runner 不实现自动换模型或余额 fallback。当前模板与市场示例都保持 `deepseek/deepseek-v4-pro`。如果 DeepSeek 返回 `402 Insufficient Balance`，运行会失败并保留日志；充值后用新的 `run_id` 重跑即可。

Codex 检索子进程的 `gpt-5.5` 是 GPT Researcher MCP 内部 retriever 配置，不是 DeepSeek 的替代或 fallback。

## 市场示例

```bash
scripts/research_workflow.sh validate research_workflows/market-daily \
  --input '{"query":"调研指定交易日的美日韩港市场、宏观、大宗商品和重要股票，生成严肃日报。","target_date":"2026-07-10","timezone":"Asia/Singapore"}'

scripts/research_workflow.sh run research_workflows/market-daily \
  --input '{"query":"调研指定交易日的美日韩港市场、宏观、大宗商品和重要股票，生成严肃日报。","target_date":"2026-07-10","timezone":"Asia/Singapore"}'
```

市场示例通过自己的 agents/skill 组合固定版本的社区、非官方 `yfinance-market-mcp` 与 Tavily MCP；`AGENTS.md` 只描述日报任务上下文。质量规则位于 workflow validator，执行仍是同一个通用 runner。三副本满载验证也直接使用通用入口：

```bash
scripts/research_workflow.sh load-test research_workflows/market-daily \
  --input '{"query":"调研指定交易日的美日韩港市场、宏观、大宗商品和重要股票，生成严肃日报。","target_date":"2026-07-10","timezone":"Asia/Singapore"}' \
  --replicas 3
```

## OpenCode 原生机制参考

- [Rules / AGENTS.md](https://opencode.ai/docs/rules/)
- [Agents](https://opencode.ai/docs/agents/)
- [Agent Skills](https://opencode.ai/docs/skills/)
- [Custom Commands](https://opencode.ai/docs/commands/)
- [MCP servers](https://opencode.ai/docs/mcp-servers/)
- [CLI 与 `run --attach`](https://opencode.ai/docs/cli/)
- [Server](https://opencode.ai/docs/server/)
