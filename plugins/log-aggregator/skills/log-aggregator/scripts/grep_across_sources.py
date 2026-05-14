#!/usr/bin/env python3
"""grep_across_sources.py — pattern hunt across many sources (scaffold).

v1.0 surface:
    grep_across_sources.py --sources <spec>... --pattern <regex>
        [--since 1h] [--format summary|raw|json]
        [--max-samples 5]

Output (default --format summary):
    Source                 Hits   Sample (first match)
    -----------------------------------------------------------
    journal://web-1/nginx  142    [14:01:55] 500 GET /api/charge
    journal://web-2/nginx  138    [14:01:56] 500 GET /api/charge
    journal://web-3/nginx  201    [14:01:54] 500 GET /api/charge
    kube://prod/payment/* 1832    [14:01:50] OOMKilled exitCode=137
    ...

--format raw : all matching lines unified with [source ts] prefix.
--format json: NDJSON, one event per line.

When --pattern is empty and --since is set, lists every source's line count
in the window (a "heat map" of log volume).
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "log-aggregator: 'grep_across_sources' is not implemented in v0.1.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grep_across_sources.py")
    p.add_argument("--sources", nargs="+", required=True)
    p.add_argument("--pattern", default=None,
                   help="regex; empty = heat map mode")
    p.add_argument("--since", default="1h")
    p.add_argument("--format", choices=["summary", "raw", "json"], default="summary")
    p.add_argument("--max-samples", type=int, default=5)
    p.add_argument("--json", action="store_true",
                   help="Equivalent to --format json")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"sources": args.sources, "pattern": args.pattern,
                 "since": args.since, "format": args.format},
    }
    if args.json or args.format == "json":
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
