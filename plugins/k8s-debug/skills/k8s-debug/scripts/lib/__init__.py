"""Shared helpers for k8s-debug scripts.

Stable surface:
    json_result(success, exit_code, stdout, stderr, data) -> dict
    emit(result, as_json: bool) -> None              # prints to stdout
    msys_safe_env() -> dict                          # adds MSYS_NO_PATHCONV=1 on Windows
    unimplemented(name) -> int                       # scaffold-era stub

The kubectl wrapper (lib.kubectl) imports nothing from here. Anything not
listed is an implementation detail and may move between versions.

These helpers are copy-paste-equivalent of the ssh-core lib counterparts
(plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py:26-67, 151-160).
Plugins are independently distributable units, so each plugin maintains
its own lib copy rather than cross-importing.
"""

from __future__ import annotations

import json
import os
import sys
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
    # Human mode: stderr → stderr, stdout → stdout.
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


def unimplemented(name: str) -> int:
    """Stub used by v0.2.0 scaffolding scripts."""
    result = json_result(
        success=False,
        exit_code=2,
        stderr=f"k8s-debug: '{name}' is not implemented in v0.2.0 scaffolding.\n",
    )
    emit(result, as_json="--json" in sys.argv)
    return 2
