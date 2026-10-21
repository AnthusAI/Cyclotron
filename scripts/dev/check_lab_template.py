"""Prove infra/lab/template.yaml == deployed snapshot + the reviewed pending changes.

Offline by default: the template must equal infra/lab/deployed-template.snapshot.json
with infra/lab/pending-changes.json applied. The pending set is a list of
add-only operations (a JSON Pointer path and the value added) that a reviewer reads
beside infra/lab/pending-changes.md, so the next deploy changes exactly those
paths and nothing else. An empty set means template == snapshot.

With --live the snapshot is also compared with the deployed stack via read-only
`aws cloudformation get-template` (Original stage); it never calls any write API.
Formatting and key order are ignored.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "infra/lab/template.yaml"
SNAPSHOT = ROOT / "infra/lab/deployed-template.snapshot.json"
PENDING = ROOT / "infra/lab/pending-changes.json"


def canonical(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def diff_paths(left: object, right: object, path: str = "$") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        out = []
        for key in sorted(set(left) | set(right)):
            if key not in left:
                out.append(f"{path}.{key}: only in snapshot")
            elif key not in right:
                out.append(f"{path}.{key}: only in template")
            else:
                out += diff_paths(left[key], right[key], f"{path}.{key}")
        return out
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [d for i, (a, b) in enumerate(zip(left, right)) for d in diff_paths(a, b, f"{path}[{i}]")]
    return [] if left == right else [f"{path}: template={left!r} snapshot={right!r}"]


def load_template() -> object:
    return yaml.safe_load(TEMPLATE.read_text())


def load_snapshot() -> object:
    return json.loads(SNAPSHOT.read_text())


def load_pending() -> list[dict]:
    return json.loads(PENDING.read_text())["operations"]


def _pointer(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError(f"pending path must be a JSON Pointer: {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def apply_pending(snapshot: object, operations: list[dict]) -> object:
    """Apply add-only operations to a copy of snapshot; replace, remove and overwrite are refused."""
    document = json.loads(json.dumps(snapshot))
    for operation in operations:
        if set(operation) != {"op", "path", "value"} or operation["op"] != "add":
            raise ValueError(f"pending operations must be add-only with op, path and value: {operation!r}")
        *parents, leaf = _pointer(operation["path"])
        target = document
        for part in parents:
            target = target[int(part)] if isinstance(target, list) else target[part]
        if isinstance(target, list):
            if leaf != "-":
                raise ValueError(f"lists are appended with '-': {operation['path']}")
            target.append(operation["value"])
        elif leaf in target:
            raise ValueError(f"pending add would overwrite an existing value: {operation['path']}")
        else:
            target[leaf] = operation["value"]
    return document


def expected_template(snapshot: object, operations: list[dict]) -> object:
    return apply_pending(snapshot, operations)


def fetch_live(stack: str, region: str) -> object:
    result = subprocess.run(
        [
            "aws",
            "cloudformation",
            "get-template",
            "--stack-name",
            stack,
            "--template-stage",
            "Original",
            "--region",
            region,
            "--output",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    body = json.loads(result.stdout)["TemplateBody"]
    # A template deployed as YAML comes back as a string; one deployed as JSON comes back parsed.
    return yaml.safe_load(body) if isinstance(body, str) else body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also compare the snapshot with the deployed stack (read-only)")
    parser.add_argument("--stack", default="cyclotron-console")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args(argv)

    template, snapshot, pending = load_template(), load_snapshot(), load_pending()
    problems = diff_paths(template, expected_template(snapshot, pending))
    print(f"template.yaml vs snapshot + {len(pending)} pending change(s): {'MATCH' if not problems else 'DIFFERENT'}")
    for operation in pending:
        print(f"  pending add {operation['path']}")
    if args.live:
        live = fetch_live(args.stack, args.region)
        live_problems = diff_paths(snapshot, live)
        print(f"snapshot vs deployed {args.stack} (Original): {'MATCH' if not live_problems else 'DIFFERENT'}")
        problems += [f"live {p}" for p in live_problems]
    for problem in problems:
        print("  " + problem)
    if not problems:
        resources = len(template["Resources"])
        print(f"{resources} resources, {len(template['Parameters'])} parameters, canonical equality")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
