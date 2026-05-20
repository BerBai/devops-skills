#!/usr/bin/env python3
"""request_command.py — draft a remote-command request (scaffold).

Produces an artifact at reports/requests/command-<id>.json. Does NOT execute.

Usage (planned):
    request_command.py <alias> "<command>"
        --reason "<intent>"
        [--workdir ~/workspace/<id>]
        [--timeout 60]
        [--expires-in 1h]
        [--env KEY=VAL ...]
        [--json]
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = (
    "ssh-guarded: 'request_command' is not implemented in v0.2.0 scaffolding. "
    "See references/request-execute.md for the artifact format we intend to write."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="request_command.py")
    p.add_argument("alias")
    p.add_argument("command")
    p.add_argument("--reason", required=True)
    p.add_argument("--workdir", default=None)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--expires-in", default="1h")
    p.add_argument("--env", action="append", default=[], help="KEY=VAL (repeatable)")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"would_have_written": {
            "operation": "command",
            "server": args.alias,
            "reason": args.reason,
            "payload": {
                "command": args.command,
                "workdir": args.workdir,
                "timeout": args.timeout,
                "env": dict(kv.split("=", 1) for kv in args.env if "=" in kv),
            },
        }},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
