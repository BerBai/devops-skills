#!/usr/bin/env python3
"""runner_check.py — CI runner pool health (scaffold).

v1.0 surface:
    runner_check.py <provider> [--org X | --group Y | --repo X/Y | --project G/P]
                               [--json]

For each runner:
    name, status (online/offline), busy, labels/tags,
    last_heartbeat (when self-hosted), running_job (if busy),
    machine_info (when available)

Aggregate:
    online_count / offline_count / busy_count
    label_coverage    {label -> count of runners}
    queue_depth       count of queued jobs
    queue_p95_age_sec p95 of queue dwell time
    summary           ok | warn | crit
"""

from __future__ import annotations

import argparse
import json
import sys

UNIMPLEMENTED_NOTE = "cicd-debug: 'runner_check' is not implemented in v0.1.0 scaffolding."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runner_check.py")
    p.add_argument("provider", choices=["gh", "glab"])
    p.add_argument("--org", default=None)
    p.add_argument("--group", default=None)
    p.add_argument("--repo", default=None)
    p.add_argument("--project", default=None)
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not any([args.org, args.group, args.repo, args.project]):
        sys.stderr.write("Need one of --org / --group / --repo / --project.\n")
        return 2
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"provider": args.provider, "scope": {
            "org": args.org, "group": args.group,
            "repo": args.repo, "project": args.project}},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
