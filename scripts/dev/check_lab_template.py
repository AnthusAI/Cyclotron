"""Compare infra/lab/template.yaml with the committed deployed-template snapshot.

Offline by default. With --live it also compares the snapshot with the deployed
stack via read-only `aws cloudformation get-template` (Original stage); it never
calls any write API. Formatting and key order are ignored.
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
    return json.loads(result.stdout)["TemplateBody"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also compare against the deployed stack (read-only)")
    parser.add_argument("--stack", default="cyclotron-console")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    template, snapshot = load_template(), load_snapshot()
    problems = diff_paths(template, snapshot)
    print(f"template.yaml vs snapshot: {'MATCH' if not problems else 'DIFFERENT'}")
    if args.live:
        live = fetch_live(args.stack, args.region)
        live_problems = diff_paths(template, live)
        print(f"template.yaml vs deployed {args.stack} (Original): {'MATCH' if not live_problems else 'DIFFERENT'}")
        problems += [f"live {p}" for p in live_problems]
    for problem in problems:
        print("  " + problem)
    if not problems:
        resources = len(template["Resources"])
        print(f"{resources} resources, {len(template['Parameters'])} parameters, canonical equality")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
