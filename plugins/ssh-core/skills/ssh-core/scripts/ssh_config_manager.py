#!/usr/bin/env python3
"""ssh_config_manager.py — CRUD on ~/.ssh/config (scaffold).

v1.0 design:
    - Parse ~/.ssh/config into an AST that preserves comments and order.
    - We embed extra metadata as comment lines immediately *above* each
      Host block:
          # description: ...
          # environment: production | staging | dev
          # tags: web,nginx
          # location: aliyun-beijing
      The OpenSSH parser ignores these; ours reads them.
    - Every write produces ~/.ssh/config.bak.<unix-ts> first.
    - Always sets `IdentitiesOnly yes` on new hosts that declare an
      IdentityFile (avoids "Too many authentication failures").

Subcommands:
    create  --alias A --host H --user U [--port P] [--key PATH]
            [--proxy-jump bastion] [--environment X] [--tags a,b]
            [--description "..."]
    update  --alias A [<any of the above fields>]
    delete  --alias A
    list    [--environment X] [--tags a,b]
    find    <alias>           # one host's full record
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import unimplemented  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_config_manager.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--alias", required=True)
        sp.add_argument("--host")
        sp.add_argument("--user")
        sp.add_argument("--port", type=int)
        sp.add_argument("--key", help="Path to IdentityFile")
        sp.add_argument("--proxy-jump")
        sp.add_argument("--environment")
        sp.add_argument("--tags", help="Comma-separated")
        sp.add_argument("--description")

    add_common(sub.add_parser("create"))
    add_common(sub.add_parser("update"))
    sub.add_parser("delete").add_argument("--alias", required=True)

    ls = sub.add_parser("list")
    ls.add_argument("--environment")
    ls.add_argument("--tags")

    sub.add_parser("find").add_argument("alias")

    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return unimplemented(f"ssh_config_manager {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
