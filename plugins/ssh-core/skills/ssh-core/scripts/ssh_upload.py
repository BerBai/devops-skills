#!/usr/bin/env python3
"""ssh_upload.py — local → remote, SFTP with resume (scaffold).

v1.0 surface:
    ssh_upload.py <alias> <local-path> <remote-path>
        [--resume]                # SFTP partial + offset retry
        [--recursive]
        [--no-progress]
        [--chmod 0644]
        [--bwlimit KB/s]
        [--json]

Implementation notes:
    - Default 128 KB block size, configurable via --block-size.
    - Progress callback prints every 5% to stderr unless --no-progress.
    - --resume:
        1. SFTP stat remote target
        2. If exists and size < local, seek both files to remote size
        3. Continue write
        4. After EOF, compare sha256 of both ends; mismatch → fail.
    - All file-path argv goes through MSYS_NO_PATHCONV=1 (Windows safety).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import unimplemented  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_upload.py")
    p.add_argument("alias")
    p.add_argument("local_path")
    p.add_argument("remote_path")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--chmod", default=None)
    p.add_argument("--bwlimit", type=int, default=None)
    p.add_argument("--block-size", type=int, default=131072)
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return unimplemented("ssh_upload")


if __name__ == "__main__":
    sys.exit(main())
