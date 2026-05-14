#!/usr/bin/env python3
"""correlate.py — anchor-and-window correlation across sources (scaffold).

v1.0 surface:
    correlate.py --sources <spec>... --anchor <regex>
        [--window 30s] [--pair-mode] [--since 1h] [--json]

Workflow:
    1. Resolve specs to sources.
    2. Search each source for first line matching --anchor in --since window.
    3. Record anchor time T.
    4. Fetch all lines from all sources within [T - window, T + window].
    5. Normalize timestamps; sort across sources.
    6. Output three sections:
         === Anchor ===
         === Within window ===
         === Summary === (likely_root_cause_hint, sources_with_hits)

    --pair-mode: anchor matches twice (start/end of a span) → emit duration.
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "log-aggregator: 'correlate' is not implemented in v0.1.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="correlate.py")
    p.add_argument("--sources", nargs="+", required=True)
    p.add_argument("--anchor", required=True, help="regex matching the anchor line")
    p.add_argument("--window", default="30s",
                   help="Window each side of anchor (e.g. 30s, 2m)")
    p.add_argument("--pair-mode", action="store_true",
                   help="Anchor matches twice; emit duration")
    p.add_argument("--since", default="1h",
                   help="How far back to search for the anchor")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"sources": args.sources, "anchor": args.anchor,
                 "window": args.window, "since": args.since,
                 "pair_mode": args.pair_mode},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
