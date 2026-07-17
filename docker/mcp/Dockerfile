# syntax=docker/dockerfile:1
# The image deliberately contains no application source. The entrypoint clones it
# into a named volume when the container starts.
FROM debian:bookworm-slim

ARG TARGETARCH
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git tini util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && git config --system --add safe.directory /workspace/gpt-researcher

ENV HOME=/home/app \
    UV_CACHE_DIR=/home/app/.cache/uv \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
COPY docker/mcp/entrypoint.sh /usr/local/bin/gpt-researcher-mcp
COPY docker/mcp/run_mcp.py /usr/local/lib/gpt-researcher-mcp-runner.py
RUN chmod 0755 /usr/local/bin/gpt-researcher-mcp

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/gpt-researcher-mcp"]
