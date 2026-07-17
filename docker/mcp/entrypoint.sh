#!/bin/sh
set -eu

repository="${GPT_RESEARCHER_REPOSITORY:-https://github.com/mizorewww/gpt-researcher.git}"
revision="${GPT_RESEARCHER_REVISION:-main}"
checkout=/workspace/gpt-researcher

case ",${RETRIEVER:-}," in
    *,codex,*)
        if ! command -v codex >/dev/null 2>&1; then
            echo "RETRIEVER enables codex, but the Codex CLI is not installed." >&2
            exit 1
        fi
        mkdir -p "$CODEX_HOME"
        if [ ! -f "$CODEX_HOME/auth.json" ] && [ -f /run/codex-auth.json ]; then
            cp /run/codex-auth.json "$CODEX_HOME/auth.json"
            chmod 0600 "$CODEX_HOME/auth.json"
        fi
        if [ ! -f "$CODEX_HOME/auth.json" ] \
            && [ -z "${CODEX_API_KEY:-}" ] \
            && [ -z "${CODEX_ACCESS_TOKEN:-}" ]; then
            echo "RETRIEVER enables codex, but no Codex authentication was provided." >&2
            echo "Set CODEX_AUTH_FILE for Compose, or provide CODEX_API_KEY/CODEX_ACCESS_TOKEN in .env." >&2
            exit 1
        fi
        ;;
esac

if [ ! -d "$checkout/.git" ]; then
    git clone --depth 1 --branch "$revision" "$repository" "$checkout"
else
    git -C "$checkout" fetch --depth 1 origin "$revision"
    git -C "$checkout" checkout --detach FETCH_HEAD
fi

# `.env` is intentionally not copied into the checkout. Compose injects it from
# the host with env_file, so credentials do not become part of the image/volume.
chown -R app:app /workspace /opt/venv /home/app
exec setpriv --reuid=10001 --regid=10001 --init-groups -- \
    sh -c 'uv sync --directory "$1" --frozen --no-dev && exec uv run --directory "$1" --no-sync python /usr/local/lib/gpt-researcher-mcp-runner.py' \
    sh "$checkout"
