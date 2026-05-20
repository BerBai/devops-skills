#!/usr/bin/env python3
"""module_validate.py — Terraform module audit (scaffold).

v1.0 surface:
    module_validate.py <host> <module-path>
        [--target-version X.Y.Z]
        [--tool auto|tofu|terraform]
        [--json]

Checks (see references/module_audit.md for the full table):
    structure         main.tf/variables.tf/outputs.tf/README/versions.tf present
    inputs            type/description/default/required, validation reasonable
    outputs           description present, sensitive flagged
    naming            name_prefix / tags consistent, no hardcoded names
    pinning           required_version, provider source FQN, versions bounded
    antipatterns      provider block in module, backend in module,
                      count=0 disable, local-exec inside, depends_on misuse
    security_hints    Resource="*"  Action="*"  0.0.0.0/0  public_acl  publicly_accessible

When --target-version is given, additionally:
    upgrade_diff      removed variables, new required variables
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "iac-state: 'module_validate' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="module_validate.py")
    p.add_argument("host")
    p.add_argument("module_path", help="Path to the module directory")
    p.add_argument("--target-version", default=None,
                   help="If set, compare against this version (registry lookup)")
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
        "data": {"host": args.host, "module_path": args.module_path,
                 "target_version": args.target_version, "tool": args.tool},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
