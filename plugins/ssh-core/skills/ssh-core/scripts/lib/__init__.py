"""Shared helpers for ssh-core scripts.

Stable surface:
    json_result(success, exit_code, stdout, stderr, data) -> dict
    emit(result, as_json: bool) -> None              # prints to stdout
    msys_safe_env() -> dict                          # adds MSYS_NO_PATHCONV=1 on Windows
    alias_state_path(alias, kind) -> Path
    filter_ssh_noise(stderr) -> tuple[str, list[str]]  # split known-hosts noise out
    classify_failure(returncode, stderr) -> str | None # network|auth|remote_error|None

Everything else is an implementation detail and may move between versions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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


# Lines emitted by the ssh client itself that we want to keep out of the
# top-level `stderr` field. Add patterns sparingly — when in doubt, leave a
# line in real_stderr (fail-open). See ADR-001 D4 for the policy.
SSH_NOISE_PATTERNS = (
    re.compile(r"^Warning: Permanently added .* to the list of known hosts\.$"),
)


# Regex sets used by classify_failure. Both are deliberately scoped to
# returncode == 255 by the classifier so they cannot false-positive on
# remote commands that emit "Permission denied" in their own stderr.
# (255 is ssh's own conventional exit code for client-side failure.)
SSH_CLIENT_FAILURE_EXIT_CODE = 255

NETWORK_PATTERN = re.compile(
    r"Could not resolve hostname|Network is unreachable|"
    r"No route to host|Connection refused|Connection timed out"
)
AUTH_PATTERN = re.compile(
    r"Permission denied \(publickey|"
    r"password authentication failed|Authentication failed"
)


def filter_ssh_noise(stderr: str) -> tuple[str, list[str]]:
    """Split captured ssh stderr into (real_stderr, noise_lines).

    Fail-open: any line that does NOT match a pattern in SSH_NOISE_PATTERNS
    stays in real_stderr. The library of patterns is intentionally small;
    missing a real error is worse than emitting an extra ssh warning to
    the user.

    Preserves a trailing newline iff the input had one and at least one
    real line survived (fixes the spike's dropped-newline behavior).
    """
    real: list[str] = []
    noise: list[str] = []
    for line in stderr.splitlines():
        if any(p.match(line) for p in SSH_NOISE_PATTERNS):
            noise.append(line)
        else:
            real.append(line)
    out = "\n".join(real)
    if real and stderr.endswith("\n"):
        out += "\n"
    return out, noise


def classify_failure(returncode: int, stderr: str) -> str | None:
    """Map (returncode, stderr) to one of None / network / auth / remote_error.

    Returns None for `returncode == 0` (success). For non-zero codes, the
    ssh-client failure regexes only apply when `returncode == 255` (ssh's
    own exit code for client-side problems); everything else is bucketed
    as `remote_error`.

    Note: this function does NOT return "timeout". The caller is the only
    code path that knows whether `subprocess.TimeoutExpired` was raised,
    so timeout classification belongs there, not here.
    """
    if returncode == 0:
        return None
    if returncode == SSH_CLIENT_FAILURE_EXIT_CODE:
        if NETWORK_PATTERN.search(stderr):
            return "network"
        if AUTH_PATTERN.search(stderr):
            return "auth"
    return "remote_error"


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
