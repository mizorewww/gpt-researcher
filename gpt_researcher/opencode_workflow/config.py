from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError as JsonSchemaValidationError


_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV = {"PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV", "LD_PRELOAD"}
_RESERVED_ENV_PREFIXES = ("OPENCODE_", "DYLD_", "LD_AUDIT")
_ALLOWED_TOP_LEVEL = {
    "$schema",
    "schemaVersion",
    "name",
    "description",
    "entryCommand",
    "entryAgent",
    "requires",
    "execution",
    "security",
    "inputSchema",
    "result",
    "validators",
}


def validate_workflow_name(value: str) -> str:
    name = _require_string(value, "name")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("name must be lowercase kebab-case and at most 64 characters")
    return name


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _safe_relative_path(value: Any, label: str) -> str:
    raw = _require_string(value, label)
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must stay inside the workflow directory")
    return path.as_posix()


def _string_list(value: Any, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be an array of non-empty strings")
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty")
    return tuple(value)


@dataclass(frozen=True)
class ValidatorSpec:
    name: str
    command: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True)
class WorkflowSpec:
    root: Path
    name: str
    description: str
    entry_command: str
    entry_agent: str
    required_env: tuple[str, ...]
    required_skills: tuple[str, ...]
    required_mcp: tuple[str, ...]
    minimum_opencode: str | None
    default_replicas: int
    max_replicas: int
    timeout_seconds: float
    persistent_server: bool
    allowed_tool_patterns: tuple[str, ...]
    tool_call_budgets: dict[str, int]
    allowed_agents: tuple[str, ...]
    agent_tool_patterns: dict[str, tuple[str, ...]]
    allow_command_shell: bool
    input_schema: str
    result_marker: str
    result_schema: str
    validators: tuple[ValidatorSpec, ...]

    @property
    def command_path(self) -> Path:
        singular = self.root / ".opencode" / "command" / f"{self.entry_command}.md"
        plural = self.root / ".opencode" / "commands" / f"{self.entry_command}.md"
        return singular if singular.is_file() else plural

    @property
    def result_schema_path(self) -> Path:
        return self.root / self.result_schema

    @property
    def input_schema_path(self) -> Path:
        return self.root / self.input_schema


def _parse_validator(value: Any, index: int) -> ValidatorSpec:
    data = _require_mapping(value, f"validators[{index}]")
    unknown = set(data) - {"name", "command", "timeoutSeconds"}
    if unknown:
        raise ValueError(f"validators[{index}] has unknown keys: {sorted(unknown)}")
    command = _string_list(data.get("command"), f"validators[{index}].command", nonempty=True)
    timeout = data.get("timeoutSeconds", 60)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError(f"validators[{index}].timeoutSeconds must be positive")
    return ValidatorSpec(
        name=validate_workflow_name(data.get("name")),
        command=command,
        timeout_seconds=float(timeout),
    )


def _validate_tree(root: Path) -> None:
    if not root.is_dir():
        raise FileNotFoundError(f"workflow directory does not exist: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"workflow symlinks are not allowed: {path.relative_to(root)}")


def load_workflow(path: str | Path) -> WorkflowSpec:
    root = Path(path).expanduser().resolve()
    _validate_tree(root)
    manifest_path = root / "workflow.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing workflow.json: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid workflow.json: {exc}") from exc
    data = _require_mapping(data, "workflow.json")
    schema_path = Path(__file__).resolve().parents[2] / "research_workflows" / "workflow.schema.json"
    if not schema_path.is_file():
        raise FileNotFoundError(f"bundled workflow schema is missing: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        Draft202012Validator(schema).validate(data)
    except JsonSchemaValidationError as exc:
        raise ValueError(f"workflow.json schema validation failed: {exc.message}") from exc
    unknown = set(data) - _ALLOWED_TOP_LEVEL
    if unknown:
        raise ValueError(f"workflow.json has unknown keys: {sorted(unknown)}")
    if data.get("schemaVersion") != 1:
        raise ValueError("workflow.json schemaVersion must be 1")

    name = validate_workflow_name(data.get("name"))
    entry_command = _require_string(data.get("entryCommand"), "entryCommand")
    entry_agent = _require_string(data.get("entryAgent"), "entryAgent")
    if not _NAME_RE.fullmatch(entry_command):
        raise ValueError("entryCommand must be lowercase kebab-case")
    if not _NAME_RE.fullmatch(entry_agent):
        raise ValueError("entryAgent must be lowercase kebab-case")

    requires = _require_mapping(data.get("requires", {}), "requires")
    unknown_requires = set(requires) - {"opencode", "env", "skills", "mcp"}
    if unknown_requires:
        raise ValueError(f"requires has unknown keys: {sorted(unknown_requires)}")
    required_env = _string_list(requires.get("env", []), "requires.env")
    for env_name in required_env:
        if not _ENV_NAME_RE.fullmatch(env_name):
            raise ValueError(f"invalid required environment variable name: {env_name}")
        if env_name in _RESERVED_ENV or env_name.startswith(_RESERVED_ENV_PREFIXES):
            raise ValueError(
                f"requires.env cannot inherit reserved control variable: {env_name}"
            )
    required_skills = _string_list(requires.get("skills", []), "requires.skills")
    required_mcp = _string_list(requires.get("mcp", []), "requires.mcp")
    minimum_opencode = requires.get("opencode")
    if minimum_opencode is not None:
        minimum_opencode = _require_string(minimum_opencode, "requires.opencode")

    execution = _require_mapping(data.get("execution", {}), "execution")
    unknown_execution = set(execution) - {
        "defaultReplicas",
        "maxReplicas",
        "timeoutSeconds",
        "persistentServer",
    }
    if unknown_execution:
        raise ValueError(f"execution has unknown keys: {sorted(unknown_execution)}")
    default_replicas = execution.get("defaultReplicas", 1)
    max_replicas = execution.get("maxReplicas", 3)
    timeout_seconds = execution.get("timeoutSeconds", 3000)
    if not isinstance(default_replicas, int) or isinstance(default_replicas, bool):
        raise ValueError("execution.defaultReplicas must be an integer")
    if not isinstance(max_replicas, int) or isinstance(max_replicas, bool):
        raise ValueError("execution.maxReplicas must be an integer")
    if not 1 <= default_replicas <= max_replicas <= 32:
        raise ValueError("replicas must satisfy 1 <= defaultReplicas <= maxReplicas <= 32")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("execution.timeoutSeconds must be positive")
    persistent_server = execution.get("persistentServer", True)
    if not isinstance(persistent_server, bool):
        raise ValueError("execution.persistentServer must be a boolean")

    security = _require_mapping(data.get("security"), "security")
    unknown_security = set(security) - {
        "allowedToolPatterns",
        "toolCallBudgets",
        "allowedAgents",
        "agentToolPatterns",
        "allowCommandShell",
    }
    if unknown_security:
        raise ValueError(f"security has unknown keys: {sorted(unknown_security)}")
    allowed_tools = _string_list(
        security.get("allowedToolPatterns"),
        "security.allowedToolPatterns",
        nonempty=True,
    )
    tool_call_budget_value = _require_mapping(
        security.get("toolCallBudgets", {}), "security.toolCallBudgets"
    )
    tool_call_budgets: dict[str, int] = {}
    for pattern, limit in tool_call_budget_value.items():
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(
                "security.toolCallBudgets keys must be non-empty strings"
            )
        if pattern not in allowed_tools:
            raise ValueError(
                "security.toolCallBudgets keys must also appear in "
                f"security.allowedToolPatterns: {pattern}"
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError(
                f"security.toolCallBudgets.{pattern} must be a positive integer"
            )
        tool_call_budgets[pattern] = limit
    allowed_agents = _string_list(
        security.get("allowedAgents"), "security.allowedAgents", nonempty=True
    )
    if entry_agent not in allowed_agents:
        raise ValueError("entryAgent must be included in security.allowedAgents")
    agent_tool_value = _require_mapping(
        security.get("agentToolPatterns"), "security.agentToolPatterns"
    )
    if set(agent_tool_value) != set(allowed_agents):
        raise ValueError(
            "security.agentToolPatterns keys must exactly match security.allowedAgents"
        )
    agent_tool_patterns = {
        agent: _string_list(
            agent_tool_value[agent],
            f"security.agentToolPatterns.{agent}",
        )
        for agent in allowed_agents
    }
    for agent, patterns in agent_tool_patterns.items():
        outside = set(patterns) - set(allowed_tools)
        if outside:
            raise ValueError(
                f"security.agentToolPatterns.{agent} exceeds allowedToolPatterns: {sorted(outside)}"
            )
    allow_command_shell = security.get("allowCommandShell", False)
    if not isinstance(allow_command_shell, bool):
        raise ValueError("security.allowCommandShell must be a boolean")

    input_schema = _safe_relative_path(data.get("inputSchema"), "inputSchema")
    result = _require_mapping(data.get("result"), "result")
    unknown_result = set(result) - {"marker", "schema"}
    if unknown_result:
        raise ValueError(f"result has unknown keys: {sorted(unknown_result)}")
    result_marker = _require_string(result.get("marker"), "result.marker")
    result_schema = _safe_relative_path(result.get("schema"), "result.schema")

    validators_value = data.get("validators", [])
    if not isinstance(validators_value, list):
        raise ValueError("validators must be an array")
    validators = tuple(
        _parse_validator(value, index) for index, value in enumerate(validators_value)
    )
    validator_names = [validator.name for validator in validators]
    if len(validator_names) != len(set(validator_names)):
        raise ValueError("validator names must be unique")

    required_files = [root / "AGENTS.md", root / "workflow.json"]
    if not any((root / filename).is_file() for filename in ("opencode.json", "opencode.jsonc")):
        raise FileNotFoundError("workflow must contain opencode.json or opencode.jsonc")
    for required in required_files:
        if not required.is_file():
            raise FileNotFoundError(f"missing required workflow file: {required}")

    spec = WorkflowSpec(
        root=root,
        name=name,
        description=str(data.get("description", "")),
        entry_command=entry_command,
        entry_agent=entry_agent,
        required_env=required_env,
        required_skills=required_skills,
        required_mcp=required_mcp,
        minimum_opencode=minimum_opencode,
        default_replicas=default_replicas,
        max_replicas=max_replicas,
        timeout_seconds=float(timeout_seconds),
        persistent_server=persistent_server,
        allowed_tool_patterns=allowed_tools,
        tool_call_budgets=tool_call_budgets,
        allowed_agents=allowed_agents,
        agent_tool_patterns=agent_tool_patterns,
        allow_command_shell=allow_command_shell,
        input_schema=input_schema,
        result_marker=result_marker,
        result_schema=result_schema,
        validators=validators,
    )
    if not spec.command_path.is_file():
        raise FileNotFoundError(
            f"entry command not found under .opencode/command(s): {entry_command}.md"
        )
    if not spec.result_schema_path.is_file():
        raise FileNotFoundError(f"result schema does not exist: {spec.result_schema_path}")
    if not spec.input_schema_path.is_file():
        raise FileNotFoundError(f"input schema does not exist: {spec.input_schema_path}")
    command_text = spec.command_path.read_text(encoding="utf-8")
    frontmatter_match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", command_text, re.DOTALL)
    if frontmatter_match:
        class UniqueKeyLoader(yaml.SafeLoader):
            pass

        def construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise ValueError(f"duplicate command frontmatter key: {key}")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        UniqueKeyLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
        )
        try:
            frontmatter = yaml.load(
                frontmatter_match.group(1), Loader=UniqueKeyLoader
            )
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid entry command frontmatter: {exc}") from exc
        if not isinstance(frontmatter, dict):
            raise ValueError("entry command frontmatter must be an object")
        command_agent = frontmatter.get("agent")
        if command_agent is not None and command_agent != entry_agent:
            raise ValueError(
                "entry command agent must match workflow.json entryAgent"
            )
        if frontmatter.get("subtask") is True:
            raise ValueError("entry command cannot be configured as a subtask")
    if not spec.allow_command_shell and re.search(r"!`[^`]+`", command_text):
        raise ValueError("entry command contains shell expansion but allowCommandShell is false")
    return spec
