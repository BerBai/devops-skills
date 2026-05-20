#!/usr/bin/env python3
"""tail_log.py — multi-host log tail with line prefixing (scaffold).

v1.0 surface:
    tail_log.py <alias> <path> [--lines N] [--since <duration>]
                              [--grep <regex>] [--follow]
    tail_log.py --hosts a,b,c <path> [...same flags...]
                              # interleaves output, prefixes each line "<alias>| ..."

Implementation notes:
    - One ssh-core ssh_execute per host. For --follow, opens a paramiko
      channel and streams. Multi-host follow uses a single thread per host.
    - --since accepts "5min", "1h", "2026-05-14T14:00:00Z". Translated to
      `--since` on journalctl when the path looks like a journalctl unit
      ("--unit nginx"); otherwise `awk` filters by mtime/header time.
    - --grep is a Python regex compiled locally and applied after stream.
      For huge files prefer running `grep` remotely (we'll surface a hint).
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = "remote-debug: 'tail_log' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tail_log.py")
    p.add_argument("path", nargs="?", help="Log file or systemd unit (with --unit)")
    p.add_argument("--unit", help="systemd unit to tail (uses journalctl)")
    p.add_argument("alias", nargs="?", default=None,
                   help="Host alias (single-host mode)")
    p.add_argument("--hosts", help="Comma-separated host aliases (multi-host mode)")
    p.add_argument("--lines", type=int, default=200)
    p.add_argument("--since", default=None,
                   help="5min | 1h | ISO8601 timestamp")
    p.add_argument("--grep", default=None, help="Python regex applied per line")
    p.add_argument("--follow", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"alias": args.alias, "hosts": args.hosts, "path": args.path,
                 "unit": args.unit, "follow": args.follow},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
