#!/usr/bin/env python3
"""ssh_execute_spike.py — throwaway PoC for v1.0 ssh_execute decisions.

NOT for shipping. Goal: exercise subprocess+ssh edge cases (H1-H6) and
emit the v0.2 JSON output envelope so we can audit field adequacy.

Usage:
    python3 ssh_execute_spike.py <host> "<command>" \
        [--timeout N] [--ssh-config PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time

# Patterns we expect to filter out of SSH-client stderr (not remote output).
KNOWN_HOSTS_NOISE_PATTERNS = [
    re.compile(r"^Warning: Permanently added .* to the list of known hosts\.$"),
    re.compile(r"^Warning: the .* host key for .* was different from the key for the IP address.*"),
]

# Substrings used to classify failure modes from ssh client stderr.
FAILURE_CLASSIFIERS = [
    ("network", re.compile(r"Could not resolve hostname|Network is unreachable|No route to host|Connection refused|Connection timed out")),
    ("auth",    re.compile(r"Permission denied|publickey|password authentication failed|Authentication failed")),
    # remote_error is the residual: ssh returned non-zero but stderr doesn't match the above
]


def classify_failure(returncode: int, stderr: str) -> str | None:
    if returncode == 0:
        return None
    for label, pattern in FAILURE_CLASSIFIERS:
        if pattern.search(stderr):
            return label
    return "remote_error"


def filter_ssh_noise(stderr: str) -> tuple[str, list[str]]:
    """Split stderr into (real_stderr, ssh_client_noise_lines)."""
    real_lines = []
    noise_lines = []
    for line in stderr.splitlines():
        if any(p.match(line) for p in KNOWN_HOSTS_NOISE_PATTERNS):
            noise_lines.append(line)
        else:
            real_lines.append(line)
    return "\n".join(real_lines), noise_lines


def envelope(success: bool, exit_code: int, stdout: str, stderr: str, data: dict) -> dict:
    return {
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": data,
    }


def run_local(command: str, timeout: int) -> dict:
    """Local route: shlex split, no shell."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return envelope(False, 1, "", f"shlex parse error: {e}", {"route": "local", "shlex_argv": None})
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return envelope(False, 124, "", f"timed out after {timeout}s", {
            "route": "local", "shlex_argv": argv, "elapsed_s": round(time.monotonic() - start, 2),
        })
    except FileNotFoundError as e:
        return envelope(False, 127, "", f"command not found: {e}", {"route": "local", "shlex_argv": argv})
    return envelope(
        success=(proc.returncode == 0),
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        data={
            "route": "local",
            "shlex_argv": argv,
            "elapsed_s": round(time.monotonic() - start, 2),
        },
    )


def run_remote(host: str, command: str, timeout: int, ssh_config: str | None) -> dict:
    """Remote route: subprocess + native ssh, capture everything."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
    if ssh_config:
        argv += ["-F", ssh_config]
    argv += [host, command]

    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return envelope(False, 124, "", f"timed out after {timeout}s", {
            "route": "remote", "ssh_argv": argv, "elapsed_s": round(time.monotonic() - start, 2),
            "failure_class": "timeout",
        })

    real_stderr, noise = filter_ssh_noise(proc.stderr)
    failure_class = classify_failure(proc.returncode, proc.stderr)
    return envelope(
        success=(proc.returncode == 0),
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=real_stderr,
        data={
            "route": "remote",
            "ssh_argv": argv,
            "elapsed_s": round(time.monotonic() - start, 2),
            "ssh_noise_lines": noise,
            "failure_class": failure_class,
            "raw_stderr": proc.stderr,
        },
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ssh_execute_spike.py")
    p.add_argument("host", help="Host alias (or 'local')")
    p.add_argument("command", help="Command to run")
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--ssh-config", default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.host == "local":
        result = run_local(args.command, args.timeout)
    else:
        result = run_remote(args.host, args.command, args.timeout, args.ssh_config)

    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        # Mirror v0.2 lib/emit() rough behavior
        if result["stdout"]:
            sys.stdout.write(result["stdout"])
            if not result["stdout"].endswith("\n"):
                sys.stdout.write("\n")
        if result["stderr"]:
            sys.stderr.write(result["stderr"])
            if not result["stderr"].endswith("\n"):
                sys.stderr.write("\n")
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
