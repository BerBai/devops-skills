#!/usr/bin/env python3
"""compare_across_hosts.py — diff state across N hosts (scaffold).

The highest-leverage tool when "it works on the other one". Pulls the
same files / runs the same commands on each host, then diffs.

v1.0 surface:
    compare_across_hosts.py <alias1> <alias2> [<alias3> ...]
        [--files /etc/nginx/nginx.conf,/etc/sysctl.conf]
        [--commands "uname -r" "nginx -V" "systemctl list-units --state=failed"]
        [--packages]            # diff installed pkg lists (dpkg/rpm)
        [--json]

Output: per-comparison-target, a unified diff and a one-line "differs: yes/no".
The summary lists every target with a difference, sorted by impact heuristic
(/etc/* > /etc/sysctl* > package list > command output).
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = "remote-debug: 'compare_across_hosts' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="compare_across_hosts.py")
    p.add_argument("aliases", nargs="+", help="Two or more host aliases")
    p.add_argument("--files", help="Comma-separated remote file paths to diff")
    p.add_argument("--commands", nargs="+", default=[],
                   help='Each command, e.g. --commands "uname -r" "nginx -V"')
    p.add_argument("--packages", action="store_true",
                   help="Compare installed packages (dpkg/rpm autodetect)")
    p.add_argument("--context", type=int, default=3, help="Unified diff context lines")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if len(args.aliases) < 2:
        sys.stderr.write("Need at least two host aliases to compare.\n")
        return 2
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"aliases": args.aliases, "files": args.files,
                 "commands": args.commands, "packages": args.packages},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
