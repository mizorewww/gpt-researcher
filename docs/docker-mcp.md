# Docker MCP 部署指南

本文说明如何在 Debian ARM64 或 AMD64 主机上，以 Docker Compose 运行
GPT Researcher MCP 服务。服务使用 Streamable HTTP，默认地址为：

```text
http://127.0.0.1:8811/mcp
```

端口只绑定宿主机回环地址，不会暴露给局域网或公网。

## 部署方式

此部署与项目原有的前后端 `docker-compose.yml` 相互独立，使用
`docker-compose.mcp.yml`：

- 镜像基于 Debian Bookworm Slim，同时支持 `linux/arm64` 与
  `linux/amd64`。
- 镜像包含 Debian Python、uv、Git 和 Codex CLI，但不包含项目源码。
- 容器启动时从指定上游 clone 源码，并在以后每次启动时抓取指定分支或
  tag 的最新提交。
- uv 使用上游提交的 `uv.lock` 执行 `uv sync --frozen`。
- `.env` 保留在宿主机，不会复制进镜像或运行时 clone 的仓库。
- 服务以 UID `10001` 的非 root 用户运行。

相关文件：

| 文件 | 用途 |
| --- | --- |
| `docker-compose.mcp.yml` | 服务、端口、外置环境变量与持久卷 |
| `docker/mcp/Dockerfile` | Debian 多架构运行镜像 |
| `docker/mcp/entrypoint.sh` | 认证检查、上游更新、依赖同步与降权启动 |
| `docker/mcp/run_mcp.py` | Streamable HTTP MCP 启动适配器 |

## 前置要求

- Docker Engine 24 或更高版本。
- Docker Compose v2，即 `docker compose` 命令。
- 能访问 GitHub、Debian 软件源、Python 包源以及所配置的模型和检索服务。
- 至少 4 GB 可用内存；并发研究任务较多时建议 8 GB 或更多。
- 首次启动需要下载 Python 依赖，建议预留至少 3 GB 磁盘空间。

检查 Docker：

```sh
docker version
docker compose version
```

## 准备外置 `.env`

在仓库根目录，也就是 `docker-compose.mcp.yml` 所在目录创建 `.env`：

```sh
cp .env.example .env
chmod 600 .env
```

最小的 Tavily 配置示例：

```dotenv
OPENAI_API_KEY=replace-me
TAVILY_API_KEY=replace-me
RETRIEVER=tavily
LANGUAGE=chinese
```

如果使用其他 LLM provider，请按项目主文档设置对应模型名称和 API key。
不要把真实 `.env` 提交到 Git；项目的 `.gitignore` 已忽略它。

### 启用 Codex 混合检索

启用 Tavily + Codex：

```dotenv
RETRIEVER=tavily,codex
TAVILY_API_KEY=replace-me
CODEX_SEARCH_MODE=search
CODEX_SEARCH_REASONING_EFFORT=medium
CODEX_SEARCH_SERVICE_TIER=fast
```

Compose 默认从宿主机的 `${HOME}/.codex/auth.json` 引导 Codex 认证。先在
宿主机确认 Codex 已登录：

```sh
codex login status
```

如果认证文件不在默认位置，在 `.env` 中指定绝对路径：

```dotenv
CODEX_AUTH_FILE=/absolute/path/to/auth.json
```

宿主机认证文件只读挂载到 `/run/codex-auth.json`。首次启动时，入口脚本将
它复制到 `gpt-researcher-codex-home` 命名卷，供容器内 Codex CLI 刷新
令牌。认证文件不会进入镜像或源码卷。

也可以通过外置 `.env` 提供 `CODEX_API_KEY` 或 `CODEX_ACCESS_TOKEN`。如果
`RETRIEVER` 包含 `codex`，但镜像中没有 Codex CLI 或没有任何可用认证，
容器会立即退出并给出明确错误。

## 选择上游和版本

默认上游与分支：

```dotenv
GPT_RESEARCHER_REPOSITORY=https://github.com/mizorewww/gpt-researcher.git
GPT_RESEARCHER_REVISION=main
```

可在 `.env` 中覆盖这两个值。`GPT_RESEARCHER_REVISION` 应为远程分支或
tag。由于启动过程使用 `uv sync --frozen`，自定义上游必须提交与
`pyproject.toml` 匹配的 `uv.lock`。

## 构建并启动

在仓库根目录执行：

```sh
docker compose -f docker-compose.mcp.yml up --build -d
```

首次启动会依次完成：

1. 构建 Debian、uv 和 Codex CLI 运行镜像。
2. 在源码卷中 clone 上游。
3. 按 `uv.lock` 创建 Python 虚拟环境。
4. 启动 MCP Streamable HTTP 服务。

首次依赖同步可能需要数分钟。查看实时日志：

```sh
docker compose -f docker-compose.mcp.yml logs -f gpt-researcher-mcp
```

看到以下内容表示服务已就绪：

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:8811
```

检查容器：

```sh
docker compose -f docker-compose.mcp.yml ps
```

## 验证 MCP

### 初始化请求

从宿主机发送 MCP 初始化请求：

```sh
curl --fail --silent --show-error \
  --request POST http://127.0.0.1:8811/mcp \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --header 'MCP-Protocol-Version: 2025-03-26' \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {
        "name": "docker-smoke-test",
        "version": "1.0"
      }
    }
  }'
```

正常响应中应包含：

```json
{
  "serverInfo": {
    "name": "gpt-researcher-codex-long"
  }
}
```

实际响应使用 Server-Sent Events 包装，因此终端还会显示 `event:` 和
`data:` 前缀。

### FastMCP 客户端

创建 `/tmp/test_gpt_researcher_mcp.py`：

```python
import asyncio

from fastmcp import Client


async def main() -> None:
    async with Client("http://127.0.0.1:8811/mcp") as client:
        tools = await client.list_tools()
    names = sorted(tool.name for tool in tools)
    assert "research_report" in names, names
    print("MCP tools:", ", ".join(names))


if __name__ == "__main__":
    asyncio.run(main())
```

使用 uv 临时安装 FastMCP 并执行：

```sh
uv run --with 'fastmcp>=3' python /tmp/test_gpt_researcher_mcp.py
```

预期输出：

```text
MCP tools: research_report
```

### 验证容器内 Codex

仅当 `RETRIEVER` 包含 `codex` 时需要：

```sh
docker compose -f docker-compose.mcp.yml exec \
  --user 10001:10001 \
  gpt-researcher-mcp codex --version

docker compose -f docker-compose.mcp.yml exec \
  --user 10001:10001 \
  gpt-researcher-mcp codex login status
```

## ARM64 与 AMD64

在原生 Debian ARM64 或 AMD64 主机上直接运行 Compose 即可，Docker 会选择
匹配架构的 Debian、uv 和 Codex CLI。

单独验证 ARM64 构建：

```sh
docker buildx build \
  --platform linux/arm64 \
  --tag gpt-researcher-mcp:arm64 \
  --file docker/mcp/Dockerfile \
  --load .
```

单独验证 AMD64 构建：

```sh
docker buildx build \
  --platform linux/amd64 \
  --tag gpt-researcher-mcp:amd64 \
  --file docker/mcp/Dockerfile \
  --load .
```

构建并推送多架构 manifest：

```sh
docker buildx build \
  --platform linux/arm64,linux/amd64 \
  --tag registry.example.com/gpt-researcher-mcp:latest \
  --file docker/mcp/Dockerfile \
  --push .
```

## 数据与持久卷

Compose 创建以下命名卷：

| 卷 | 内容 | 是否建议持久化 |
| --- | --- | --- |
| `gpt-researcher-source` | 运行时 clone 的上游源码、输出和任务审计文件 | 是 |
| `gpt-researcher-venv` | uv 创建的 Python 虚拟环境 | 可重建 |
| `gpt-researcher-uv-cache` | Python 包下载缓存 | 可重建 |
| `gpt-researcher-codex-home` | 容器内 Codex 认证和状态 | 是，敏感 |

实际卷名会带 Compose project 前缀。查看卷：

```sh
docker volume ls --filter label=com.docker.compose.project
```

停止服务但保留所有数据：

```sh
docker compose -f docker-compose.mcp.yml down
```

删除容器、网络和全部命名卷：

```sh
docker compose -f docker-compose.mcp.yml down -v
```

`down -v` 会删除运行时源码、研究输出、虚拟环境、缓存以及容器内 Codex
认证，操作不可恢复。执行前应确认不再需要这些数据。

## 更新与回滚

### 拉取上游最新提交

每次容器启动都会 fetch `GPT_RESEARCHER_REVISION`。更新默认 `main`：

```sh
docker compose -f docker-compose.mcp.yml restart gpt-researcher-mcp
```

入口脚本会 checkout 最新的 `FETCH_HEAD`，然后执行冻结依赖同步。

### 重建基础镜像

更新 Debian、uv 或 Codex CLI：

```sh
docker compose -f docker-compose.mcp.yml build --pull gpt-researcher-mcp
docker compose -f docker-compose.mcp.yml up -d
```

### 固定或回滚版本

在 `.env` 中将 `GPT_RESEARCHER_REVISION` 改为稳定 tag 或维护分支，然后：

```sh
docker compose -f docker-compose.mcp.yml restart gpt-researcher-mcp
```

## 常用运维命令

```sh
# 查看状态
docker compose -f docker-compose.mcp.yml ps

# 跟踪日志
docker compose -f docker-compose.mcp.yml logs -f --tail=200

# 重启
docker compose -f docker-compose.mcp.yml restart gpt-researcher-mcp

# 查看容器内检出的提交
docker compose -f docker-compose.mcp.yml exec gpt-researcher-mcp \
  git -C /workspace/gpt-researcher rev-parse HEAD

# 查看磁盘占用
docker system df

# 停止并保留数据
docker compose -f docker-compose.mcp.yml down
```

## 故障排查

### 连接被拒绝或返回 Empty reply

首次启动时，端口已经发布，但 uv 可能仍在同步依赖。先检查：

```sh
docker compose -f docker-compose.mcp.yml ps
docker compose -f docker-compose.mcp.yml logs --tail=200
```

等待日志出现 `Application startup complete` 后再发送 MCP 请求。

### `No such file or directory: 'codex'`

当前镜像过旧，尚未包含 Codex CLI。强制重新构建并启动：

```sh
docker compose -f docker-compose.mcp.yml build --no-cache gpt-researcher-mcp
docker compose -f docker-compose.mcp.yml up -d
```

随后运行 `codex --version` 验证。

### `no Codex authentication was provided`

确认：

```sh
codex login status
test -f "${CODEX_AUTH_FILE:-${HOME}/.codex/auth.json}"
```

若认证文件位于其他位置，在 `.env` 中设置 `CODEX_AUTH_FILE`，再重新创建
容器：

```sh
docker compose -f docker-compose.mcp.yml up -d --force-recreate
```

### `Unable to find lockfile` 或锁文件不匹配

运行时使用 `uv sync --frozen`。确认所选上游分支或 tag 已提交 `uv.lock`，
并且它与 `pyproject.toml` 一致。在上游工作区执行：

```sh
uv lock
git add uv.lock
git commit -m "build: update uv lockfile"
```

然后重启容器，让运行时源码卷抓取新提交。

### `too few HTTP evidence sources`

这不是 MCP 传输故障，而是研究质量门槛未通过。先检查失败结果中的
`codex_runs`：

- 如果出现 `FileNotFoundError: codex`，重建镜像。
- 如果出现认证错误，检查容器内 `codex login status`。
- 如果 Codex 调用成功但来源仍不足，可调整检索问题、检索器组合或
  `MCP_RESEARCH_MIN_HTTP_SOURCES`，但降低门槛会削弱报告质量。

### 端口 8811 被占用

找出占用者：

```sh
ss -ltnp | grep ':8811'
```

如需改端口，同时修改 Compose 的宿主机端口和容器内 `MCP_PORT`。例如改为
`8822`：

```yaml
environment:
  MCP_PORT: "8822"
ports:
  - "127.0.0.1:8822:8822"
```

### 查看 Compose 展开结果

```sh
docker compose -f docker-compose.mcp.yml config --quiet
```

不要把完整的 `docker compose config` 输出粘贴到公开日志或工单中，因为它
会展开 `.env` 中的密钥。

## 安全建议

- 保持 `127.0.0.1:8811:8811` 绑定；不要在没有认证、TLS 和反向代理访问
  控制的情况下改成 `0.0.0.0`。
- 将 `.env` 和 Codex `auth.json` 权限设为 `0600`。
- 不要把 `gpt-researcher-codex-home` 卷导出到不可信位置。
- Docker 管理员能够读取容器环境变量和命名卷，应限制 Docker daemon
  访问权限。
- 研究调用可能产生模型、搜索 API 和 Codex 用量费用，应设置服务侧预算与
  告警。
