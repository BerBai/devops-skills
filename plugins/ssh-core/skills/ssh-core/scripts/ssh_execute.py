#!/usr/bin/env python3
"""ssh_execute.py — run a command on a remote host (scaffold).

v0.2.0 surface:
    python ssh_execute.py <alias> <command> [--timeout N] [--no-daemon] [--json]

v1.0 behavior (planned):
    - If `~/.ssh/config` declares an IdentityFile that exists and isn't
      passphrase-protected, spawn native `ssh` directly (so we inherit
      ControlMaster, ProxyJump, ForwardAgent for free).
    - Otherwise, look up the alias's daemon at $TMPDIR/ssh_daemon/<md5>.json
      and send a length-prefixed JSON frame:
          { "action": "execute", "command": <str>, "timeout": <int> }
      If no daemon, spawn one, then retry.
    - Always emit the shared JSON contract.

Today: refuses politely with exit code 2.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import unimplemented  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ssh_execute.py",
        description="Run a command on a remote host. See SKILL.md for the workflow.",
    )
    p.add_argument("alias", help="Host alias from ~/.ssh/config")
    p.add_argument("command", help="Shell command to run on the remote host")
    p.add_argument("--timeout", type=int, default=120, help="Per-call timeout in seconds")
    p.add_argument("--no-daemon", action="store_true", help="Skip the local daemon")
    p.add_argument("--json", action="store_true", help="Emit the shared JSON contract")
    return p


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return unimplemented("ssh_execute")


if __name__ == "__main__":
    sys.exit(main())
