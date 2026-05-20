#!/usr/bin/env python3
"""image_audit.py — Docker image waste detector (scaffold).

v1.0 surface:
    image_audit.py <host> <image> [--threshold-mb 200] [--json]

Analysis:
    layers              per-layer size + cmd + age
    waste_findings      apt cache left in / pip cache left in / .git inside
                       / npm cache / build tools in runtime
    suggested_savings   estimated MB recoverable
    base_image          recommendation if a slimmer variant exists
    security_hints      USER root? setuid binaries? .ssh inside? secrets inside?
    summary             ok | warn | crit + top findings

Uses `docker history --no-trunc` plus inspection of well-known waste paths.
Does NOT pull the image — caller's responsibility.
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "docker-quick: 'image_audit' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="image_audit.py")
    p.add_argument("host")
    p.add_argument("image", help="e.g. myorg/api:1.2.3")
    p.add_argument("--threshold-mb", type=int, default=200,
                   help="Layer size threshold to flag as 'large'")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"host": args.host, "image": args.image, "threshold_mb": args.threshold_mb},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
