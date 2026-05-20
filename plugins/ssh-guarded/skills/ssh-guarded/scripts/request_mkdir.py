#!/usr/bin/env python3
"""request_mkdir.py — draft a remote mkdir request (scaffold).

v1.0 surface:
    request_mkdir.py <alias> --remote ~/workspace/<id>/staging/2026-05-14
        --reason "stage release artifacts before deploy"
        [--mode 0755]
        [--parents]          # mkdir -p
        [--json]

Same artifact format as request_command, with operation="mkdir":
    payload: { "remote_path": "...", "mode": "0755", "parents": true }
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "ssh-guarded: 'request_mkdir' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="request_mkdir.py")
    p.add_argument("alias")
    p.add_argument("--remote", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--mode", default="0755")
    p.add_argument("--parents", action="store_true")
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
            "operation": "mkdir",
            "server": args.alias,
            "reason": args.reason,
            "payload": {"remote_path": args.remote, "mode": args.mode,
                        "parents": args.parents},
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
