from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .config import load_workflow, validate_workflow_name
from .runner import run_workflow


def project_root() -> Path:
    return Path.cwd().resolve()


def bundled_workflows_root() -> Path:
    return Path(__file__).resolve().parents[2] / "research_workflows"


def _copy_template(source: Path, destination: Path, name: str) -> None:
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    if not source.is_dir():
        raise FileNotFoundError(f"workflow template does not exist: {source}")
    shutil.copytree(source, destination)
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        path.write_text(text.replace("__WORKFLOW_NAME__", name), encoding="utf-8")
    manifest_path = destination / "workflow.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = name
        local_schema = destination / "schemas" / "workflow.schema.json"
        local_schema.parent.mkdir(parents=True, exist_ok=True)
        schema_source = source.parent / "workflow.schema.json"
        if not schema_source.is_file():
            schema_source = bundled_workflows_root() / "workflow.schema.json"
        shutil.copy2(schema_source, local_schema)
        manifest["$schema"] = "schemas/workflow.schema.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def init_workflow(args: argparse.Namespace) -> int:
    validate_workflow_name(args.name)
    root = Path(args.project_root).expanduser().resolve()
    if args.template:
        template = Path(args.template).expanduser().resolve()
    else:
        local_template = root / "research_workflows" / "_template"
        template = (
            local_template
            if local_template.is_dir()
            else bundled_workflows_root() / "_template"
        )
    destination = (
        Path(args.destination).expanduser().resolve()
        if args.destination
        else root / "research_workflows" / args.name
    )
    _copy_template(template, destination, args.name)
    spec = load_workflow(destination)
    print(
        json.dumps(
            {
                "status": "created",
                "name": spec.name,
                "path": str(destination),
                "next": [
                    f"edit {destination / 'AGENTS.md'}",
                    f"edit {destination / 'opencode.jsonc'}",
                    f"gptr-workflow validate {destination}",
                    f"gptr-workflow run {destination} --input 'your research question'",
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


def validate_workflow(args: argparse.Namespace) -> int:
    manifest = run_workflow(
        args.workflow,
        inputs=[args.input or '{"query":"workflow preflight"}'],
        replicas=1,
        run_id=args.run_id,
        project_root=args.project_root,
        base_dir=args.base_dir,
        opencode_bin=args.opencode_bin,
        dry_run=True,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "workflow": manifest["workflow"]["name"],
                "manifest_path": manifest["paths"]["manifest"],
                "preflight": manifest["preflight"],
                "missing_required_env": manifest["missing_required_env"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if manifest["status"] == "dry_run" else 1


def _collect_inputs(args: argparse.Namespace) -> list[str]:
    values = list(args.input or [])
    for filename in args.input_file or []:
        values.append(Path(filename).expanduser().read_text(encoding="utf-8"))
    if args.input_json:
        values.append(Path(args.input_json).expanduser().read_text(encoding="utf-8"))
    return values


def execute_workflow(args: argparse.Namespace) -> int:
    inputs = _collect_inputs(args)
    if not inputs:
        if args.dry_run:
            inputs = ['{"query":"workflow preflight"}']
        else:
            raise ValueError("run requires --input, --input-file, or --input-json")
    manifest = run_workflow(
        args.workflow,
        inputs=inputs,
        replicas=args.replicas,
        run_id=args.run_id,
        project_root=args.project_root,
        base_dir=args.base_dir,
        opencode_bin=args.opencode_bin,
        timeout_seconds=args.timeout,
        serve_start_timeout=args.serve_start_timeout,
        dry_run=args.dry_run,
    )
    result = {
        "status": manifest["status"],
        "workflow": manifest["workflow"]["name"],
        "run_id": manifest["run_id"],
        "replicas": manifest["replicas"],
        "manifest_path": manifest["paths"]["manifest"],
        "responses": [
            session.get("response_path") for session in manifest.get("sessions", [])
        ],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if manifest["status"] in {"completed", "dry_run"} else 1


def list_workflows(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    workflows = []
    if root.is_dir():
        for manifest in sorted(root.glob("*/workflow.json")):
            if manifest.parent.name.startswith("_"):
                continue
            try:
                spec = load_workflow(manifest.parent)
            except (OSError, ValueError) as exc:
                workflows.append(
                    {"path": str(manifest.parent), "status": "invalid", "error": str(exc)}
                )
            else:
                workflows.append(
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "path": str(spec.root),
                        "default_replicas": spec.default_replicas,
                        "max_replicas": spec.max_replicas,
                    }
                )
    print(json.dumps({"workflows": workflows}, ensure_ascii=False, indent=2))
    return 0


def _add_run_arguments(parser: argparse.ArgumentParser, *, default_replicas: int | None) -> None:
    parser.add_argument("workflow")
    parser.add_argument("--input", action="append", help="text or JSON; repeat for distinct replicas")
    parser.add_argument("--input-file", action="append", help="read one input per file")
    parser.add_argument("--input-json", help="read a canonical JSON input file")
    parser.add_argument("--replicas", type=int, default=default_replicas)
    parser.add_argument("--run-id")
    parser.add_argument("--base-dir")
    parser.add_argument("--opencode-bin")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--serve-start-timeout", type=float, default=30)
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    root = project_root()
    parser = argparse.ArgumentParser(
        prog="gptr-workflow",
        description="Create and run isolated OpenCode-native research workflows.",
    )
    parser.add_argument("--project-root", default=str(root))
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    create = subparsers.add_parser("init", aliases=["create"], help="scaffold a workflow")
    create.add_argument("name")
    create.add_argument("--destination")
    create.add_argument("--template", help="copy another workflow as the starting point")
    create.set_defaults(handler=init_workflow)

    validate = subparsers.add_parser("validate", aliases=["check"], help="validate without model calls")
    validate.add_argument("workflow")
    validate.add_argument("--input")
    validate.add_argument("--run-id")
    validate.add_argument("--base-dir")
    validate.add_argument("--opencode-bin")
    validate.set_defaults(handler=validate_workflow)

    run = subparsers.add_parser("run", help="run one or more workflow replicas")
    _add_run_arguments(run, default_replicas=None)
    run.set_defaults(handler=execute_workflow)

    load_test = subparsers.add_parser(
        "load-test", help="run concurrent replicas through one persistent OpenCode server"
    )
    _add_run_arguments(load_test, default_replicas=3)
    load_test.set_defaults(handler=execute_workflow)

    listing = subparsers.add_parser("list", help="list valid workflow directories")
    listing.add_argument("--root", default=str(root / "research_workflows"))
    listing.set_defaults(handler=list_workflows)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
