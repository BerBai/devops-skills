"""Shared helpers for ssh-core scripts.

Stable surface:
    json_result(success, exit_code, stdout, stderr, data) -> dict
    emit(result, as_json: bool) -> None  # prints to stdout
    msys_safe_env() -> dict              # adds MSYS_NO_PATHCONV=1 on Windows
    alias_state_path(alias, kind) -> Path

Everything else is an implementation detail and may move between versions.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def json_result(
    success: bool,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": data or {},
    }


def emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    # Human mode: stderr → stderr, stdout → stdout, exit code in trailer.
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
        if not result["stdout"].endswith("\n"):
            sys.stdout.write("\n")
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
        if not result["stderr"].endswith("\n"):
            sys.stderr.write("\n")


def msys_safe_env() -> dict[str, str]:
    """Return a copy of os.environ with MSYS_NO_PATHCONV=1 forced on.

    Harmless on non-Windows; required on Windows MSYS bash to stop POSIX
    paths from being rewritten into Windows drive paths in argv.
    """
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    return env


def alias_state_path(alias: str, kind: str = "ssh_daemon") -> Path:
    """Where to put per-alias state.

    Uses md5(alias) as the basename so casual `ls /tmp` does not leak host
    names. The directory mode is 0o700; callers should set 0o600 on the file.
    """
    base = Path(tempfile.gettempdir()) / kind
    base.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.md5(alias.encode("utf-8")).hexdigest()
    return base / f"{digest}.json"


def unimplemented(name: str) -> int:
    """Stub used by every script in v0.2.0 scaffolding."""
    result = json_result(
        success=False,
        exit_code=2,
        stderr=f"ssh-core: '{name}' is not implemented in v0.2.0 scaffolding.\n"
        "See CONTRIBUTING.md for the build-out roadmap.\n",
    )
    emit(result, as_json="--json" in sys.argv)
    return 2
