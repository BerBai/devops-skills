#!/usr/bin/env python3
"""state_inspect.py — Terraform/OpenTofu state overview (scaffold).

v1.0 surface:
    state_inspect.py <host> <project-dir> [--tool tofu|terraform] [--json]

Collected:
    backend           type + key + lock_method (best-effort, from backend block)
    state_size_bytes  file size (local) or remote object size
    resource_count    `terraform state list | wc -l`
    is_locked         try a non-mutating operation; parse lock error if any
    lock_info         who/when/operation (when locked)
    workspaces        `terraform workspace list`
    summary           ok | warn | crit
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "iac-state: 'state_inspect' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="state_inspect.py")
    p.add_argument("host", help="ssh-core alias, or 'local'")
    p.add_argument("project_dir", help="Terraform project root containing .terraform/")
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
        "data": {"host": args.host, "project_dir": args.project_dir, "tool": args.tool},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
