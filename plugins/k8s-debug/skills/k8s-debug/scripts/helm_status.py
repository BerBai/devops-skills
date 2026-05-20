#!/usr/bin/env python3
"""helm_status.py — Helm release health (scaffold).

v1.0 surface:
    helm_status.py <host> <namespace> <release> [--json]
    helm_status.py <host> --list-pending [--json]
        # lists ALL releases in pending-* state across namespaces

Per-release output:
    status              deployed | failed | pending-install | pending-upgrade
                       | pending-rollback | superseded | uninstalling | uninstalled
    history             last 5 revisions + their status + age
    associated_jobs    helm hook jobs that are stuck / failed
    stuck_resources    resources helm thinks it owns but external edits made
                       them drift
    summary            ok | warn | crit + recommended next step
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "k8s-debug: 'helm_status' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="helm_status.py")
    p.add_argument("host")
    p.add_argument("namespace", nargs="?", default=None)
    p.add_argument("release", nargs="?", default=None)
    p.add_argument("--list-pending", action="store_true",
                   help="List all pending-* releases cluster-wide")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.list_pending and (not args.namespace or not args.release):
        sys.stderr.write("Need <namespace> <release>, or --list-pending.\n")
        return 2
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"host": args.host, "namespace": args.namespace,
                 "release": args.release, "list_pending": args.list_pending},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
