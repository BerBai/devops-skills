#!/usr/bin/env python3
"""inspect_container.py — single-container snapshot (scaffold).

v1.0 surface:
    inspect_container.py <host> <name-or-id>
                         [--tail 100] [--json]

Collected:
    state             status / exit_code / OOMKilled / restart_count / health
    config            cmd / entrypoint / env / mounts / user
    health_log        last N healthcheck probe outputs (when present)
    logs              last --tail lines stdout/stderr
    summary           ok | warn | crit + top findings + hint links

Execution:
    host == "local" → subprocess.run(["docker", ...])
    host != "local" → ssh-core ssh_execute.py <host> "docker ..."
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "docker-quick: 'inspect_container' is not implemented in v0.2.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inspect_container.py")
    p.add_argument("host", help="ssh-core alias, or 'local'")
    p.add_argument("name", help="container name or id (id prefix works)")
    p.add_argument("--tail", type=int, default=100)
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"host": args.host, "name": args.name, "tail": args.tail},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
