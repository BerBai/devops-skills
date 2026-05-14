#!/usr/bin/env python3
"""ssh_download.py — remote → local, SFTP with resume (scaffold).

v1.0 surface:
    ssh_download.py <alias> <remote-path> <local-path>
        [--resume]
        [--recursive]
        [--no-progress]
        [--bwlimit KB/s]
        [--json]

Mirror of ssh_upload.py with the direction inverted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import unimplemented  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_download.py")
    p.add_argument("alias")
    p.add_argument("remote_path")
    p.add_argument("local_path")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--bwlimit", type=int, default=None)
    p.add_argument("--block-size", type=int, default=131072)
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return unimplemented("ssh_download")


if __name__ == "__main__":
    sys.exit(main())
