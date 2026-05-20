#!/usr/bin/env python3
"""port_check.py — TCP reachability probe FROM a remote host (scaffold).

The trick: this runs `nc -zv` (or its fallback) on the remote source, not
on your laptop. The question "can the app server reach the database?"
only has the right answer when asked from the right place.

v1.0 surface:
    port_check.py <alias> --target <host> --ports 5432,6379,9092
    port_check.py --from a,b,c --to db-1,cache-1 --ports 5432,6379
        # produces a matrix: source × (target, port) -> reachable | filtered | refused

Implementation notes:
    - Default tool: `nc -zv -w 2`. Fallback to `bash -c "timeout 2 cat </dev/tcp/host/port"`
      when nc is unavailable.
    - --udp uses `nc -uzv -w 2`. UDP "open" results are unreliable; we
      annotate them as "open|filtered".
    - Output (JSON) includes per-cell exit_code and elapsed_ms.
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = "remote-debug: 'port_check' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="port_check.py")
    p.add_argument("alias", nargs="?", help="Single-source mode: host alias")
    p.add_argument("--from", dest="src_hosts",
                   help="Multi-source mode: comma-separated aliases")
    p.add_argument("--target", help="Single target (single-source mode)")
    p.add_argument("--to", dest="dst_hosts",
                   help="Multi-target mode: comma-separated targets")
    p.add_argument("--ports", required=True,
                   help="Comma-separated TCP ports (or 'a:b' ranges)")
    p.add_argument("--udp", action="store_true")
    p.add_argument("--timeout", type=int, default=2)
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"alias": args.alias, "from": args.src_hosts,
                 "target": args.target, "to": args.dst_hosts,
                 "ports": args.ports, "udp": args.udp},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
