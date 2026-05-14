#!/usr/bin/env python3
"""request_delete.py — draft a remote delete request (scaffold).

The most dangerous of the request_* family. Risk summary is mandatory and
non-trivial; recursive deletes get extra-loud warnings.

v1.0 surface:
    request_delete.py <alias> --remote ~/workspace/<id>/stale.log
        --reason "log older than retention policy"
        [--recursive]            # mandatory for directories
        [--json]

Payload (operation="delete"):
    { "remote_path": "...", "recursive": bool, "is_directory": bool }

Validation (deferred to v1.0):
    - remote_path inside ~/workspace/<project_id>/
    - --recursive required if path is a directory
    - deny set still applies (~/.ssh, .env, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "ssh-guarded: 'request_delete' is not implemented in v0.1.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="request_delete.py")
    p.add_argument("alias")
    p.add_argument("--remote", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--recursive", action="store_true")
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
            "operation": "delete",
            "server": args.alias,
            "reason": args.reason,
            "payload": {"remote_path": args.remote, "recursive": args.recursive},
            "risk_summary": [
                "irreversible delete",
                "recursive — entire subtree removed" if args.recursive else "single path",
            ],
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
