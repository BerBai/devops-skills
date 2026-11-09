"""Terraform/OpenTofu runner for iac-state scripts."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _ssh_core_locator as _locator

# Resolved through the shared locator so DEVOPS_SSH_CORE_DIR is honoured and
# the lookup survives the marketplace layout (each plugin in its own dir).
SSH_EXECUTE_PATH = _locator.ssh_core_script("ssh_execute.py")

_HOST_UNREACHABLE = 120
_TOOL_MISSING = 121
_PROJECT_DIR_INVALID = 122
_TIMEOUT = 124
_RC_COMMAND_NOT_FOUND = 127


@dataclass(frozen=True)
class TfResult:
    stdout: str
    stderr: str
    returncode: int
    elapsed_s: float
    argv: list[str]
    route: str
    json_data: Any = None


class TfError(Exception):
    def __init__(self, failure_class: str, exit_code: int, stderr: str) -> None:
        self.failure_class = failure_class
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"terraform failed [{failure_class}]: {stderr}")


def _classify_failure(returncode: int, stderr: str) -> tuple[str, int] | None:
    lower = stderr.lower()
    if returncode == 0:
        return None
    if returncode == _RC_COMMAND_NOT_FOUND or "command not found" in lower:
        return "tool_missing", _TOOL_MISSING
    if "no such file or directory" in lower or "can't cd" in lower or "cd:" in lower:
        return "project_dir_invalid", _PROJECT_DIR_INVALID
    return None


def _run_remote_shell(host: str, command: str, timeout: int) -> TfResult:
    if not SSH_EXECUTE_PATH.exists():
        raise TfError("host_unreachable", _HOST_UNREACHABLE, f"ssh_execute.py not found at {SSH_EXECUTE_PATH}")
    argv = [sys.executable, str(SSH_EXECUTE_PATH), host, command, "--timeout", str(timeout), "--json"]
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 15, check=False)
    except subprocess.TimeoutExpired as exc:
        raise TfError("timeout", _TIMEOUT, f"ssh_execute timeout after {timeout + 15}s: {exc}") from None
    elapsed = round(time.monotonic() - start, 3)
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TfError("host_unreachable", _HOST_UNREACHABLE, f"ssh_execute returned non-JSON output: {exc}") from None
    ssh_fc = envelope.get("data", {}).get("failure_class")
    if ssh_fc in {"network", "timeout", "auth"}:
        raise TfError("host_unreachable", _HOST_UNREACHABLE, envelope.get("stderr", "ssh transport failed"))
    returncode = int(envelope.get("exit_code", 0))
    stdout = str(envelope.get("stdout") or "")
    stderr = str(envelope.get("stderr") or "")
    return TfResult(stdout, stderr, returncode, elapsed, argv, "remote")


def resolve_tool(host: str, tool_arg: str) -> str:
    if tool_arg not in {"auto", "tofu", "terraform"}:
        raise TfError("tool_missing", _TOOL_MISSING, f"unsupported tool selection: {tool_arg}")
    candidates = ["tofu", "terraform"] if tool_arg == "auto" else [tool_arg]
    if host == "local":
        for candidate in candidates:
            if shutil.which(candidate):
                return candidate
        raise TfError("tool_missing", _TOOL_MISSING, "neither tofu nor terraform found in PATH")

    for candidate in candidates:
        result = _run_remote_shell(host, f"command -v {shlex.quote(candidate)}", timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return candidate
    raise TfError("tool_missing", _TOOL_MISSING, "neither tofu nor terraform found on remote host")


def run_tf(
    host: str,
    project_dir: str,
    args: list[str],
    *,
    tool: str | None = None,
    timeout: int = 60,
    parse_json: bool = False,
) -> TfResult:
    selected = tool or resolve_tool(host, "auto")
    if host == "local":
        project = Path(project_dir).expanduser()
        if not project.is_dir():
            raise TfError("project_dir_invalid", _PROJECT_DIR_INVALID, f"project dir is not a directory: {project_dir}")
        argv = [selected, *args]
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=project, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TfError("timeout", _TIMEOUT, f"{selected} timeout after {timeout}s: {exc}") from None
        except FileNotFoundError:
            raise TfError("tool_missing", _TOOL_MISSING, f"{selected} binary not found") from None
        elapsed = round(time.monotonic() - start, 3)
        classified = _classify_failure(proc.returncode, proc.stderr)
        if classified:
            failure_class, exit_code = classified
            raise TfError(failure_class, exit_code, proc.stderr)
        json_data = json.loads(proc.stdout) if parse_json and proc.stdout else None
        return TfResult(proc.stdout, proc.stderr, proc.returncode, elapsed, argv, "local", json_data)

    command = "cd " + shlex.quote(project_dir) + " && " + " ".join(shlex.quote(part) for part in [selected, *args])
    result = _run_remote_shell(host, command, timeout)
    classified = _classify_failure(result.returncode, result.stderr)
    if classified:
        failure_class, exit_code = classified
        raise TfError(failure_class, exit_code, result.stderr)
    json_data = json.loads(result.stdout) if parse_json and result.stdout else None
    return TfResult(result.stdout, result.stderr, result.returncode, result.elapsed_s, [selected, *args], "remote", json_data)


def read_text(host: str, project_dir: str, relative_path: str, timeout: int = 30) -> str:
    if host == "local":
        path = Path(project_dir).expanduser() / relative_path
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise TfError("not_initialized", 123, f"missing {relative_path}") from None
    command = "cd " + shlex.quote(project_dir) + " && cat " + shlex.quote(relative_path)
    result = _run_remote_shell(host, command, timeout)
    if result.returncode != 0:
        raise TfError("not_initialized", 123, result.stderr)
    return result.stdout


def list_remote_files(host: str, directory: str, pattern: str = "*.tf", timeout: int = 30) -> list[str]:
    """Return ``directory``'s entries matching ``pattern`` as bare file names.

    Used by the static analyser to read a *remote* module without copying it:
    ``ls -1`` on the far side, then one ``cat`` per file. ``--json`` on
    ssh_execute keeps the transport surface identical to every other call, so
    a failure here classifies the same way.

    Returns an empty list when the directory has no matches (not an error: an
    empty module is a legitimate, if useless, module).
    """
    # `find -maxdepth 1 -name <pattern> -printf '%f\n'` is not portable to BSD
    # find; `ls -1 -- <pattern>` is, and the pattern is shell-quoted so a glob
    # that matches nothing stays a literal the shell can reject cleanly.
    command = (
        "cd " + shlex.quote(directory) + " && "
        "ls -1 -- " + shlex.quote(pattern) + " 2>/dev/null || true"
    )
    result = _run_remote_shell(host, command, timeout)
    if result.returncode != 0:
        raise TfError("module_path_invalid", _PROJECT_DIR_INVALID, result.stderr or "cannot list module directory")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def read_remote_text(host: str, directory: str, filename: str, timeout: int = 30) -> str:
    """Read one file on ``host`` as text, refusing a missing file loudly."""
    command = "cd " + shlex.quote(directory) + " && cat " + shlex.quote(filename)
    result = _run_remote_shell(host, command, timeout)
    if result.returncode != 0:
        raise TfError(
            "module_path_invalid",
            _PROJECT_DIR_INVALID,
            result.stderr or f"cannot read {filename}",
        )
    return result.stdout


def remove_path(host: str, path: str, timeout: int = 15) -> TfResult | None:
    """Best-effort removal helper for temp artifacts created by iac-state scripts."""
    if host == "local":
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as exc:
            raise TfError("project_dir_invalid", _PROJECT_DIR_INVALID, str(exc)) from None
        return None
    return _run_remote_shell(host, "rm -f " + shlex.quote(path), timeout)
