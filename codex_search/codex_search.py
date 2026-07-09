#!/usr/bin/env python3
"""Small, credential-safe wrapper around `codex --search exec`."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 900
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def load_local_env() -> None:
    """Load simple KEY=VALUE entries from the repo .env without overriding env."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    load_local_env()
    parser = argparse.ArgumentParser(
        description="Run a bounded non-interactive Codex web search helper."
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="Search question. Use '-' or omit it to read the question from stdin.",
    )
    parser.add_argument(
        "--mode",
        choices=("search", "plan-exec"),
        default=os.environ.get("CODEX_SEARCH_MODE", "search"),
        help="search is one Codex run; plan-exec adds a planning pass first.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CODEX_SEARCH_MODEL") or os.environ.get("CODEX_MODEL"),
        help="Codex model. Defaults to CODEX_SEARCH_MODEL, then CODEX_MODEL, then CLI config.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=os.environ.get("CODEX_SEARCH_REASONING_EFFORT") or os.environ.get("CODEX_REASONING_EFFORT"),
        help="Optional Codex model_reasoning_effort override, e.g. medium.",
    )
    parser.add_argument(
        "--service-tier",
        default=os.environ.get("CODEX_SEARCH_SERVICE_TIER") or os.environ.get("CODEX_SERVICE_TIER"),
        help="Optional Codex service_tier override, e.g. priority.",
    )
    parser.add_argument(
        "--model-provider",
        default=os.environ.get("CODEX_SEARCH_MODEL_PROVIDER", "chatgpt-http"),
        help="Codex model_provider override. Defaults to a ChatGPT HTTPS-only provider.",
    )
    parser.add_argument(
        "--provider-base-url",
        default=os.environ.get("CODEX_SEARCH_PROVIDER_BASE_URL", "https://chatgpt.com/backend-api/codex"),
        help="Base URL for the HTTPS-only Codex provider.",
    )
    parser.add_argument(
        "--supports-websockets",
        action="store_true",
        default=os.environ.get("CODEX_SEARCH_SUPPORTS_WEBSOCKETS", "").lower() in {"1", "true", "yes"},
        help="Allow Responses WebSocket transport for the custom provider. Default is false.",
    )
    parser.add_argument(
        "--workdir",
        default=os.environ.get("CODEX_SEARCH_WORKDIR", str(SCRIPT_DIR)),
        help="Workspace passed to codex exec via --cd. Defaults to this helper directory so AGENTS.md is loaded.",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("CODEX_SEARCH_CODEX_BIN", "codex"),
        help="Codex executable path or name.",
    )
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_SEARCH_CODEX_HOME"),
        help="Optional CODEX_HOME override. Auth is inherited; credentials are never printed.",
    )
    parser.add_argument(
        "--use-user-config",
        action="store_true",
        default=os.environ.get("CODEX_SEARCH_USE_USER_CONFIG", "").lower() in {"1", "true", "yes"},
        help="Load the user's Codex config/plugins. Default is off for predictable search runs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional file to receive the final answer.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("CODEX_SEARCH_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
        help="Per Codex invocation timeout in seconds.",
    )
    parser.add_argument(
        "--show-events",
        action="store_true",
        help="Stream Codex JSONL events to stderr for debugging.",
    )
    return parser.parse_args()


def read_query(parts: list[str]) -> str:
    if not parts or parts == ["-"]:
        query = sys.stdin.read()
    else:
        query = " ".join(parts)
    query = query.strip()
    if not query:
        raise SystemExit("No query provided.")
    return query


def redaction_values(env: dict[str, str]) -> list[str]:
    sensitive_markers = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
    values = []
    for key, value in env.items():
        if len(value) >= 8 and any(marker in key.upper() for marker in sensitive_markers):
            values.append(value)
    return sorted(values, key=len, reverse=True)


def redact(text: str, values: list[str]) -> str:
    redacted = text
    for value in values:
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def build_base_cmd(args: argparse.Namespace, *, search_enabled: bool) -> list[str]:
    codex_bin = shutil.which(args.codex_bin) or args.codex_bin
    cmd = [codex_bin]
    if search_enabled:
        # In codex-cli 0.142.5, --search is a top-level flag and must precede exec.
        cmd.append("--search")
    cmd.extend(["--ask-for-approval", "never"])
    if args.model_provider:
        provider = args.model_provider
        supports_websockets = "true" if args.supports_websockets else "false"
        cmd.extend(
            [
                "-c",
                f'model_provider="{provider}"',
                "-c",
                f'model_providers.{provider}.name="ChatGPT HTTP"',
                "-c",
                f'model_providers.{provider}.base_url="{args.provider_base_url}"',
                "-c",
                f'model_providers.{provider}.wire_api="responses"',
                "-c",
                f"model_providers.{provider}.requires_openai_auth=true",
                "-c",
                f"model_providers.{provider}.supports_websockets={supports_websockets}",
            ]
        )
    if args.reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{args.reasoning_effort}"'])
    if args.service_tier:
        cmd.extend(["-c", f'service_tier="{args.service_tier}"'])
        if args.service_tier == "fast":
            cmd.extend(["-c", "features.fast_mode=true"])
    cmd.extend(
        [
            "exec",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--cd",
            args.workdir,
        ]
    )
    if not args.use_user_config:
        cmd.append("--ignore-user-config")
    if args.model:
        cmd.extend(["--model", args.model])
    return cmd


def run_codex(
    args: argparse.Namespace,
    prompt: str,
    *,
    search_enabled: bool,
    label: str,
) -> str:
    env = os.environ.copy()
    if args.codex_home:
        env["CODEX_HOME"] = args.codex_home

    with tempfile.NamedTemporaryFile(
        prefix=f"codex-search-{label}-", suffix=".md", delete=False
    ) as output_file:
        output_path = Path(output_file.name)

    cmd = build_base_cmd(args, search_enabled=search_enabled)
    cmd.extend(["--output-last-message", str(output_path), "-"])

    try:
        completed = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=args.timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise SystemExit(f"Codex {label} pass timed out after {args.timeout}s.") from exc

    sensitive_values = redaction_values(env)
    if args.show_events and completed.stdout:
        print(redact(completed.stdout, sensitive_values), file=sys.stderr, end="")

    if completed.returncode != 0:
        stderr = redact(completed.stderr.strip(), sensitive_values)
        stdout = redact(completed.stdout.strip(), sensitive_values)
        output_path.unlink(missing_ok=True)
        details = "\n".join(part for part in (stderr, stdout) if part)
        raise SystemExit(f"Codex {label} pass failed with exit {completed.returncode}.\n{details}")

    final = output_path.read_text(encoding="utf-8").strip()
    output_path.unlink(missing_ok=True)
    return final


def search_prompt(query: str, plan: str | None = None) -> str:
    plan_block = f"\nPlanning context:\n{plan}\n" if plan else ""
    agent_instructions = ""
    agents_path = SCRIPT_DIR / "AGENTS.md"
    if agents_path.exists():
        agent_instructions = f"\nSearch instructions:\n{agents_path.read_text(encoding='utf-8')}\n"
    return f"""You are a concise research assistant.

Use live web search when needed. Follow the search instructions below.
Prefer primary sources and cite sources with links.
Do not reveal, print, copy, or infer local credentials, tokens, or private config.
Return a direct answer with:
- Findings
- Sources
- Caveats or freshness notes, if relevant
{agent_instructions}
{plan_block}
Question:
{query}
"""


def plan_prompt(query: str) -> str:
    return f"""Create a brief research plan for answering this question.

Do not browse, do not inspect private credentials, and do not produce the final answer.
List the likely source types to check and the specific ambiguities to resolve.

Question:
{query}
"""


def main() -> int:
    args = parse_args()
    query = read_query(args.query)

    if args.mode == "plan-exec":
        plan = run_codex(
            args,
            plan_prompt(query),
            search_enabled=False,
            label="plan",
        )
        answer = run_codex(
            args,
            search_prompt(query, plan=plan),
            search_enabled=True,
            label="search",
        )
    else:
        answer = run_codex(
            args,
            search_prompt(query),
            search_enabled=True,
            label="search",
        )

    if args.output:
        args.output.write_text(answer + "\n", encoding="utf-8")
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
