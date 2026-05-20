#!/usr/bin/env python3
"""compose_status.py — docker compose stack diagnosis (scaffold).

v1.0 surface:
    compose_status.py <host> <project-dir> [--json]
        # project-dir is the directory containing compose.yml on <host>

Workflow:
    1. cd <project-dir> && docker compose config         → resolved final config
    2. docker compose ps --format json                   → service states
    3. For each non-running/unhealthy service:
         docker inspect <container>                      → state.Health.Log[]
         docker compose logs --tail 50 <svc>             → last logs
    4. Detect:
         - depends_on cycles
         - depends_on missing condition: service_healthy
         - port binds colliding with host listeners
         - volume name drift (project rename → orphan volumes)
         - healthcheck command tool missing (`curl` / `wget` etc.)
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "docker-quick: 'compose_status' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="compose_status.py")
    p.add_argument("host")
    p.add_argument("project_dir",
                   help="Directory on <host> containing compose.yml/compose.yaml")
    p.add_argument("--service", default=None,
                   help="Focus on one service (default: all)")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"host": args.host, "project_dir": args.project_dir,
                 "service": args.service},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
