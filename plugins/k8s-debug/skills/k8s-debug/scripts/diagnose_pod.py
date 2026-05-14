#!/usr/bin/env python3
"""diagnose_pod.py — drill into a single Pod (scaffold).

v1.0 surface:
    diagnose_pod.py <host> <namespace> <pod> [--container NAME]
                    [--json] [--logs-tail 200] [--previous]

Runs in parallel:
    kubectl describe pod
    kubectl logs (current + --previous)
    kubectl get events --field-selector involvedObject.name=<pod>
    kubectl top pod (if metrics-server available)

Outputs the classifications used by references/pod_lifecycle.md:
    Pending  | ContainerCreating | Running-NotReady | Running-Restarting
    | Running-Healthy | Terminating | CrashLoopBackOff | ImagePullBackOff
    | OOMKilled

Each finding includes a `hint` field pointing to the relevant section in
references/ for the LLM to follow.
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "k8s-debug: 'diagnose_pod' is not implemented in v0.1.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="diagnose_pod.py")
    p.add_argument("host")
    p.add_argument("namespace")
    p.add_argument("pod")
    p.add_argument("--container", default=None, help="Specific container in the pod")
    p.add_argument("--logs-tail", type=int, default=200)
    p.add_argument("--previous", action="store_true",
                   help="Include --previous container logs (crucial for CrashLoop)")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"host": args.host, "namespace": args.namespace, "pod": args.pod},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
