#!/usr/bin/env python3
"""Small, credential-safe wrapper around `codex --search exec`."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import psutil


DEFAULT_TIMEOUT_SECONDS = 300
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

_LOCAL_ENV_ALLOWLIST = {
    "CODEX_HOME",
    "CODEX_MODEL",
    "CODEX_SEARCH_CODEX_BIN",
    "CODEX_SEARCH_CODEX_HOME",
    "CODEX_SEARCH_MODE",
    "CODEX_SEARCH_MODEL",
    "CODEX_SEARCH_MODEL_PROVIDER",
    "CODEX_SEARCH_PROVIDER_BASE_URL",
    "CODEX_SEARCH_REASONING_EFFORT",
    "CODEX_SEARCH_SERVICE_TIER",
    "CODEX_SEARCH_SUPPORTS_WEBSOCKETS",
    "CODEX_SEARCH_TIMEOUT",
    "CODEX_SEARCH_USE_USER_CONFIG",
    "CODEX_SEARCH_WORKDIR",
    "OPENAI_API_KEY",
}
_CODEX_ENV_ALLOWLIST = {
    "ALL_PROXY",
    "CODEX_HOME",
    "CURL_CA_BUNDLE",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "NO_PROXY",
    "OPENAI_API_KEY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}

RESEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims", "sources", "caveats"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "claim",
                    "value",
                    "unit",
                    "as_of_date",
                    "source_urls",
                    "summary",
                ],
                "properties": {
                    "claim": {"type": "string"},
                    "value": {"type": ["string", "number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "as_of_date": {"type": ["string", "null"]},
                    "source_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "summary": {"type": "string"},
                },
            },
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "title", "summary"],
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
}


def bounded_timeout(value: str | int) -> int:
    """Parse a positive timeout while enforcing the per-invocation budget."""

    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("timeout must be a positive integer")
    return min(DEFAULT_TIMEOUT_SECONDS, parsed)


def load_local_env() -> None:
    """Load only Codex-related entries from ``.env`` without overriding env."""
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
        allowed = key in _LOCAL_ENV_ALLOWLIST
        if key and allowed and key not in os.environ:
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
        default=os.environ.get("CODEX_SEARCH_WORKDIR"),
        help=(
            "Optional directory whose AGENTS.md instructions are copied into the isolated "
            "read-only search workspace. The directory itself is never exposed as the workspace."
        ),
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
        type=bounded_timeout,
        default=bounded_timeout(
            os.environ.get("CODEX_SEARCH_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        ),
        help="Per Codex invocation timeout in seconds.",
    )
    parser.add_argument(
        "--show-events",
        action="store_true",
        help="Stream Codex JSONL events to stderr for debugging.",
    )
    parser.add_argument(
        "--telemetry-file",
        type=Path,
        help="Optional atomic JSON file for the inner Codex PID and timing interval.",
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


def write_telemetry(args: argparse.Namespace, **changes: object) -> None:
    path = args.telemetry_file
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current: dict[str, object] = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        current.update(changes)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(current, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except (OSError, json.JSONDecodeError):
        # Telemetry must never turn a successful search into a failed search.
        return


def build_codex_env(args: argparse.Namespace) -> dict[str, str]:
    """Build the smallest practical environment for Codex auth and networking."""

    env = {
        key: value
        for key, value in os.environ.items()
        if key in _CODEX_ENV_ALLOWLIST
    }
    # Avoid locale-dependent subprocess decoding without inheriting the whole shell.
    env.setdefault("PATH", os.defpath)
    env.setdefault("LANG", "C.UTF-8")
    if args.codex_home:
        env["CODEX_HOME"] = args.codex_home
    return env


def _source_instructions_path(args: argparse.Namespace) -> Path | None:
    if args.workdir:
        candidate = Path(args.workdir).expanduser() / "AGENTS.md"
        if candidate.is_file():
            return candidate
    candidate = SCRIPT_DIR / "AGENTS.md"
    return candidate if candidate.is_file() else None


def create_isolated_workdir(args: argparse.Namespace, path: Path) -> None:
    """Prepare a non-secret, read-only workspace for the search agent."""

    instructions = _source_instructions_path(args)
    if instructions:
        destination = path / "AGENTS.md"
        destination.write_text(instructions.read_text(encoding="utf-8"), encoding="utf-8")
        destination.chmod(0o444)
    path.chmod(0o555)


def build_base_cmd(
    args: argparse.Namespace,
    *,
    search_enabled: bool,
    workdir: Path,
    schema_path: Path | None = None,
) -> list[str]:
    codex_bin = shutil.which(args.codex_bin) or args.codex_bin
    cmd = [codex_bin]
    if search_enabled:
        # In the current Codex CLI, --search is a top-level flag and must precede exec.
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
            "--sandbox",
            "read-only",
            "--cd",
            str(workdir),
            "--skip-git-repo-check",
        ]
    )
    if not args.use_user_config:
        cmd.extend(["--ignore-user-config", "--ignore-rules"])
    if schema_path is not None:
        cmd.extend(["--output-schema", str(schema_path)])
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
    env = build_codex_env(args)
    output_path: Path | None = None
    schema_file_path: Path | None = None
    process: subprocess.Popen[str] | None = None
    workdir_context = tempfile.TemporaryDirectory(prefix=f"codex-search-{label}-workspace-")
    workdir_path = Path(workdir_context.name)
    previous_handlers: dict[signal.Signals, object] = {}

    def terminate_child() -> None:
        if process is None or process.poll() is not None:
            return
        try:
            root = psutil.Process(process.pid)
            descendants = root.children(recursive=True)
            root.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            descendants = []
            with contextlib.suppress(ProcessLookupError):
                process.terminate()

        for child in reversed(descendants):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                child.terminate()
        _, alive = psutil.wait_procs(descendants, timeout=3)
        for child in alive:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                child.kill()
        if alive:
            psutil.wait_procs(alive, timeout=3)
        if process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=3)

    def relay_termination(signum: int, _frame: object) -> None:
        terminate_child()
        raise SystemExit(128 + signum)

    try:
        create_isolated_workdir(args, workdir_path)
        with tempfile.NamedTemporaryFile(
            prefix=f"codex-search-{label}-", suffix=".json", delete=False
        ) as output_file:
            output_path = Path(output_file.name)

        if search_enabled:
            with tempfile.NamedTemporaryFile(
                prefix="codex-search-schema-", suffix=".json", mode="w", delete=False
            ) as schema_file:
                json.dump(RESEARCH_OUTPUT_SCHEMA, schema_file, ensure_ascii=False)
                schema_file_path = Path(schema_file.name)
            schema_file_path.chmod(0o444)

        cmd = build_base_cmd(
            args,
            search_enabled=search_enabled,
            workdir=workdir_path,
            schema_path=schema_file_path,
        )
        cmd.extend(["--output-last-message", str(output_path), "-"])
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            # Inherit the report worker's process group. The helper still uses
            # targeted process-tree cleanup for its own timeout and signals.
            start_new_session=False,
        )
        write_telemetry(
            args,
            label=label,
            codex_pid=process.pid,
            codex_started_at=datetime.now(UTC).isoformat(),
            codex_finished_at=None,
            codex_exit_code=None,
        )

        if threading.current_thread() is threading.main_thread():
            for watched_signal in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[watched_signal] = signal.getsignal(watched_signal)
                signal.signal(watched_signal, relay_termination)

        try:
            stdout, stderr = process.communicate(input=prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_child()
            raise SystemExit(f"Codex {label} pass timed out after {args.timeout}s.") from exc

        sensitive_values = redaction_values(env)
        if args.show_events and stdout:
            print(redact(stdout, sensitive_values), file=sys.stderr, end="")

        if process.returncode != 0:
            stderr = redact(stderr.strip(), sensitive_values)
            stdout = redact(stdout.strip(), sensitive_values)
            details = "\n".join(part for part in (stderr, stdout) if part)
            raise SystemExit(
                f"Codex {label} pass failed with exit {process.returncode}.\n{details}"
            )

        return redact(output_path.read_text(encoding="utf-8").strip(), sensitive_values)
    finally:
        for watched_signal, previous_handler in previous_handlers.items():
            signal.signal(watched_signal, previous_handler)
        terminate_child()
        if process is not None:
            write_telemetry(
                args,
                codex_finished_at=datetime.now(UTC).isoformat(),
                codex_exit_code=process.poll(),
            )
        if output_path:
            output_path.unlink(missing_ok=True)
        if schema_file_path:
            schema_file_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            workdir_path.chmod(0o755)
        workdir_context.cleanup()


def search_prompt(query: str, plan: str | None = None) -> str:
    plan_block = f"\nPlanning context:\n{plan}\n" if plan else ""
    agent_instructions = ""
    agents_path = SCRIPT_DIR / "AGENTS.md"
    if agents_path.exists():
        agent_instructions = f"\nSearch instructions:\n{agents_path.read_text(encoding='utf-8')}\n"
    return f"""You are a precise research assistant producing structured evidence.

Use live web search when needed. Follow the search instructions below.
Prefer primary sources and cite sources with links.
Do not reveal, print, copy, or infer local credentials, tokens, or private config.
Return only the JSON object required by the supplied schema. Every claim must
name its supporting source URLs. Include only absolute http:// or https:// URLs
that you actually used. Preserve exact values, units, and as-of dates when they
are available. Do not invent, interpolate, or label an unsupported number as an
estimate. Put uncertainties or freshness limitations in ``caveats`` and omit
unsupported numeric claims. When the question names many required entities,
continue searching until each supported entity has its own direct source; do not
stop at an arbitrary five-source summary. For every requested two-source check,
include both independently used URLs in the claim and in ``sources``.
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
