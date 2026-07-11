from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psutil
from dotenv import dotenv_values
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from gpt_researcher.job_manager import default_global_slot_root

from .config import WorkflowSpec, load_workflow


_FORMAT_CHECKER = FormatChecker()
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\[\]()|]+", re.IGNORECASE)
_TOOL_PERMISSION_RE = re.compile(r"\bevaluated permission=([^\s]+)")
_SAFE_INHERITED_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "CODEX_HOME",
}


def validate_json(value: Any, schema: dict[str, Any]) -> None:
    Draft202012Validator(schema, format_checker=_FORMAT_CHECKER).validate(value)


def validate_run_id(value: str) -> str:
    if not _RUN_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(
            "run_id must use only letters, digits, dot, underscore, or hyphen and stay within 128 characters"
        )
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _private_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _open_private_binary(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(descriptor, "wb")


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)


def _freeze_tree(root: Path) -> None:
    for path in sorted((item for item in root.rglob("*") if item.is_file())):
        executable = bool(path.stat().st_mode & 0o111)
        path.chmod(0o500 if executable else 0o400)
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        path.chmod(0o500)
    root.chmod(0o500)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_tree(root: Path) -> tuple[str, dict[str, str]]:
    files: dict[str, str] = {}
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        checksum = _sha256_bytes(path.read_bytes())
        files[relative] = checksum
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(checksum.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), files


def snapshot_integrity(root: Path, expected_sha256: str) -> dict[str, Any]:
    actual_sha256, _ = hash_tree(root)
    return {
        "passed": actual_sha256 == expected_sha256,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
    }


def _redact(text: str, env: dict[str, str]) -> str:
    redacted = text
    for key, value in env.items():
        if not value or len(value) < 8:
            continue
        if (
            key not in _SAFE_INHERITED_ENV
            and not key.startswith(("RESEARCH_WORKFLOW_", "XDG_"))
            and key not in {"GPT_RESEARCHER_PROFILE_DIR", "OPENCODE_PURE"}
        ):
            redacted = redacted.replace(value, f"<redacted:{key}>")
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "<redacted:key>", redacted)
    redacted = re.sub(
        r"(?i)(authorization:\s*(?:bearer|basic)\s+)\S+", r"\1<redacted>", redacted
    )
    redacted = re.sub(
        r"([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@/\s]+@", r"\1<redacted>@", redacted
    )
    return redacted[-4000:]


@dataclass(frozen=True)
class RunLayout:
    run_id: str
    runtime_dir: Path
    artifact_dir: Path
    log_dir: Path
    snapshot_dir: Path
    manifest_path: Path
    event_path: Path
    xdg_config: Path
    xdg_data: Path
    xdg_cache: Path
    xdg_state: Path
    jobs_dir: Path
    codex_slots_dir: Path
    global_job_slots_dir: Path


@dataclass(frozen=True, order=True)
class ProcessIdentity:
    """A PID bound to its creation time so a recycled PID is never signalled."""

    pid: int
    create_time: float


@dataclass
class SessionRun:
    index: int
    input_payload: str
    command: list[str]
    process: subprocess.Popen[bytes]
    log_path: Path
    log_handle: Any
    started_at: str
    started_monotonic: float
    finished_at: str | None = None
    finished_monotonic: float | None = None
    exit_code: int | None = None
    response_path: Path | None = None
    response_sha256: str | None = None
    session_id: str | None = None
    tool_calls: tuple[str, ...] = ()
    http_sources: tuple[str, ...] = ()
    result: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()

    @property
    def elapsed_seconds(self) -> float | None:
        if self.finished_monotonic is None:
            return None
        return round(self.finished_monotonic - self.started_monotonic, 3)


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        _private_mkdir(path.parent)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)

    def emit(self, event: str, **fields: Any) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"timestamp": iso_now(), "event": event, **fields},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            handle.write("\n")


class ToolPermissionLogMonitor:
    """Incrementally count OpenCode permission decisions for one isolated run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.counts: dict[str, int] = {}
        self.coverage = "unavailable"
        self.error: str | None = None
        self._handle: Any | None = None
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""
        self._tail = b""
        self._tail_start = 0
        try:
            if path.is_file():
                self._open(start_at_end=True)
        except OSError as exc:
            self._mark_error(exc)

    def _mark_incomplete(self, reason: str) -> None:
        if self.coverage != "error":
            self.coverage = "incomplete"
        if self.error is None:
            self.error = reason

    def _mark_error(self, exc: OSError) -> None:
        self.coverage = "error"
        self.error = f"{type(exc).__name__}: {exc}"

    def _open(self, *, start_at_end: bool) -> None:
        handle = self.path.open("rb")
        stat = os.fstat(handle.fileno())
        self._handle = handle
        self._identity = (stat.st_dev, stat.st_ino)
        if start_at_end:
            handle.seek(0, os.SEEK_END)
        self._offset = handle.tell()
        if self.coverage == "unavailable":
            self.coverage = "complete"
        self._remember_tail()

    def _remember_tail(self) -> None:
        if self._handle is None:
            return
        position = self._handle.tell()
        self._tail_start = max(0, position - 64)
        self._handle.seek(self._tail_start)
        self._tail = self._handle.read(position - self._tail_start)
        self._handle.seek(position)

    def _tail_matches(self) -> bool:
        if self._handle is None or not self._tail:
            return True
        position = self._handle.tell()
        self._handle.seek(self._tail_start)
        actual = self._handle.read(len(self._tail))
        self._handle.seek(position)
        return actual == self._tail

    def _consume(self, chunk: bytes, *, final: bool) -> None:
        buffered = self._partial + chunk
        lines = buffered.split(b"\n")
        self._partial = lines.pop()
        if final and self._partial:
            lines.append(self._partial)
            self._partial = b""
        for raw_line in lines:
            match = _TOOL_PERMISSION_RE.search(
                raw_line.decode("utf-8", errors="replace")
            )
            if match:
                tool = match.group(1)
                self.counts[tool] = self.counts.get(tool, 0) + 1

    def _drain(self, *, final: bool) -> None:
        if self._handle is None:
            return
        chunk = self._handle.read()
        self._offset = self._handle.tell()
        self._consume(chunk, final=final)
        self._remember_tail()

    def _close(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self._handle = None

    def poll(self, *, final: bool = False) -> dict[str, int]:
        try:
            if self._handle is None:
                if not self.path.is_file():
                    return dict(self.counts)
                self._open(start_at_end=False)

            try:
                path_stat = self.path.stat()
            except FileNotFoundError:
                self._mark_incomplete("permission log disappeared during the run")
                self._drain(final=True)
                self._close()
                return dict(self.counts)

            identity = (path_stat.st_dev, path_stat.st_ino)
            if identity != self._identity:
                self._mark_incomplete("permission log rotated during the run")
                self._drain(final=True)
                self._close()
                self._open(start_at_end=False)
            elif path_stat.st_size < self._offset or not self._tail_matches():
                self._mark_incomplete("permission log was truncated during the run")
                self._handle.seek(0)
                self._offset = 0
                self._partial = b""
                self._tail = b""

            self._drain(final=final)
            if final:
                self._close()
        except OSError as exc:
            self._mark_error(exc)
            self._close()
        return dict(self.counts)


def make_layout(
    project_root: Path,
    workflow_name: str,
    run_id: str,
    base_dir: Path | None = None,
) -> RunLayout:
    validate_run_id(run_id)
    base = base_dir or project_root
    project_fingerprint = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[
        :12
    ]
    base_fingerprint = hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:12]
    # A caller-controlled artifact directory may itself be another Git worktree.
    # Runtime placement must never inherit that tree's AGENTS.md or OpenCode config.
    runtime = (
        Path(tempfile.gettempdir())
        / "gptr-opencode-workflows"
        / project_fingerprint
        / base_fingerprint
        / workflow_name
        / run_id
    )
    slot_root = default_global_slot_root()
    artifacts = base / "outputs" / "workflows" / workflow_name / run_id
    logs = base / "run_logs" / "opencode-workflows" / workflow_name / run_id
    for path in (runtime, artifacts, logs):
        if path.exists():
            raise FileExistsError(f"refusing to reuse workflow run path: {path}")
    return RunLayout(
        run_id=run_id,
        runtime_dir=runtime,
        artifact_dir=artifacts,
        log_dir=logs,
        snapshot_dir=runtime / "workflow",
        manifest_path=artifacts / "manifest.json",
        event_path=logs / "runner.jsonl",
        xdg_config=runtime / "xdg" / "config",
        xdg_data=runtime / "xdg" / "data",
        xdg_cache=runtime / "xdg" / "cache",
        xdg_state=runtime / "xdg" / "state",
        jobs_dir=runtime / "research-jobs",
        codex_slots_dir=slot_root / "codex",
        global_job_slots_dir=slot_root / "reports",
    )


def initialize_layout(layout: RunLayout, workflow_root: Path) -> None:
    for path in (
        layout.runtime_dir,
        layout.artifact_dir,
        layout.log_dir,
        layout.xdg_config,
        layout.xdg_data,
        layout.xdg_cache,
        layout.xdg_state,
        layout.jobs_dir,
        layout.codex_slots_dir,
        layout.global_job_slots_dir,
        layout.runtime_dir / "inputs",
        layout.artifact_dir / "responses",
    ):
        _private_mkdir(path)
    shutil.copytree(workflow_root, layout.snapshot_dir)
    bundled_schema = (
        Path(__file__).resolve().parents[2]
        / "research_workflows"
        / "workflow.schema.json"
    )
    local_schema = layout.snapshot_dir / "schemas" / "workflow.schema.json"
    shutil.copy2(bundled_schema, local_schema)
    snapshot_manifest = layout.snapshot_dir / "workflow.json"
    snapshot_metadata = json.loads(snapshot_manifest.read_text(encoding="utf-8"))
    snapshot_metadata["$schema"] = "schemas/workflow.schema.json"
    _write_private_text(
        snapshot_manifest,
        json.dumps(snapshot_metadata, ensure_ascii=False, indent=2) + "\n",
    )
    opencode_ignore = layout.snapshot_dir / ".opencode" / ".gitignore"
    if not opencode_ignore.exists():
        _write_private_text(
            opencode_ignore,
            "node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore\n",
        )
    _freeze_tree(layout.snapshot_dir)


def _load_project_environment(project_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in dotenv_values(project_root / ".env").items():
        if value is not None:
            env.setdefault(key, value)
    return env


def workflow_environment(
    layout: RunLayout,
    project_root: Path,
    spec: WorkflowSpec,
) -> dict[str, str]:
    available = _load_project_environment(project_root)
    env = {
        key: value
        for key, value in available.items()
        if key in _SAFE_INHERITED_ENV or key in spec.required_env
    }
    env.update(
        {
            "XDG_CONFIG_HOME": str(layout.xdg_config),
            "XDG_DATA_HOME": str(layout.xdg_data),
            "XDG_CACHE_HOME": str(layout.xdg_cache),
            "XDG_STATE_HOME": str(layout.xdg_state),
            "OPENCODE_PURE": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "RESEARCH_WORKFLOW_NAME": spec.name,
            "RESEARCH_WORKFLOW_RUN_ID": layout.run_id,
            "RESEARCH_WORKFLOW_PROJECT_ROOT": str(project_root),
            "RESEARCH_WORKFLOW_RUNTIME_DIR": str(layout.runtime_dir),
            "RESEARCH_WORKFLOW_ARTIFACT_DIR": str(layout.artifact_dir),
            "RESEARCH_WORKFLOW_JOBS_DIR": str(layout.jobs_dir),
            "RESEARCH_WORKFLOW_CODEX_SLOT_DIR": str(layout.codex_slots_dir),
            "RESEARCH_WORKFLOW_GLOBAL_JOB_SLOT_DIR": str(layout.global_job_slots_dir),
            "RESEARCH_WORKFLOW_UV_CACHE_DIR": str(
                Path(os.getenv("UV_CACHE_DIR", Path.home() / ".cache" / "uv"))
                .expanduser()
                .resolve()
            ),
            "RESEARCH_WORKFLOW_NPM_CACHE_DIR": str(
                Path(os.getenv("NPM_CONFIG_CACHE", Path.home() / ".npm"))
                .expanduser()
                .resolve()
            ),
            "GPT_RESEARCHER_PROFILE_DIR": str(project_root),
            "RESEARCH_WORKFLOW_GPTR_BIN": shutil.which("gpt-researcher")
            or "gpt-researcher",
        }
    )
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        _security_overlay(spec), ensure_ascii=False, separators=(",", ":")
    )
    return env


def _permission_overlay(
    patterns: Iterable[str], allowed_agents: Iterable[str]
) -> dict[str, Any]:
    permissions: dict[str, Any] = {"*": "deny"}
    for pattern in patterns:
        if pattern == "task":
            permissions[pattern] = {
                "*": "deny",
                **{agent: "allow" for agent in allowed_agents},
            }
        else:
            permissions[pattern] = "allow"
    return permissions


def _security_overlay(spec: WorkflowSpec) -> dict[str, Any]:
    task_targets = tuple(
        agent for agent in spec.allowed_agents if agent != spec.entry_agent
    )
    permissions = _permission_overlay(spec.allowed_tool_patterns, task_targets)
    agent_overlay = {
        name: {
            "permission": _permission_overlay(
                spec.agent_tool_patterns[name], task_targets
            )
        }
        for name in spec.allowed_agents
    }
    return {"permission": permissions, "agent": agent_overlay}


def _run_preflight_command(
    command: list[str], snapshot: Path, env: dict[str, str], timeout: float = 60
) -> subprocess.CompletedProcess[bytes]:
    tracked: set[ProcessIdentity] = set()
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryFile("w+b") as output:
            process = subprocess.Popen(
                command,
                cwd=snapshot,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _remember_pid(tracked, process.pid)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                tracked.update(_descendant_identities(process.pid))
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"command timed out after {timeout}s: {command[0]} {command[1]}"
                    )
                time.sleep(0.02)
            process.wait()
            output.seek(0)
            stdout = output.read()

        descendants = {identity for identity in tracked if identity.pid != process.pid}
        live_descendants = _live_identities(descendants)
        cleanup_deadline = time.monotonic() + 1
        while live_descendants and time.monotonic() < cleanup_deadline:
            time.sleep(0.05)
            live_descendants = _live_identities(descendants)
        if live_descendants:
            detected = [identity.pid for identity in live_descendants]
            _kill_tracked_identities(live_descendants)
            raise RuntimeError(f"command left child processes after exit: {detected}")
        return subprocess.CompletedProcess(command, process.returncode, stdout)
    except BaseException:
        # Preflight and validators can start local MCP servers. Clean them on
        # timeout, Ctrl-C, and unexpected parser/I/O failures alike.
        if process is not None:
            tracked.update(_descendant_identities(process.pid))
            tracked.update(terminate_process_tree(process))
        _kill_tracked_identities(tracked)
        raise


def _parse_agent_modes(output: str) -> dict[str, str]:
    agents: dict[str, str] = {}
    for line in output.splitlines():
        match = re.fullmatch(r"([^\s]+) \((primary|subagent|all)\)", line.strip())
        if match:
            agents[match.group(1)] = match.group(2)
    return agents


def _parse_skill_names(output: str) -> list[str]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return sorted(
        item["name"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    )


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError(f"could not parse OpenCode version: {value!r}")
    return tuple(int(part) for part in match.groups())


def _minimum_version(value: str | None) -> tuple[int, int, int] | None:
    if value is None:
        return None
    if not value.startswith(">="):
        raise ValueError("requires.opencode currently supports only >=X.Y.Z")
    return _version_tuple(value[2:])


def _connected_mcp_names(output: str) -> set[str]:
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", output)
    return set(re.findall(r"([A-Za-z0-9._-]+)\s+connected\b", cleaned))


def preflight(
    spec: WorkflowSpec,
    snapshot: Path,
    env: dict[str, str],
    opencode_bin: str,
) -> dict[str, Any]:
    version_run = _run_preflight_command([opencode_bin, "--version"], snapshot, env)
    if version_run.returncode != 0:
        raise RuntimeError("could not determine OpenCode version")
    version_text = version_run.stdout.decode("utf-8", errors="replace").strip()
    actual_version = _version_tuple(version_text)
    minimum_version = _minimum_version(spec.minimum_opencode)
    if minimum_version is not None and actual_version < minimum_version:
        raise RuntimeError(
            f"OpenCode {version_text} is below required {spec.minimum_opencode}"
        )
    config = _run_preflight_command(
        [opencode_bin, "debug", "config", "--pure"], snapshot, env
    )
    if config.returncode != 0:
        raise RuntimeError(
            "OpenCode rejected workflow config: "
            + _redact(config.stdout.decode("utf-8", errors="replace"), env)
        )
    agents_run = _run_preflight_command(
        [opencode_bin, "agent", "list", "--pure"], snapshot, env
    )
    if agents_run.returncode != 0:
        raise RuntimeError("OpenCode agent discovery failed")
    agents = _parse_agent_modes(agents_run.stdout.decode("utf-8", errors="replace"))
    if agents.get(spec.entry_agent) not in {"primary", "all"}:
        raise RuntimeError(
            f"entry agent {spec.entry_agent!r} was not discovered as a primary agent"
        )
    missing_agents = sorted(set(spec.allowed_agents) - set(agents))
    if missing_agents:
        raise RuntimeError(f"allowed agents were not discovered: {missing_agents}")
    invalid_subagents = sorted(
        name
        for name in spec.allowed_agents
        if name != spec.entry_agent and agents.get(name) not in {"subagent", "all"}
    )
    if invalid_subagents:
        raise RuntimeError(
            f"non-entry allowed agents must be subagent/all: {invalid_subagents}"
        )

    skills_run = _run_preflight_command(
        [opencode_bin, "debug", "skill", "--pure"], snapshot, env
    )
    if skills_run.returncode != 0:
        raise RuntimeError("OpenCode skill discovery failed")
    skills = _parse_skill_names(skills_run.stdout.decode("utf-8", errors="replace"))
    missing_skills = sorted(set(spec.required_skills) - set(skills))
    if missing_skills:
        raise RuntimeError(
            f"required workflow skills were not discovered: {missing_skills}"
        )

    mcp_run = _run_preflight_command(
        [opencode_bin, "mcp", "list", "--pure"], snapshot, env, timeout=120
    )
    if mcp_run.returncode != 0:
        raise RuntimeError(
            "OpenCode MCP preflight failed: "
            + _redact(mcp_run.stdout.decode("utf-8", errors="replace"), env)
        )
    connected_mcp = _connected_mcp_names(
        mcp_run.stdout.decode("utf-8", errors="replace")
    )
    required_mcp = set(spec.required_mcp) | {
        pattern[:-2]
        for pattern in spec.allowed_tool_patterns
        if pattern.endswith("_*") and len(pattern) > 2
    }
    missing_mcp = sorted(required_mcp - connected_mcp)
    if missing_mcp:
        raise RuntimeError(f"required MCP servers are not connected: {missing_mcp}")
    return {
        "opencode_version": version_text,
        "config": "passed",
        "entry_agent": {"name": spec.entry_agent, "mode": agents[spec.entry_agent]},
        "allowed_agents": {name: agents[name] for name in spec.allowed_agents},
        "skills": skills,
        "mcp": {"status": "passed", "connected": sorted(connected_mcp)},
    }


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_tcp(port: int, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"opencode serve exited with status {process.returncode}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"opencode serve did not listen within {timeout}s")


def start_opencode_server(
    binary: str,
    snapshot: Path,
    env: dict[str, str],
    log_path: Path,
    timeout: float,
) -> tuple[subprocess.Popen[bytes], Any, int]:
    for attempt in range(3):
        port = free_tcp_port()
        handle = _open_private_binary(log_path)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [
                    binary,
                    "serve",
                    "--pure",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--print-logs",
                    "--log-level",
                    "INFO",
                ],
                cwd=snapshot,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            wait_for_tcp(port, process, timeout)
        except BaseException as exc:
            terminate_process_tree(process)
            handle.close()
            try:
                output = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                output = ""
            address_in_use = (
                "EADDRINUSE" in output or "address already in use" in output.lower()
            )
            if isinstance(exc, RuntimeError) and address_in_use and attempt < 2:
                continue
            raise
        assert process is not None
        return process, handle, port
    raise RuntimeError("unable to reserve a port for opencode serve")


def _process_identity(pid: int) -> ProcessIdentity | None:
    try:
        process = psutil.Process(pid)
        return ProcessIdentity(pid=pid, create_time=process.create_time())
    except (psutil.Error, OSError):
        return None


def _remember_pid(tracked: set[ProcessIdentity], pid: int) -> None:
    identity = _process_identity(pid)
    if identity is not None:
        tracked.add(identity)


def _descendant_identities(pid: int) -> set[ProcessIdentity]:
    try:
        children = psutil.Process(pid).children(recursive=True)
    except (psutil.Error, OSError):
        return set()
    identities: set[ProcessIdentity] = set()
    for child in children:
        try:
            identities.add(
                ProcessIdentity(pid=child.pid, create_time=child.create_time())
            )
        except (psutil.Error, OSError):
            continue
    return identities


def _matching_process(identity: ProcessIdentity) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        if process.create_time() != identity.create_time:
            return None
        return process
    except (psutil.Error, OSError):
        return None


def terminate_process_tree(
    process: subprocess.Popen[bytes] | None, grace: float = 5
) -> set[ProcessIdentity]:
    if process is None:
        return set()
    tracked = _descendant_identities(process.pid)
    _remember_pid(tracked, process.pid)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
    for identity in sorted(tracked, reverse=True):
        child = _matching_process(identity)
        if child is None:
            continue
        try:
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                child.kill()
        except (psutil.Error, OSError):
            continue
    return tracked


def _live_identities(
    identities: Iterable[ProcessIdentity],
) -> list[ProcessIdentity]:
    live: list[ProcessIdentity] = []
    for identity in sorted(set(identities)):
        process = _matching_process(identity)
        if process is None:
            continue
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                live.append(identity)
        except (psutil.Error, OSError):
            continue
    return live


def _kill_tracked_identities(
    identities: Iterable[ProcessIdentity],
) -> list[int]:
    targets = _live_identities(identities)
    for identity in targets:
        process = _matching_process(identity)
        if process is None:
            continue
        try:
            process.kill()
        except (psutil.Error, OSError):
            continue
    if targets:
        time.sleep(0.1)
    return [identity.pid for identity in _live_identities(targets)]


def _render_input(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return json.dumps({"query": ""}, ensure_ascii=False, sort_keys=True)
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        decoded = {"query": value}
    return json.dumps(
        decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _decode_marker(response: str, marker: str) -> dict[str, Any] | None:
    if response.count(marker) != 1:
        return None
    lines = response.rstrip().splitlines()
    if not lines or not lines[-1].startswith(marker):
        return None
    remainder = lines[-1][len(marker) :].strip()
    try:
        value = json.loads(remainder)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def parse_session_log(
    run: SessionRun,
    response_dir: Path,
    spec: WorkflowSpec,
    result_schema: dict[str, Any],
) -> None:
    texts: list[str] = []
    tools: list[str] = []
    errors: list[str] = []
    session_id: str | None = None
    with run.log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("sessionID"), str):
                session_id = event["sessionID"]
            if event.get("type") == "text":
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
            elif event.get("type") == "tool_use":
                part = event.get("part")
                if isinstance(part, dict) and isinstance(part.get("tool"), str):
                    tools.append(part["tool"])
            elif event.get("type") == "error":
                errors.append(json.dumps(event.get("error"), ensure_ascii=False)[:2000])
    response = "\n".join(texts).strip()
    response_path = response_dir / f"session-{run.index}.md"
    _write_private_text(response_path, response + ("\n" if response else ""))
    result = _decode_marker(response, spec.result_marker)
    if result is None:
        errors.append(f"missing or invalid result marker: {spec.result_marker}")
    else:
        try:
            validate_json(result, result_schema)
        except ValidationError as exc:
            errors.append(f"result schema validation failed: {exc.message}")
        if result.get("status") != "completed":
            errors.append(
                f"workflow result status is not completed: {result.get('status')!r}"
            )
    unauthorized = sorted(
        {
            tool
            for tool in tools
            if not any(
                fnmatch.fnmatchcase(tool, pattern)
                for pattern in spec.allowed_tool_patterns
            )
        }
    )
    if unauthorized:
        errors.append(f"unauthorized tool calls: {unauthorized}")
    run.response_path = response_path
    run.response_sha256 = _sha256_bytes(response_path.read_bytes())
    run.session_id = session_id
    run.tool_calls = tuple(tools)
    run.http_sources = tuple(
        sorted(
            {
                match.group(0).rstrip(".,;:!?\"'")
                for match in _HTTP_URL_RE.finditer(response)
            }
        )
    )
    run.result = result
    run.errors = tuple(errors)


def execution_peak(runs: list[SessionRun]) -> int:
    points: list[tuple[float, int]] = []
    for run in runs:
        if run.finished_monotonic is None:
            continue
        points.append((run.started_monotonic, 1))
        points.append((run.finished_monotonic, -1))
    active = peak = 0
    for _, delta in sorted(points, key=lambda item: (item[0], -item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def _serialize_session(run: SessionRun) -> dict[str, Any]:
    return {
        "index": run.index,
        "pid": run.process.pid,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "elapsed_seconds": run.elapsed_seconds,
        "exit_code": run.exit_code,
        "session_id": run.session_id,
        "input_sha256": _sha256_bytes(run.input_payload.encode("utf-8")),
        "log_path": str(run.log_path),
        "response_path": str(run.response_path) if run.response_path else None,
        "response_sha256": run.response_sha256,
        "tool_calls": list(run.tool_calls),
        "http_sources": list(run.http_sources),
        "http_sources_count": len(run.http_sources),
        "result": run.result,
        "errors": list(run.errors),
    }


def _nested_tool_call_counts(
    layout: RunLayout,
) -> tuple[Path, dict[str, int], str, str | None]:
    """Count permission decisions across primary and nested OpenCode agents."""

    audit_log = layout.xdg_data / "opencode" / "log" / "opencode.log"
    counts: dict[str, int] = {}
    try:
        if not audit_log.is_file():
            return audit_log, counts, "unavailable", None
        with audit_log.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _TOOL_PERMISSION_RE.search(line)
                if match:
                    tool = match.group(1)
                    counts[tool] = counts.get(tool, 0) + 1
        return audit_log, counts, "complete", None
    except OSError as exc:
        return audit_log, counts, "error", f"{type(exc).__name__}: {exc}"


def _tool_budget_violations(
    counts: dict[str, int],
    budgets: dict[str, int],
    replicas: int,
) -> list[dict[str, int | str]]:
    """Return aggregate violations for per-root-session tool budgets."""

    violations: list[dict[str, int | str]] = []
    for pattern, configured_limit in budgets.items():
        effective_limit = configured_limit * replicas
        count = sum(
            value
            for tool, value in counts.items()
            if fnmatch.fnmatchcase(tool, pattern)
        )
        if count > effective_limit:
            violations.append(
                {
                    "pattern": pattern,
                    "count": count,
                    "configured_per_replica": configured_limit,
                    "effective_limit": effective_limit,
                }
            )
    return violations


def _opencode_tool_audit(
    layout: RunLayout,
    runs: Iterable[SessionRun],
    allowed_patterns: Iterable[str],
    tool_call_budgets: dict[str, int] | None = None,
    replicas: int = 1,
    monitored_counts: dict[str, int] | None = None,
    permission_log_coverage: str | None = None,
    permission_log_error: str | None = None,
) -> dict[str, Any]:
    """Collect root and nested-agent tool permissions from this isolated run."""

    root_calls = [tool for run in runs for tool in run.tool_calls]
    if monitored_counts is None:
        audit_log, counts, coverage, audit_error = _nested_tool_call_counts(layout)
    else:
        audit_log = layout.xdg_data / "opencode" / "log" / "opencode.log"
        counts = dict(monitored_counts)
        coverage = permission_log_coverage or "unavailable"
        audit_error = permission_log_error
    root_counts = Counter(root_calls)
    missing_root_calls = {
        tool: expected - counts.get(tool, 0)
        for tool, expected in root_counts.items()
        if counts.get(tool, 0) < expected
    }
    if coverage == "complete" and missing_root_calls:
        coverage = "incomplete"
        audit_error = "permission log omitted root-session tool calls"
    # The isolated permission log covers the full agent tree. Root JSONL is a
    # fallback only; combining the two would count primary calls twice.
    count_source = "opencode_permission_log"
    if coverage == "unavailable":
        count_source = "root_session_jsonl"
        for tool in root_calls:
            counts[tool] = counts.get(tool, 0) + 1
    elif coverage != "complete":
        count_source = "opencode_permission_log_partial"
    observed = set(root_calls) | set(counts)
    patterns = tuple(allowed_patterns)
    unauthorized = sorted(
        tool
        for tool in observed
        if not any(fnmatch.fnmatchcase(tool, pattern) for pattern in patterns)
    )
    budgets = dict(tool_call_budgets or {})
    detected_violations = _tool_budget_violations(counts, budgets, replicas)
    violations = detected_violations if coverage == "complete" else []
    untrusted_violations = detected_violations if coverage != "complete" else []
    matched_usage = {
        pattern: sum(
            count
            for tool, count in counts.items()
            if fnmatch.fnmatchcase(tool, pattern)
        )
        for pattern in budgets
    }
    missing_budget_audit = bool(budgets and coverage != "complete")
    return {
        "passed": not unauthorized and not violations and not missing_budget_audit,
        "observed": sorted(observed),
        "counts": dict(sorted(counts.items())),
        "count_source": count_source,
        "unauthorized": unauthorized,
        "budgets": {
            "configured_per_replica": budgets,
            "replicas": replicas,
            "effective": {
                pattern: limit * replicas for pattern, limit in budgets.items()
            },
            "matched_usage": matched_usage,
            "coverage": coverage,
            "audit_error": audit_error,
            "missing_root_calls": missing_root_calls,
            "enforcement_scope": "run_aggregate",
            "limit_semantics": "configured_per_replica_scaled_by_replicas",
        },
        "budget_violations": violations,
        "untrusted_budget_violations": untrusted_violations,
        "nested_audit_log": str(audit_log) if audit_log.is_file() else None,
    }


def _run_validators(
    spec: WorkflowSpec,
    layout: RunLayout,
    env: dict[str, str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    validator_env = {
        key: value
        for key, value in env.items()
        if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
        or key.startswith("RESEARCH_WORKFLOW_")
    }
    validator_env["RESEARCH_WORKFLOW_MANIFEST"] = str(layout.manifest_path)
    for validator in spec.validators:
        started = time.monotonic()
        command = [
            item.replace("{python}", sys.executable) for item in validator.command
        ]
        completed = _run_preflight_command(
            command,
            layout.snapshot_dir,
            validator_env,
            timeout=validator.timeout_seconds,
        )
        log_path = layout.log_dir / f"validator-{validator.name}.log"
        with _open_private_binary(log_path) as handle:
            handle.write(completed.stdout)
        results.append(
            {
                "name": validator.name,
                "exit_code": completed.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "log_path": str(log_path),
            }
        )
    return results


def run_workflow(
    workflow: str | Path,
    *,
    inputs: list[str] | None = None,
    replicas: int | None = None,
    run_id: str | None = None,
    project_root: str | Path | None = None,
    base_dir: str | Path | None = None,
    opencode_bin: str | None = None,
    timeout_seconds: float | None = None,
    serve_start_timeout: float = 30,
    dry_run: bool = False,
) -> dict[str, Any]:
    source_spec = load_workflow(workflow)
    project = (
        Path(project_root).expanduser().resolve()
        if project_root
        else Path.cwd().resolve()
    )
    selected_replicas = (
        replicas
        if replicas is not None
        else len(inputs)
        if inputs and len(inputs) > 1
        else source_spec.default_replicas
    )
    if not 1 <= selected_replicas <= source_spec.max_replicas:
        raise ValueError(
            f"replicas must be between 1 and workflow maxReplicas={source_spec.max_replicas}"
        )
    input_values = list(inputs or [""])
    if len(input_values) == 1:
        input_values *= selected_replicas
    elif len(input_values) != selected_replicas:
        raise ValueError("provide exactly one input or one input per replica")
    canonical_inputs = [_render_input(value) for value in input_values]
    input_schema = json.loads(source_spec.input_schema_path.read_text(encoding="utf-8"))
    for index, value in enumerate(canonical_inputs, 1):
        try:
            validate_json(json.loads(value), input_schema)
        except ValidationError as exc:
            raise ValueError(
                f"input {index} failed schema validation: {exc.message}"
            ) from exc
    binary = opencode_bin or shutil.which("opencode")
    if not binary:
        raise FileNotFoundError("opencode was not found on PATH")
    initial_env = _load_project_environment(project)
    missing_env = sorted(
        key for key in source_spec.required_env if not initial_env.get(key)
    )
    if missing_env and not dry_run:
        raise RuntimeError(f"missing required environment variables: {missing_env}")
    deadline_seconds = (
        timeout_seconds if timeout_seconds is not None else source_spec.timeout_seconds
    )
    if deadline_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    identifier = run_id or f"{utc_now():%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    base = Path(base_dir).expanduser().resolve() if base_dir else None
    layout = make_layout(project, source_spec.name, identifier, base)
    initialize_layout(layout, source_spec.root)
    spec = load_workflow(layout.snapshot_dir)
    event_log = EventLog(layout.event_path)
    env = workflow_environment(layout, project, spec)
    workflow_hash, workflow_files = hash_tree(layout.snapshot_dir)
    result_schema = json.loads(spec.result_schema_path.read_text(encoding="utf-8"))
    input_records: list[dict[str, str]] = []
    for index, value in enumerate(canonical_inputs, 1):
        input_path = layout.runtime_dir / "inputs" / f"session-{index}.json"
        _write_private_text(input_path, value + "\n")
        input_records.append(
            {
                "path": str(input_path),
                "sha256": _sha256_bytes(value.encode("utf-8")),
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "initializing",
        "run_id": identifier,
        "workflow": {
            "name": spec.name,
            "description": spec.description,
            "source_path": str(source_spec.root),
            "snapshot_path": str(layout.snapshot_dir),
            "sha256": workflow_hash,
            "files": workflow_files,
            "entry_command": spec.entry_command,
            "entry_agent": spec.entry_agent,
        },
        "created_at": iso_now(),
        "replicas": selected_replicas,
        "timeout_seconds": deadline_seconds,
        "required_env": list(spec.required_env),
        "required_skills": list(spec.required_skills),
        "required_mcp": list(spec.required_mcp),
        "missing_required_env": missing_env,
        "security": {
            "allowed_tool_patterns": list(spec.allowed_tool_patterns),
            "tool_call_budgets_per_replica": dict(spec.tool_call_budgets),
            "effective_tool_call_budgets": {
                pattern: limit * selected_replicas
                for pattern, limit in spec.tool_call_budgets.items()
            },
            "tool_call_budget_enforcement_scope": "run_aggregate",
            "tool_call_budget_limit_semantics": (
                "configured_per_replica_scaled_by_replicas"
            ),
            "allowed_agents": list(spec.allowed_agents),
            "agent_tool_patterns": {
                name: list(patterns)
                for name, patterns in spec.agent_tool_patterns.items()
            },
            "command_shell_allowed": spec.allow_command_shell,
        },
        "paths": {
            "manifest": str(layout.manifest_path),
            "runtime": str(layout.runtime_dir),
            "logs": str(layout.log_dir),
            "artifacts": str(layout.artifact_dir),
        },
        "inputs": input_records,
    }
    atomic_write_json(layout.manifest_path, manifest)
    event_log.emit(
        "workflow_initialized", workflow=spec.name, replicas=selected_replicas
    )
    try:
        preflight_result = preflight(spec, layout.snapshot_dir, env, binary)
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "finished_at": iso_now(),
                "failure": f"{type(exc).__name__}: {exc}",
                "sessions": [],
            }
        )
        atomic_write_json(layout.manifest_path, manifest)
        event_log.emit("preflight_failed", error=manifest["failure"])
        raise
    manifest["preflight"] = preflight_result
    if dry_run:
        integrity = snapshot_integrity(layout.snapshot_dir, workflow_hash)
        manifest.update(
            {
                "status": "dry_run" if integrity["passed"] else "failed",
                "finished_at": iso_now(),
                "sessions": [],
                "snapshot_integrity": integrity,
            }
        )
        atomic_write_json(layout.manifest_path, manifest)
        event_log.emit("dry_run_completed", snapshot_integrity=integrity["passed"])
        return manifest

    serve: subprocess.Popen[bytes] | None = None
    serve_handle: Any | None = None
    runs: list[SessionRun] = []
    tool_monitor = ToolPermissionLogMonitor(
        layout.xdg_data / "opencode" / "log" / "opencode.log"
    )
    tracked_processes: set[ProcessIdentity] = set()
    failure: str | None = None
    wall_started = time.monotonic()
    manifest["status"] = "running"
    atomic_write_json(layout.manifest_path, manifest)
    try:
        attach_url: str | None = None
        if selected_replicas > 1 and spec.persistent_server:
            serve, serve_handle, port = start_opencode_server(
                binary,
                layout.snapshot_dir,
                env,
                layout.log_dir / "opencode-serve.log",
                serve_start_timeout,
            )
            _remember_pid(tracked_processes, serve.pid)
            attach_url = f"http://127.0.0.1:{port}"
            event_log.emit("server_ready", pid=serve.pid, url=attach_url)
        for index, input_payload in enumerate(canonical_inputs, 1):
            command = [
                binary,
                "run",
                "--pure",
                "--command",
                spec.entry_command,
                "--agent",
                spec.entry_agent,
                "--format",
                "json",
                "--title",
                f"{spec.name} {identifier} #{index}",
                "--dir",
                str(layout.snapshot_dir),
            ]
            if attach_url:
                command.extend(["--attach", attach_url])
            command.append(input_payload)
            log_path = layout.log_dir / f"session-{index}.jsonl"
            log_handle = _open_private_binary(log_path)
            process = subprocess.Popen(
                command,
                cwd=layout.snapshot_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _remember_pid(tracked_processes, process.pid)
            run = SessionRun(
                index=index,
                input_payload=input_payload,
                command=command,
                process=process,
                log_path=log_path,
                log_handle=log_handle,
                started_at=iso_now(),
                started_monotonic=time.monotonic(),
            )
            runs.append(run)
            event_log.emit("session_started", index=index, pid=process.pid)
        deadline = wall_started + deadline_seconds
        next_tool_budget_check = 0.0
        while any(run.exit_code is None for run in runs):
            for run in runs:
                if run.exit_code is not None:
                    continue
                tracked_processes.update(_descendant_identities(run.process.pid))
                exit_code = run.process.poll()
                if exit_code is None:
                    continue
                run.exit_code = exit_code
                run.finished_at = iso_now()
                run.finished_monotonic = time.monotonic()
                run.log_handle.close()
                parse_session_log(
                    run,
                    layout.artifact_dir / "responses",
                    spec,
                    result_schema,
                )
                event_log.emit(
                    "session_finished",
                    index=run.index,
                    exit_code=run.exit_code,
                    errors=list(run.errors),
                )
            if serve is not None:
                tracked_processes.update(_descendant_identities(serve.pid))
                if serve.poll() is not None:
                    raise RuntimeError(
                        f"opencode serve exited with status {serve.returncode}"
                    )
            now = time.monotonic()
            if spec.tool_call_budgets and now >= next_tool_budget_check:
                live_tool_counts = tool_monitor.poll()
                if tool_monitor.coverage in {"incomplete", "error"}:
                    raise RuntimeError(
                        "tool call budget audit "
                        f"{tool_monitor.coverage}: {tool_monitor.error or 'unknown error'}"
                    )
                budget_violations = _tool_budget_violations(
                    live_tool_counts,
                    spec.tool_call_budgets,
                    selected_replicas,
                )
                if budget_violations:
                    event_log.emit(
                        "tool_call_budget_exceeded",
                        violations=budget_violations,
                    )
                    details = ", ".join(
                        f"{item['pattern']}={item['count']}/{item['effective_limit']}"
                        for item in budget_violations
                    )
                    raise RuntimeError(f"tool call budget exceeded: {details}")
                next_tool_budget_check = now + 0.25
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"workflow timeout reached after {deadline_seconds}s"
                )
            time.sleep(0.05)
    except (Exception, KeyboardInterrupt) as exc:
        failure = f"{type(exc).__name__}: {exc}"
        event_log.emit("workflow_failure", error=failure)
    finally:
        for run in runs:
            if run.process.poll() is None:
                tracked_processes.update(terminate_process_tree(run.process))
            if not run.log_handle.closed:
                run.exit_code = run.process.poll()
                run.finished_at = iso_now()
                run.finished_monotonic = time.monotonic()
                run.log_handle.close()
                parse_session_log(
                    run,
                    layout.artifact_dir / "responses",
                    spec,
                    result_schema,
                )
        tracked_processes.update(terminate_process_tree(serve))
        if serve_handle is not None:
            serve_handle.close()

    time.sleep(0.05)
    orphan_identities = _live_identities(tracked_processes)
    orphan_pids_detected = [identity.pid for identity in orphan_identities]
    orphan_pids = _kill_tracked_identities(orphan_identities)
    monitored_tool_counts = tool_monitor.poll(final=True)
    validator_results: list[dict[str, Any]] = []
    integrity = snapshot_integrity(layout.snapshot_dir, workflow_hash)
    try:
        tool_audit = _opencode_tool_audit(
            layout,
            runs,
            spec.allowed_tool_patterns,
            spec.tool_call_budgets,
            selected_replicas,
            monitored_tool_counts,
            tool_monitor.coverage,
            tool_monitor.error,
        )
    except Exception as exc:
        audit_error = f"{type(exc).__name__}: {exc}"
        tool_audit = {
            "passed": False,
            "observed": sorted({tool for run in runs for tool in run.tool_calls}),
            "counts": dict(sorted(monitored_tool_counts.items())),
            "count_source": "audit_error",
            "unauthorized": [],
            "budgets": {
                "configured_per_replica": dict(spec.tool_call_budgets),
                "replicas": selected_replicas,
                "effective": {
                    pattern: limit * selected_replicas
                    for pattern, limit in spec.tool_call_budgets.items()
                },
                "matched_usage": {},
                "coverage": "error",
                "audit_error": audit_error,
                "missing_root_calls": {},
                "enforcement_scope": "run_aggregate",
                "limit_semantics": ("configured_per_replica_scaled_by_replicas"),
            },
            "budget_violations": [],
            "untrusted_budget_violations": [],
            "nested_audit_log": None,
        }
    if (
        spec.tool_call_budgets
        and tool_audit["budgets"]["coverage"] != "complete"
        and failure is None
    ):
        failure = f"tool call budget audit {tool_audit['budgets']['coverage']}"
    if tool_audit["unauthorized"] and failure is None:
        failure = f"unauthorized nested agent tool calls: {tool_audit['unauthorized']}"
    if tool_audit["budget_violations"] and failure is None:
        failure = f"tool call budget exceeded: {tool_audit['budget_violations']}"
    base_passed = (
        failure is None
        and len(runs) == selected_replicas
        and all(run.exit_code == 0 and not run.errors for run in runs)
        and not orphan_pids_detected
        and integrity["passed"]
    )
    manifest.update(
        {
            "status": "validating"
            if base_passed and spec.validators
            else "completed"
            if base_passed
            else "failed",
            "finished_at": iso_now(),
            "wall_seconds": round(time.monotonic() - wall_started, 3),
            "failure": failure,
            "session_execution_peak": execution_peak(runs),
            "sessions": [_serialize_session(run) for run in runs],
            "orphan_pids": orphan_pids,
            "orphan_pids_detected": orphan_pids_detected,
            "tool_audit": tool_audit,
            "snapshot_integrity": integrity,
        }
    )
    atomic_write_json(layout.manifest_path, manifest)
    if base_passed and spec.validators:
        try:
            validator_results = _run_validators(spec, layout, env)
        except (subprocess.SubprocessError, OSError) as exc:
            validator_results = [
                {"name": "runner", "exit_code": None, "error": str(exc)}
            ]
        manifest["validators"] = validator_results
        integrity = snapshot_integrity(layout.snapshot_dir, workflow_hash)
        manifest["snapshot_integrity"] = integrity
        manifest["status"] = (
            "completed"
            if validator_results
            and all(item.get("exit_code") == 0 for item in validator_results)
            and integrity["passed"]
            else "failed"
        )
        manifest["finished_at"] = iso_now()
        atomic_write_json(layout.manifest_path, manifest)
    event_log.emit(
        "workflow_finished", status=manifest["status"], orphan_pids=orphan_pids
    )
    return manifest
