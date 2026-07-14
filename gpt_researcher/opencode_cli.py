"""Small scaffold and launcher for native OpenCode research projects."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_TEMPLATE = PROJECT_ROOT / "opencode" / "_template"
DEFAULT_TEMPLATE = "_template"
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class WorkflowError(RuntimeError):
    """Raised for invalid workflow input or structure."""


def _strip_jsonc(text: str) -> str:
    """Remove JSONC comments and trailing commas without touching strings."""

    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                index += 1
            index += 2
            continue
        output.append(char)
        index += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(output))


def load_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise WorkflowError(f"missing {path.name}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"invalid JSONC in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowError(f"configuration must be an object: {path}")
    return payload


def workflows_root(value: str | Path | None = None) -> Path:
    configured = value or os.getenv("OPENCODE_WORKFLOWS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    local = (Path.cwd() / "opencode").resolve()
    if local.is_dir():
        return local
    bundled = (PROJECT_ROOT / "opencode").resolve()
    if bundled.is_dir():
        return bundled
    return local


def workflow_path(root: Path, name: str) -> Path:
    if not _SAFE_NAME.fullmatch(name):
        raise WorkflowError(
            "workflow name must use lowercase letters, digits, '.', '_' or '-'"
        )
    path = (root / name).resolve()
    if path.parent != root.resolve():
        raise WorkflowError("workflow path escapes the workflows directory")
    return path


def validate_workflow(path: Path) -> dict[str, Any]:
    if not path.is_dir():
        raise WorkflowError(f"workflow does not exist: {path}")
    agents = path / "AGENTS.md"
    if not agents.is_file():
        raise WorkflowError(f"missing AGENTS.md: {path}")
    if not agents.read_text(encoding="utf-8").strip():
        raise WorkflowError(f"AGENTS.md is empty: {path}")
    return load_config(path / "opencode.jsonc")


def _template_path(root: Path, template: str) -> Path:
    candidate = Path(template).expanduser()
    if candidate.is_absolute() or len(candidate.parts) > 1:
        return candidate.resolve()
    path = (root / template).resolve()
    if template == DEFAULT_TEMPLATE and not path.exists():
        return BUNDLED_TEMPLATE.resolve()
    if path.parent != root.resolve():
        raise WorkflowError("template path escapes the workflows directory")
    return path


def _replace_template_tokens(path: Path, name: str) -> None:
    replacements = {
        "{{WORKFLOW_NAME}}": name,
        "{{WORKFLOW_TITLE}}": name.replace("-", " ").replace("_", " ").title(),
    }
    for file_path in path.rglob("*"):
        if not file_path.is_file() or file_path.name.startswith("."):
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = content
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if updated != content:
            file_path.write_text(updated, encoding="utf-8")


def create_workflow(root: Path, name: str, template: str) -> Path:
    source = _template_path(root, template)
    validate_workflow(source)
    destination = workflow_path(root, name)
    if destination.exists():
        raise WorkflowError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(
            "node_modules", "package-lock.json", "bun.lock", ".DS_Store"
        ),
    )
    _replace_template_tokens(destination, name)
    return destination


def discover_workflows(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and not path.name.startswith("_")
        and (path / "AGENTS.md").is_file()
        and (path / "opencode.jsonc").is_file()
    )


def _names(directory: Path, suffix: str = "") -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(
        path.name.removesuffix(suffix)
        for path in directory.iterdir()
        if path.is_file() and (not suffix or path.name.endswith(suffix))
    )


def workflow_summary(path: Path) -> str:
    config = validate_workflow(path)
    prompt = (path / "AGENTS.md").read_text(encoding="utf-8")
    title = next(
        (
            line.removeprefix("# ").strip()
            for line in prompt.splitlines()
            if line.startswith("# ")
        ),
        path.name,
    )
    mcp = config.get("mcp") if isinstance(config.get("mcp"), dict) else {}
    harness = path / ".opencode"
    agents = _names(harness / "agents", ".md")
    skills = (
        sorted(skill.parent.name for skill in (harness / "skills").glob("*/SKILL.md"))
        if (harness / "skills").is_dir()
        else []
    )
    commands = _names(harness / "commands", ".md")

    lines = [
        f"Workflow: {title}",
        f"Path: {path}",
        f"Model: {config.get('model', '(OpenCode default)')}",
        "",
        "OpenCode",
        "├── Prompt: AGENTS.md",
    ]
    if mcp:
        lines.append("├── MCP tools")
        entries = list(mcp.items())
        for index, (name, settings) in enumerate(entries):
            branch = "│   └──" if index == len(entries) - 1 else "│   ├──"
            enabled = (
                settings.get("enabled", True) if isinstance(settings, dict) else True
            )
            lines.append(f"{branch} {name} ({'enabled' if enabled else 'disabled'})")
    else:
        lines.append("├── MCP tools: none configured")
    lines.append("└── Optional harness (.opencode)")
    lines.append(f"    ├── agents: {', '.join(agents) if agents else 'none'}")
    lines.append(f"    ├── skills: {', '.join(skills) if skills else 'none'}")
    lines.append(f"    └── commands: {', '.join(commands) if commands else 'none'}")
    return "\n".join(lines)


def open_workflow(path: Path, *, web: bool, pure: bool) -> None:
    validate_workflow(path)
    if shutil.which("opencode") is None:
        raise WorkflowError("opencode executable was not found on PATH")
    os.chdir(path)
    command = ["opencode", "web"] if web else ["opencode", "."]
    if pure:
        command.append("--pure")
    os.execvp(command[0], command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencode-workflow",
        description="Scaffold, inspect, and open native OpenCode research workflows.",
    )
    parser.add_argument(
        "--root",
        help="Workflow directory (default: OPENCODE_WORKFLOWS_DIR or ./opencode).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new = subparsers.add_parser(
        "new", help="Create a workflow from a directory template."
    )
    new.add_argument("name")
    new.add_argument("--template", default=DEFAULT_TEMPLATE)

    subparsers.add_parser("list", help="List generated workflows.")

    show = subparsers.add_parser("show", help="Visualize one workflow in the terminal.")
    show.add_argument("name")

    open_parser = subparsers.add_parser("open", help="Open a workflow in OpenCode.")
    open_parser.add_argument("name")
    open_parser.add_argument(
        "--web", action="store_true", help="Open the OpenCode web UI."
    )
    open_parser.add_argument(
        "--with-plugins",
        action="store_true",
        help="Allow external OpenCode plugins (default uses --pure).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = workflows_root(args.root)
    try:
        if args.command == "new":
            path = create_workflow(root, args.name, args.template)
            print(f"Created {path}")
            print(f"Edit {path / 'AGENTS.md'} and {path / 'opencode.jsonc'}")
            print(f"Open with: opencode-workflow --root {root} open {args.name}")
        elif args.command == "list":
            for path in discover_workflows(root):
                print(path.name)
        elif args.command == "show":
            print(workflow_summary(workflow_path(root, args.name)))
        elif args.command == "open":
            open_workflow(
                workflow_path(root, args.name),
                web=args.web,
                pure=not args.with_plugins,
            )
        return 0
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
