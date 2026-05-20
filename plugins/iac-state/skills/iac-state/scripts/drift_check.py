#!/usr/bin/env python3
"""drift_check.py — detect drift via `plan -detailed-exitcode` (scaffold).

v1.0 surface:
    drift_check.py <host> <project-dir> [--target ADDR] [--tool auto|tofu|terraform]
                                          [--json]

Workflow:
    1. terraform init -input=false -lock=false -upgrade=false
    2. terraform plan -detailed-exitcode -out=/tmp/plan.bin [-target=<addr>]
    3. terraform show -json /tmp/plan.bin
    4. Parse resource_changes[]; classify each:
         no-op | create | update-in-place | replace | destroy
    5. For each non-no-op:
         severity = priority_table(resource_type)
         hint = link into references/drift_patterns.md
    6. Summary: count by action + crit/warn/info histogram
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "iac-state: 'drift_check' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="drift_check.py")
    p.add_argument("host")
    p.add_argument("project_dir")
    p.add_argument("--target", default=None,
                   help="Limit to one resource address, e.g. aws_instance.web")
    p.add_argument("--tool", choices=["auto", "tofu", "terraform"], default="auto")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"host": args.host, "project_dir": args.project_dir,
                 "target": args.target, "tool": args.tool},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
