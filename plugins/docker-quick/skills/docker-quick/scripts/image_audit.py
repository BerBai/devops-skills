#!/usr/bin/env python3
"""image_audit.py - Docker image layer waste detector (v1.0).

Public surface::

    image_audit.py <host> <image> [--threshold-mb 200]
                   [--runtime docker|podman] [--connect-timeout N]
                   [--json]

Behaviour
---------
Local host (``host == "local"``) shells out directly to ``docker``/
``podman`` via ``subprocess.run`` with an argv list. Remote hosts (any
other alias) go through ``ssh-core``'s ``ssh_execute.py`` as a CLI
subprocess. Per CONTRIBUTING.md L29 cross-plugin invocation is **always**
shell-out, never a Python import.

Per call the script issues exactly two docker invocations:

    1. ``<runtime> history --no-trunc --format "{{json .}}" <image>``
       JSON Lines, one layer per row. Each row contains ``Size`` (a
       human-readable string like ``"12.3MB"``), ``CreatedBy``,
       ``CreatedAt``, ``Comment``.

    2. ``<runtime> inspect --format "{{json .}}" <image>``
       Single JSON blob; we read ``Config.User`` to flag root images.
       Failure here is **non-fatal** -- we still emit layers based on
       the history output.

These feed the aggregated envelope, following ``docker-quick/SKILL.md``
lines 82-101::

    data.summary  = {state, total_size_mb, layer_count, large_layers}
    data.layers   = [{index, size_bytes, size_mb, created_by, created_at, ...}]
    data.findings = [{severity, kind, value, hint}, ...]
    data.raw      = {history_stdout, inspect_stdout}

Severity rules (see design.md S 7):

    warn   any layer Size > threshold_mb * 1024 * 1024
    warn   layer CreatedBy contains an apt/pip/npm install without a
           matching cache-clean substring
    info   Config.User in {"", "root", "0"} (NOT aggregated into summary)
    ok     otherwise

v1.0 does **not** introduce a ``crit`` tier -- nothing about a stale
layer is unrecoverable. ``crit`` therefore never appears in summary.

Top-level ``success`` is ``True`` iff the script ran to completion AND
no ``failure_class`` was set. (image_audit's ``state`` never reaches
``crit``, so it doesn't enter the success calculation.)

Exit codes follow ``error-handling.md``::

    0    script ran AND no failure_class set
    1    failure_class set (remote_error / precondition / parse_error /
         ssh_execute_broken / inspect_unavailable)
    2    argv error
    124  ssh_execute subprocess timed out (remote only)

See:
    plugins/docker-quick/skills/docker-quick/SKILL.md           output contract
    .trellis/spec/backend/adr-001-ssh-execute.md                ssh_execute contract
    .trellis/spec/backend/error-handling.md                     JSON envelope rules
    CONTRIBUTING.md line 29                                      no cross-plugin imports
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

# --- Constants ---------------------------------------------------------------

DEFAULT_RUNTIME = "docker"
ALLOWED_RUNTIMES = ("docker", "podman")
DEFAULT_THRESHOLD_MB = 200

HISTORY_TIMEOUT_S = 30
INSPECT_TIMEOUT_S = 30
DEFAULT_CONNECT_TIMEOUT_S = 8

HISTORY_RAW_KEEP_BYTES = 4096
INSPECT_RAW_KEEP_BYTES = 2048

CMD_HEAD_MAX_CHARS = 120  # snippet length we surface in finding values
HUMAN_LAYER_DUMP_CAP = 10  # max layers to render in non-JSON output

# Size unit table. Longest-first to defeat "MB" matching "B" suffix early.
_SIZE_UNITS_ORDERED = (
    ("TB", 1024 ** 4),
    ("GB", 1024 ** 3),
    ("MB", 1024 ** 2),
    ("KB", 1024),
    ("B", 1),
)

# Waste patterns: (kind, must_have, must_NOT_have). Case-sensitive substring
# checks against `CreatedBy`.
_WASTE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("apt_cache_left", "apt-get install", "rm -rf /var/lib/apt/lists"),
    ("pip_cache_left", "pip install", "--no-cache-dir"),
    ("npm_cache_left", "npm install", "npm cache clean"),
)

# Users that all mean "root".
ROOT_USERS = ("", "root", "0", "0:0")

# Severity ranks (lower = more severe; used for finding ordering).
SEVERITY_RANKS = {"crit": 0, "warn": 1, "info": 2}

# Exit codes (subset of error-handling.md).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124


# --- Envelope helpers (inlined; ssh-core's lib cannot be imported across
# plugin boundaries -- CONTRIBUTING.md line 29). ------------------------------


def _envelope(
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


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
        if not result["stdout"].endswith("\n"):
            sys.stdout.write("\n")
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
        if not result["stderr"].endswith("\n"):
            sys.stderr.write("\n")


# --- ssh_execute.py discovery (only used for remote routes) -----------------


def _ssh_execute_path() -> Path:
    """Locate ssh-core's ssh_execute.py relative to this file.

    Layout::

        plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py
        plugins/docker-quick/skills/docker-quick/scripts/image_audit.py
                                                                       ^ here

    parents[0]=scripts, [1]=docker-quick (skill), [2]=skills,
    [3]=docker-quick (plugin), [4]=plugins.
    """
    here = Path(__file__).resolve()
    return (
        here.parents[4]
        / "ssh-core"
        / "skills"
        / "ssh-core"
        / "scripts"
        / "ssh_execute.py"
    )


# --- Local execution --------------------------------------------------------


def _run_local(argv: list[str], timeout: int) -> dict[str, Any]:
    """Run a docker/podman command directly on this host (no ssh)."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=(
                "image_audit: local command timed out after "
                f"{timeout}s"
            ),
            data={"failure_class": "timeout"},
        )
    except FileNotFoundError as e:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"image_audit: command not found: {e}\n",
            data={"failure_class": "remote_error"},
        )
    return _envelope(
        proc.returncode == 0,
        proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        data={"failure_class": None if proc.returncode == 0 else "remote_error"},
    )


# --- Remote execution via ssh_execute ---------------------------------------


def _run_via_ssh_execute(
    ssh_exec: Path,
    host: str,
    command: str,
    timeout: int,
    connect_timeout: int,
) -> dict[str, Any]:
    """Run a shell command on `host` through ssh_execute.py; return envelope."""
    argv = [
        sys.executable,
        str(ssh_exec),
        host,
        command,
        "--json",
        "--connect-timeout",
        str(connect_timeout),
        "--timeout",
        str(timeout),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout + connect_timeout + 5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=(
                "image_audit: ssh_execute subprocess timed out after "
                f"{timeout + connect_timeout + 5}s"
            ),
            data={"failure_class": "timeout"},
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _envelope(
            False,
            proc.returncode if proc.returncode != 0 else EXIT_FAIL,
            stdout=proc.stdout,
            stderr=(
                "image_audit: ssh_execute produced non-JSON output. "
                f"stderr={proc.stderr!r}"
            ),
            data={"failure_class": "ssh_execute_broken"},
        )


# --- Per-command workers ----------------------------------------------------


def _history_image(
    host: str,
    image: str,
    runtime: str,
    connect_timeout: int,
    *,
    ssh_exec_override: Path | None = None,
) -> dict[str, Any]:
    """Run `<runtime> history --no-trunc --format "{{json .}}" <image>`."""
    if host == "local":
        return _run_local(
            [runtime, "history", "--no-trunc", "--format", "{{json .}}", image],
            HISTORY_TIMEOUT_S,
        )
    ssh_exec = ssh_exec_override or _ssh_execute_path()
    if not ssh_exec.exists():
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"image_audit: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
    cmd = (
        f"{runtime} history --no-trunc --format '{{{{json .}}}}' "
        f"{shlex.quote(image)}"
    )
    return _run_via_ssh_execute(
        ssh_exec, host, cmd, HISTORY_TIMEOUT_S, connect_timeout
    )


def _inspect_image(
    host: str,
    image: str,
    runtime: str,
    connect_timeout: int,
    *,
    ssh_exec_override: Path | None = None,
) -> dict[str, Any]:
    """Run `<runtime> inspect --format "{{json .}}" <image>`."""
    if host == "local":
        return _run_local(
            [runtime, "inspect", "--format", "{{json .}}", image],
            INSPECT_TIMEOUT_S,
        )
    ssh_exec = ssh_exec_override or _ssh_execute_path()
    if not ssh_exec.exists():
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"image_audit: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
    cmd = (
        f"{runtime} inspect --format '{{{{json .}}}}' {shlex.quote(image)}"
    )
    return _run_via_ssh_execute(
        ssh_exec, host, cmd, INSPECT_TIMEOUT_S, connect_timeout
    )


# --- Parsers ----------------------------------------------------------------


def _parse_size(s: str) -> int:
    """Parse a docker-formatted size string (``"12.3MB"``, ``"0B"``, etc.)
    into bytes. Returns 0 on any parse failure.
    """
    if not s:
        return 0
    s = s.strip()
    if not s:
        return 0
    upper = s.upper()
    for suffix, mul in _SIZE_UNITS_ORDERED:
        if upper.endswith(suffix):
            num_str = s[: -len(suffix)].strip()
            try:
                return int(float(num_str) * mul)
            except (ValueError, TypeError):
                return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _parse_history_output(
    stdout: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Parse docker history --format "{{json .}}" JSON Lines output.

    Returns (layers, error_msg). Each layer is a normalized dict with
    ``index`` / ``size_bytes`` / ``size_mb`` / ``created_by`` / etc.
    Empty stdout -> (None, error) because no image yields no layers.
    """
    text = (stdout or "").strip()
    if not text:
        return None, "empty stdout"
    layers: list[dict[str, Any]] = []
    for idx, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            return None, f"line {idx + 1} JSON parse failed: {e}"
        if not isinstance(row, dict):
            return None, f"line {idx + 1} not a JSON object"
        size_bytes = _parse_size(str(row.get("Size") or "0"))
        layers.append({
            "index": idx,
            "id": row.get("ID") or "",
            "size_raw": row.get("Size") or "",
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
            "created_by": row.get("CreatedBy") or "",
            "created_at": row.get("CreatedAt") or "",
            "comment": row.get("Comment") or "",
        })
    if not layers:
        return None, "no layer rows found"
    return layers, None


def _parse_inspect_output(
    stdout: str,
) -> tuple[dict[str, Any] | None, str | None]:
    text = (stdout or "").strip()
    if not text:
        return None, "empty stdout"
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as e:
        return None, str(e)
    if not isinstance(loaded, dict):
        return None, f"expected object, got {type(loaded).__name__}"
    return loaded, None


# --- Severity scoring -------------------------------------------------------


def _cmd_head(s: str) -> str:
    snippet = (s or "").strip()
    if len(snippet) <= CMD_HEAD_MAX_CHARS:
        return snippet
    return snippet[:CMD_HEAD_MAX_CHARS] + "..."


def _score_layers(
    layers: list[dict[str, Any]],
    threshold_bytes: int,
) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    saw_warn = False

    for layer in layers:
        size_bytes = layer.get("size_bytes") or 0
        cmd = layer.get("created_by") or ""

        if size_bytes > threshold_bytes:
            findings.append({
                "severity": "warn",
                "kind": "large_layer",
                "value": {
                    "index": layer.get("index"),
                    "size_mb": layer.get("size_mb"),
                    "cmd_head": _cmd_head(cmd),
                },
                "hint": "references/image_optimization.md#large-layer",
            })
            saw_warn = True

        for kind, must_have, must_not_have in _WASTE_PATTERNS:
            if must_have in cmd and must_not_have not in cmd:
                findings.append({
                    "severity": "warn",
                    "kind": kind,
                    "value": {
                        "index": layer.get("index"),
                        "cmd_head": _cmd_head(cmd),
                    },
                    "hint": f"references/image_optimization.md#{kind.replace('_', '-')}",
                })
                saw_warn = True

    state = "warn" if saw_warn else "ok"
    return state, findings


def _score_user(
    inspect_blob: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not inspect_blob:
        return []
    config = inspect_blob.get("Config") or {}
    user = str(config.get("User") or "").strip()
    if user in ROOT_USERS:
        return [{
            "severity": "info",
            "kind": "running_as_root",
            "value": user or "(default root)",
            "hint": "references/container_issues.md#root-user",
        }]
    return []


def _severity_rank(sev: str) -> int:
    return SEVERITY_RANKS.get(sev, 3)


# --- Human formatter ---------------------------------------------------------


def _format_human(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    findings = data.get("findings") or []
    layers = data.get("layers") or []
    lines = [
        f"host:        {data.get('host') or '(unknown)'}",
        f"image:       {data.get('image') or '(unknown)'}",
        f"runtime:     {data.get('runtime') or '(unknown)'}",
        f"summary:     state={summary.get('state', '?')}  "
        f"total_size_mb={summary.get('total_size_mb', 0)}  "
        f"layers={summary.get('layer_count', 0)}  "
        f"large_layers={summary.get('large_layers', 0)}",
    ]
    if layers:
        lines.append("layers:")
        for layer in layers[:HUMAN_LAYER_DUMP_CAP]:
            cmd_head = _cmd_head(layer.get("created_by") or "")
            lines.append(
                f"  [{layer.get('index'):>3}] "
                f"{layer.get('size_mb', 0):>7.2f} MB  {cmd_head}"
            )
        if len(layers) > HUMAN_LAYER_DUMP_CAP:
            lines.append(
                f"  ... ({len(layers) - HUMAN_LAYER_DUMP_CAP} more)"
            )
    if findings:
        lines.append("findings:")
        for f in findings:
            lines.append(
                f"  [{f['severity']:<4s}] {f['kind']:<20s} {f.get('value')}"
            )
    return "\n".join(lines) + "\n"


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="image_audit.py",
        description=(
            "Docker image layer waste detector via `docker history`. "
            "host == 'local' runs the runtime directly; any other host "
            "goes through ssh-core. Emits the shared JSON contract "
            "with --json."
        ),
    )
    p.add_argument(
        "host",
        help="ssh-core alias, or the literal 'local' for direct execution",
    )
    p.add_argument(
        "image",
        help="Image reference, e.g. myorg/api:1.2.3",
    )
    p.add_argument(
        "--threshold-mb",
        type=int,
        default=DEFAULT_THRESHOLD_MB,
        help=(
            "Layer size threshold (MB) above which a 'large_layer' "
            f"finding is emitted (default: {DEFAULT_THRESHOLD_MB})"
        ),
    )
    p.add_argument(
        "--runtime",
        default=DEFAULT_RUNTIME,
        choices=list(ALLOWED_RUNTIMES),
        help=f"Container runtime to invoke (default: {DEFAULT_RUNTIME})",
    )
    p.add_argument(
        "--connect-timeout",
        type=int,
        default=DEFAULT_CONNECT_TIMEOUT_S,
        help=(
            "SSH ConnectTimeout for remote hosts (ignored for 'local') "
            f"(default: {DEFAULT_CONNECT_TIMEOUT_S})"
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the shared JSON output contract on stdout",
    )
    return p


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    threshold_bytes = args.threshold_mb * 1024 * 1024

    # Step 1: history.
    hist_env = _history_image(
        args.host, args.image, args.runtime, args.connect_timeout
    )

    if not hist_env.get("success"):
        fc = (hist_env.get("data") or {}).get("failure_class") or "remote_error"
        result = _envelope(
            False,
            hist_env.get("exit_code") or EXIT_FAIL,
            stderr=hist_env.get("stderr") or "",
            data={
                "host": args.host,
                "image": args.image,
                "runtime": args.runtime,
                "threshold_mb": args.threshold_mb,
                "summary": {
                    "state": "ok",
                    "total_size_mb": 0,
                    "layer_count": 0,
                    "large_layers": 0,
                },
                "layers": [],
                "findings": [],
                "failure_class": fc,
                "raw": {
                    "history_stdout": (hist_env.get("stdout") or "")[:HISTORY_RAW_KEEP_BYTES],
                    "history_stderr": hist_env.get("stderr") or "",
                },
            },
        )
        _emit(result, args.json)
        return result["exit_code"]

    # Step 2: parse history.
    layers, parse_err = _parse_history_output(hist_env.get("stdout") or "")
    if layers is None:
        result = _envelope(
            False,
            EXIT_FAIL,
            stderr=f"image_audit: history parse failed ({parse_err})\n",
            data={
                "host": args.host,
                "image": args.image,
                "runtime": args.runtime,
                "threshold_mb": args.threshold_mb,
                "summary": {
                    "state": "ok",
                    "total_size_mb": 0,
                    "layer_count": 0,
                    "large_layers": 0,
                },
                "layers": [],
                "findings": [],
                "failure_class": "parse_error",
                "raw": {
                    "history_stdout": (hist_env.get("stdout") or "")[:HISTORY_RAW_KEEP_BYTES],
                },
            },
        )
        _emit(result, args.json)
        return EXIT_FAIL

    # Step 3: inspect (best-effort).
    inspect_env = _inspect_image(
        args.host, args.image, args.runtime, args.connect_timeout
    )
    inspect_blob: dict[str, Any] | None = None
    inspect_stderr_text = ""
    if inspect_env.get("success"):
        parsed_blob, inspect_parse_err = _parse_inspect_output(
            inspect_env.get("stdout") or ""
        )
        if parsed_blob is not None:
            inspect_blob = parsed_blob
        else:
            inspect_stderr_text = (
                f"image_audit: inspect parse failed ({inspect_parse_err})\n"
            )
    else:
        inspect_stderr_text = inspect_env.get("stderr") or ""

    # Step 4: score.
    state, layer_findings = _score_layers(layers, threshold_bytes)
    user_findings = _score_user(inspect_blob)

    findings = sorted(
        layer_findings + user_findings,
        key=lambda f: _severity_rank(f["severity"]),
    )

    total_size_bytes = sum(layer.get("size_bytes", 0) for layer in layers)
    large_layers = sum(
        1 for layer in layers
        if (layer.get("size_bytes") or 0) > threshold_bytes
    )

    failure_class: str | None = None
    if inspect_blob is None:
        failure_class = "inspect_unavailable"

    success = failure_class is None  # state never reaches crit in v1.0
    overall_exit = EXIT_OK if success else EXIT_FAIL

    summary = {
        "state": state,
        "total_size_mb": round(total_size_bytes / (1024 * 1024), 2),
        "layer_count": len(layers),
        "large_layers": large_layers,
    }

    data = {
        "host": args.host,
        "image": args.image,
        "runtime": args.runtime,
        "threshold_mb": args.threshold_mb,
        "summary": summary,
        "layers": layers,
        "findings": findings,
        "failure_class": failure_class,
        "raw": {
            "history_stdout": (hist_env.get("stdout") or "")[:HISTORY_RAW_KEEP_BYTES],
            "inspect_stdout": (
                (inspect_env.get("stdout") or "")[:INSPECT_RAW_KEEP_BYTES]
                if inspect_env.get("success") else ""
            ),
        },
    }
    if inspect_stderr_text:
        data["raw"]["inspect_stderr"] = inspect_stderr_text

    result = _envelope(success, overall_exit, stdout=_format_human(data), data=data)
    _emit(result, args.json)
    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
