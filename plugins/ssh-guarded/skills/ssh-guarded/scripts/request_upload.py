#!/usr/bin/env python3
"""request_upload.py — draft a file-upload request (scaffold).

Produces an artifact at reports/requests/upload-<id>.json. Does NOT execute.

Usage (planned):
    request_upload.py <alias> --local <path> --remote <path>
        --reason "<intent>"
        [--overwrite]
        [--confirm-sensitive-local-upload]
        [--json]

Validation deferred to v1.0:
    - local path must be inside paths.upload_roots
    - remote path must be inside ~/workspace/<project_id>/
    - SHA-256 checksum + size computed and recorded
    - sensitive local sources require --confirm-sensitive-local-upload and --reason
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = (
    "ssh-guarded: 'request_upload' is not implemented in v0.1.0 scaffolding."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="request_upload.py")
    p.add_argument("alias")
    p.add_argument("--local", required=True)
    p.add_argument("--remote", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--confirm-sensitive-local-upload", action="store_true")
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
            "operation": "upload",
            "server": args.alias,
            "reason": args.reason,
            "payload": {
                "local_path": args.local,
                "remote_path": args.remote,
                "overwrite": args.overwrite,
                "sensitive_confirmed": args.confirm_sensitive_local_upload,
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
