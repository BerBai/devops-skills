#!/usr/bin/env python3
"""run_request.py — validate-only or validate-and-execute (scaffold).

Validate (default):
    run_request.py --request reports/requests/<id>.json
        - re-checks expiry, server, paths, checksum
        - prints a redacted summary
        - exits 0 on valid, 1 on invalid

Execute:
    run_request.py --request reports/requests/<id>.json --execute
        - same validation
        - then hands off to ssh-core (ssh_execute / ssh_upload)
        - captures result into <id>.result.json next to the request
        - exits with the remote command's exit code

Concurrency:
    Acquires <id>.lock next to the artifact for the duration of --execute.
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = (
    "ssh-guarded: 'run_request' is not implemented in v0.1.0 scaffolding."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_request.py")
    p.add_argument("--request", required=True, help="Path to request JSON")
    p.add_argument("--execute", action="store_true",
                   help="Without this flag, only validates and reports.")
    p.add_argument("--show-sensitive", action="store_true",
                   help="Disable output redaction (use sparingly).")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"would_have": "executed" if args.execute else "validated",
                 "request_path": args.request},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
