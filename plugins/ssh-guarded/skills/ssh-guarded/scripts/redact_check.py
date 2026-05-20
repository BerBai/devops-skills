#!/usr/bin/env python3
"""redact_check.py — preview what the redaction pipeline would scrub (scaffold).

Reads from stdin, applies the same secret-regex catalog ssh-guarded uses
on real output, prints the redacted form. Use this before pasting a
transcript or filing a bug.

v1.0 surface:
    redact_check.py [--catalog ~/.devops/redact.json] [--json] < input.log
    redact_check.py --catalog ~/.devops/redact.json --report-only < input.log

Output (default): the redacted text on stdout, summary on stderr:
    redact_check: 3 hits (aws_access_key=1, github_pat=1, jwt=1)

Output (--json):
    { "success": true, "data": { "hits": [
        {"kind": "aws_access_key", "line": 42, "match_len": 20}, ...
    ], "redacted_text_len": 12345 } }
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "ssh-guarded: 'redact_check' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="redact_check.py")
    p.add_argument("--catalog", default="~/.devops/redact.json",
                   help="Extra catalog file (merged with built-ins)")
    p.add_argument("--report-only", action="store_true",
                   help="Print only the hits summary, no redacted text")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {},
    }
    if "--json" in (argv or sys.argv):
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
