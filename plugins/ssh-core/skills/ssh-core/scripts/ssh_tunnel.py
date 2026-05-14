#!/usr/bin/env python3
"""ssh_tunnel.py — managed port-forward daemons (scaffold).

v1.0 design:
    - Same daemon shape as ssh_daemon.py, but the connection holds an SSH
      port-forward open instead of an interactive channel.
    - One state file per (alias, local_port) at
      $TMPDIR/ssh_tunnel/<md5(alias-port)>.json:
        { "pid": int, "local_port": int, "remote_host": str,
          "remote_port": int, "alias": str, "started_at": iso8601 }
    - Local port pool: 10000–20000 when --local-port is omitted.
    - Idle exit: 30 min default.

Subcommands:
    start <alias> --remote-port N [--local-port N] [--remote-host host]
                  [--idle-timeout N] [--bind-address 127.0.0.1]
    stop <alias> [--port N]      # if --port omitted and only one forward, stops it
    status <alias>               # forwards for this alias
    list                         # all active forwards
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import unimplemented  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_tunnel.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("alias")
    s.add_argument("--remote-port", type=int, required=True)
    s.add_argument("--local-port", type=int, default=None)
    s.add_argument("--remote-host", default="localhost")
    s.add_argument("--bind-address", default="127.0.0.1")
    s.add_argument("--idle-timeout", type=int, default=1800)

    st = sub.add_parser("stop")
    st.add_argument("alias")
    st.add_argument("--port", type=int, default=None)

    sub.add_parser("status").add_argument("alias")
    sub.add_parser("list")

    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return unimplemented(f"ssh_tunnel {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
