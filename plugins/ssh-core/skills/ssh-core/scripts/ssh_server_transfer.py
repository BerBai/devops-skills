#!/usr/bin/env python3
"""ssh_server_transfer.py — move bytes between two remote hosts (scaffold).

v1.0 design — four modes:
    direct   src machine runs scp/rsync directly to dst (data does not
             touch the local machine). Requires src can reach dst's port 22.
    stream   local process bridges two open SFTP sessions. Use when src
             cannot egress to dst but local can reach both.
    hybrid   try direct, fall back to stream if direct fails.
    auto     default. Pick direct for files >100 MB when reachability is
             provable in <1s; otherwise stream.

Flags:
    --use-rsync       Replace scp with rsync in the direct path. Gives you
                      --partial, --checksum, --delete.
    --recursive       Tree copy.
    --bwlimit KB/s    Bandwidth cap on direct/rsync path.

Output: shared JSON contract with data = {
    "mode_used": "direct" | "stream",
    "bytes": <int>,
    "elapsed_s": <float>,
    "src": "<alias>:<path>", "dst": "<alias>:<path>"
}
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import unimplemented  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_server_transfer.py")
    p.add_argument("src_alias")
    p.add_argument("src_path")
    p.add_argument("dst_alias")
    p.add_argument("dst_path")
    p.add_argument("--mode", choices=["auto", "direct", "stream", "hybrid"], default="auto")
    p.add_argument("--use-rsync", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--bwlimit", type=int, default=None, help="kilobytes per second")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return unimplemented("ssh_server_transfer")


if __name__ == "__main__":
    sys.exit(main())
