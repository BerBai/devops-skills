#!/usr/bin/env python3
"""check_namespace.py — namespace health snapshot (scaffold).

v1.0 surface:
    check_namespace.py <host> <namespace> [--since 60] [--json]

Collected, in parallel where possible:
    pods         total / Running / CrashLoop / Pending / Failed
    events       last <since> minutes filtered for Warning+
    deployments  desired/ready/available, stuck rollouts
    services     endpoints empty?
    pvcs         pending / bound
    summary      each domain scored ok | warn | crit + top-3 findings

Execution model:
    host == "local" → subprocess.run(["kubectl", "-n", ns, ...])
    host != "local" → ssh-core ssh_execute.py <host> "kubectl -n ns ..."
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "k8s-debug: 'check_namespace' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="check_namespace.py")
    p.add_argument("host", help="ssh-core alias, or 'local'")
    p.add_argument("namespace")
    p.add_argument("--since", type=int, default=60,
                   help="Look this many minutes back in Events")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"host": args.host, "namespace": args.namespace, "since_min": args.since},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
