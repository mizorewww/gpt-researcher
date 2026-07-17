# Docker MCP 服务

此部署针对 Debian arm64 与 amd64；Docker 会选择基础镜像和 `uv` 的对应架构。
镜像不包含项目源码：每次容器启动时，入口脚本会在持久卷中首次 clone 上游，随后抓取指定 ref 的最新提交。

1. 在 Compose 文件旁准备外置凭据文件：`cp .env.example .env`，再填写所需 API key。`.env` 不会被复制到镜像或 clone 的仓库中。
   如果 `RETRIEVER` 包含 `codex`，Compose 默认从 `${HOME}/.codex/auth.json`
   引导容器认证；也可设置 `CODEX_AUTH_FILE=/path/to/auth.json`。认证会复制到
   独立的 `gpt-researcher-codex-home` 卷中，以便 Codex CLI 刷新令牌。uv
   下载缓存和虚拟环境也分别保存在命名卷中，后续重建无需重新下载全部依赖。
2. 启动：

   ```sh
   docker compose -f docker-compose.mcp.yml up --build -d
   ```

3. MCP Streamable HTTP 地址为 `http://localhost:8811/mcp`。端口只绑定到本机回环地址，局域网无法访问。

可在外置 `.env` 中覆写以下可选值：

```dotenv
GPT_RESEARCHER_REPOSITORY=https://github.com/mizorewww/gpt-researcher.git
GPT_RESEARCHER_REVISION=main
```

检查服务与停止服务：

```sh
docker compose -f docker-compose.mcp.yml logs -f
docker compose -f docker-compose.mcp.yml down
```

如需连同持久的源码与虚拟环境卷一并删除，执行 `docker compose -f docker-compose.mcp.yml down -v`。
