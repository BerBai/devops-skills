#!/usr/bin/env python3
"""cluster_health.py — cluster-wide health snapshot (scaffold).

v1.0 surface:
    cluster_health.py <host> [--json] [--since 30]

Collected:
    nodes              count + Ready / NotReady / SchedulingDisabled
    node_conditions    DiskPressure / MemoryPressure / PIDPressure / NetworkUnavailable
    control_plane      kube-apiserver / kube-controller-manager / kube-scheduler / etcd
                       (pods in kube-system if exposed; else from /healthz)
    kube_system        pods total / failing
    events             kube-system Warning+ events in last <since> min
    versions           kubectl version --short + node kubelet versions
    summary            scored ok | warn | crit + top findings

Use this as the **first** check before drilling into a namespace.
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "k8s-debug: 'cluster_health' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cluster_health.py")
    p.add_argument("host", help="ssh-core alias, or 'local'")
    p.add_argument("--since", type=int, default=30,
                   help="kube-system Events lookback minutes")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"host": args.host, "since_min": args.since},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
