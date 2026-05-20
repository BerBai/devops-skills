#!/usr/bin/env python3
"""exec_detached.py — fire-and-track long remote commands (scaffold).

A "long" command is anything where the synchronous SSH timeout might fire
before the command finishes. Builds, snapshots, migrations, model training,
docker pulls of large images — all live here.

Subcommands (planned):
    run --request reports/requests/<id>.json
        - wraps the command in nohup … > <log> 2>&1 &
        - records reports/jobs/<job_id>.json with remote PID, log path
        - returns the job_id immediately

    status <job_id>
        - kill -0 <remote_pid> on the host
        - returns "running" | "exited" | "vanished"
        - on exited: best-effort exit code from log tail

    tail-log <job_id> [--lines N] [--follow]
        - plain `tail -n N <remote_log>` via ssh-core

    wait <job_id> [--timeout SEC]
        - polls status until exited or timeout

    list
        - all jobs known on this machine
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = (
    "ssh-guarded: 'exec_detached' is not implemented in v0.2.0 scaffolding."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="exec_detached.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--request", required=True)

    sub.add_parser("status").add_argument("job_id")

    t = sub.add_parser("tail-log")
    t.add_argument("job_id")
    t.add_argument("--lines", type=int, default=200)
    t.add_argument("--follow", action="store_true")

    w = sub.add_parser("wait")
    w.add_argument("job_id")
    w.add_argument("--timeout", type=int, default=3600)

    sub.add_parser("list")

    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + f" (subcommand: {args.cmd})\n",
        "data": {},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
