#!/bin/sh
set -eu

repository="${GPT_RESEARCHER_REPOSITORY:-https://github.com/mizorewww/gpt-researcher.git}"
revision="${GPT_RESEARCHER_REVISION:-main}"
checkout=/workspace/gpt-researcher

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
    sh -c 'uv sync --directory "$1" --no-dev && exec uv run --directory "$1" --no-sync python /usr/local/lib/gpt-researcher-mcp-runner.py' \
    sh "$checkout"
