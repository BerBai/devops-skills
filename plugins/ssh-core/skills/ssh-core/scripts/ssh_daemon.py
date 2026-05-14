#!/usr/bin/env python3
"""ssh_daemon.py — long-lived connection pool for one alias (scaffold).

v1.0 design:
    - Each alias gets ONE local daemon process listening on 127.0.0.1:<random>.
    - Wire protocol: 4-byte big-endian length prefix + UTF-8 JSON body.
    - Heartbeat: paramiko transport.send_ignore() every 60 s.
    - Idle exit: 30 min default, override with --idle-timeout.
    - State at $TMPDIR/ssh_daemon/<md5(alias)>.json mode 0o600:
        { "pid": int, "port": int, "started_at": iso8601,
          "last_used_at": iso8601, "alias": str }
    - Multiple Claude Code processes share the same daemon — the state file
      is the rendezvous, first writer wins, others connect.

Subcommands (planned):
    start <alias> [--idle-timeout N] [--no-detach]
    status <alias>            -> daemon health JSON
    stop <alias>              -> remove state file + SIGTERM recorded PID
    list                      -> all daemons known to this user

Today: argparse skeleton only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import unimplemented  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_daemon.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="Start (or attach to) a daemon for an alias")
    s.add_argument("alias")
    s.add_argument("--idle-timeout", type=int, default=1800)
    s.add_argument("--no-detach", action="store_true")

    sub.add_parser("status", help="Show daemon health").add_argument("alias")
    sub.add_parser("stop", help="Stop a daemon and remove its state file").add_argument("alias")
    sub.add_parser("list", help="List daemons")

    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return unimplemented(f"ssh_daemon {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
