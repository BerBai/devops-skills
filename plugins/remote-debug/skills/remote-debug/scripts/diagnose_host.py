#!/usr/bin/env python3
"""diagnose_host.py — 60-second health snapshot for a remote host (scaffold).

v1.0 surface:
    diagnose_host.py <alias> [--json] [--since 60]

What it collects, in parallel where possible:
    cpu        uptime + load + per-CPU mpstat + top by CPU
    memory     free + /proc/meminfo + top by RSS + OOM scan (dmesg last N min)
    disk       df + df -i + iostat + top by I/O
    network    ip -br link + ss -s + nstat retrans + conntrack saturation
    services   systemd failed units + recent restarts
    kernel     dmesg tail filtered for ERR|WARN|OOM|panic
    summary    each domain scored ok | warn | crit + top-3 findings

Output (JSON):
    {
      "success": true,
      "exit_code": 0,
      "stdout": "<human-readable summary>",
      "stderr": "",
      "data": {
        "alias": "...",
        "collected_at": "<iso8601>",
        "summary": {
          "cpu": "ok", "memory": "warn", "disk": "ok",
          "network": "ok", "services": "ok", "kernel": "ok"
        },
        "findings": [
          {"domain": "memory", "severity": "warn", "text": "swap usage 30% rising"},
          ...
        ],
        "raw": { ...per-domain structured data... }
      }
    }
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = (
    "remote-debug: 'diagnose_host' is not implemented in v0.1.0 scaffolding. "
    "See references/linux_diagnostics.md for the commands it will run."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="diagnose_host.py")
    p.add_argument("alias")
    p.add_argument("--since", type=int, default=60,
                   help="Look this many minutes back in dmesg/journal")
    p.add_argument("--skip", action="append", default=[],
                   choices=["cpu", "memory", "disk", "network", "services", "kernel"],
                   help="Domain to skip (repeatable)")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"alias": args.alias, "since_min": args.since, "skipped": args.skip},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
