#!/usr/bin/env python3
"""deploy_pubkey.py — install your public key on a remote (scaffold).

The preferred public-key bootstrap path. Equivalent to `ssh-copy-id` but:
    - Goes through ssh-core's auth path (so it works behind ProxyJump)
    - Sets the right perms on ~/.ssh and authorized_keys
    - Optionally strips a password recorded in our ~/.ssh/config metadata
      after the key is in place (so you stop falling back to password auth)

v1.0 surface (planned):
    deploy_pubkey.py <alias>
        [--key ~/.ssh/id_ed25519.pub]
        [--strip-password]                # remove our managed password comment
        [--json]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import unimplemented  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deploy_pubkey.py")
    p.add_argument("alias")
    p.add_argument("--key", default="~/.ssh/id_ed25519.pub")
    p.add_argument("--strip-password", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return unimplemented("deploy_pubkey")


if __name__ == "__main__":
    sys.exit(main())
