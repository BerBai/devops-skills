#!/usr/bin/env python3
"""tail_multi.py — multi-source log tail with unified prefix (scaffold).

v1.0 surface:
    tail_multi.py --sources <spec> [<spec>...]
        [--follow] [--since <duration>]
        [--min-level warn]
        [--suppress] [--aggressive-suppress]
        [--max-concurrent 8]
        [--no-default-filters]
        [--json]

Each source is a URL-like spec — see references/sources.md. Examples:
    journal://web-1/nginx
    docker://local/redis
    kube://prod/payment/payment-svc-*
    file://web-2/var/log/myapp/app.log?ts_regex=...&ts_format=...

Behavior:
    1. Resolve glob/brace specs to concrete sources.
    2. For each source, dispatch a fetcher (subprocess for local, ssh-core
       for remote).
    3. Time-skew probe each remote host once.
    4. Stream lines through normalizer → noise filter → level filter →
       suppressor → output writer.
    5. Output is prefixed `[host/source <ts>] <line>`; --json emits NDJSON.
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "log-aggregator: 'tail_multi' is not implemented in v0.1.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tail_multi.py")
    p.add_argument("--sources", nargs="+", required=True,
                   help="One or more source specs (see references/sources.md)")
    p.add_argument("--follow", action="store_true")
    p.add_argument("--since", default="5min",
                   help="Lookback duration or ISO timestamp")
    p.add_argument("--min-level",
                   choices=["trace", "debug", "info", "warn", "error", "fatal"],
                   default="trace")
    p.add_argument("--suppress", action="store_true",
                   help="Suppress repeated lines (syslog-style)")
    p.add_argument("--aggressive-suppress", action="store_true")
    p.add_argument("--max-concurrent", type=int, default=8)
    p.add_argument("--no-default-filters", action="store_true",
                   help="Disable built-in noise deny-list")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"sources": args.sources, "follow": args.follow, "since": args.since},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
