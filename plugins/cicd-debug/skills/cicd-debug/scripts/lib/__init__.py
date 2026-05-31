"""Shared helpers for cicd-debug scripts.

Plugins are independently distributable, so cicd-debug keeps its own small
copy of the JSON envelope and environment helpers instead of importing from a
different plugin.
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


def emit(
    result_or_data: dict[str, Any],
    exit_code: int | bool | None = None,
    stderr: str = "",
    json_mode: bool | None = None,
) -> int:
    """Emit the project JSON envelope.

    Supports both local call styles in this plugin:
    - emit(envelope, as_json)
    - emit(data, exit_code, stderr, json_mode)
    """
    if isinstance(exit_code, bool) and json_mode is None:
        result = result_or_data
        as_json = exit_code
        code = int(result.get("exit_code", 0))
    else:
        code = int(exit_code or 0)
        as_json = bool(json_mode)
        result = json_result(
            success=code == 0,
            exit_code=code,
            stdout="",
            stderr=stderr,
            data=result_or_data,
        )

    if as_json:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return code
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
        if not result["stdout"].endswith("\n"):
            sys.stdout.write("\n")
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
        if not result["stderr"].endswith("\n"):
            sys.stderr.write("\n")
    return code


def msys_safe_env() -> dict[str, str]:
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    return env


def unimplemented(name: str) -> int:
    result = json_result(
        success=False,
        exit_code=2,
        stderr=f"cicd-debug: '{name}' is not implemented in v0.2.0 scaffolding.\n",
    )
    return emit(result, "--json" in sys.argv)
