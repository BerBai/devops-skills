#!/usr/bin/env python3
"""scan_software.py — cached "what's installed?" probe (scaffold).

Runs a battery of `command -v X` + `X --version` checks on a host, caches
the result, answers fast forever after.

Cache location: ~/.devops/cache/<alias>/software.json
TTL: 7 days, force refresh with --refresh.

Detected tools (extensible via config/software_catalog.json):
    python, python3, pip, conda
    nodejs, npm, yarn, pnpm
    java, mvn, gradle
    gcc, g++, clang, cmake, make
    cuda (nvcc), nvidia_driver (nvidia-smi)
    docker, podman, kubectl, helm, kustomize
    terraform, tofu, ansible, helmfile, argocd, flux
    git, jq, yq, rsync, tmux
    vivado, vitis, vivado_hls   (Xilinx — scan /opt/Xilinx/*)

Usage (planned):
    scan_software.py <alias> [--refresh] [--json]
    scan_software.py <alias> --name kubectl       # one tool, cached
    scan_software.py <alias> --has docker         # exit 0/1
"""

from __future__ import annotations

import argparse
import json
import sys


UNIMPLEMENTED_NOTE = (
    "ssh-guarded: 'scan_software' is not implemented in v0.2.0 scaffolding."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scan_software.py")
    p.add_argument("alias")
    p.add_argument("--refresh", action="store_true", help="Ignore cache, re-probe")
    p.add_argument("--name", help="Report a single tool")
    p.add_argument("--has", help="Exit 0 if present, 1 if not (for shell branching)")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = {
        "success": False,
        "exit_code": 2,
        "stdout": "",
        "stderr": UNIMPLEMENTED_NOTE + "\n",
        "data": {"alias": args.alias},
    }
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stderr.write(payload["stderr"])
    return 2


if __name__ == "__main__":
    sys.exit(main())
