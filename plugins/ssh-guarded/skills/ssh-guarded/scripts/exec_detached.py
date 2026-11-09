#!/usr/bin/env python3
"""exec_detached.py - fire-and-track long remote commands (v1.0).

Runs a command request artifact in the background on a remote host by
wrapping it in ``nohup``, capturing the remote PID, writing a remote log,
and recording a ``$SSH_GUARDED_HOME/reports/<project_id>/jobs/<job_id>.json``
state file.

v1.0 surface:
    exec_detached.py run --request <artifact> [--reports-dir PATH] [--json]
    exec_detached.py status <job_id> [--reports-dir PATH] [--json]
    exec_detached.py tail-log <job_id> [--lines N] [--follow] [--no-redact] [--json]
    exec_detached.py wait <job_id> [--timeout SEC] [--json]
    exec_detached.py list [--probe] [--json]

Job state lives outside the plugin tree (GAP-006): ``$SSH_GUARDED_HOME``
defaults to ``~/.devops/ssh-guarded`` and ``--reports-dir`` replaces the
``.../reports`` level for CI and containers.

See:
    .trellis/tasks/05-25-ssh-guarded-exec-detached/{prd,design,implement}.md
    .trellis/spec/backend/adr-001-ssh-execute.md
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_SCRIPTS_DIR = _THIS.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import (  # type: ignore[import-not-found]  # noqa: E402, I001
    ArtifactError,
    GuardedArgumentParser,
    LockHeld,
    Redactor,
    RequestArtifact,
    add_version_flag,
    emit,
    error_object,
    exclusive_lock,
    handle_version,
    iso_utc_now,
    json_envelope,
    resolve_project_id,
)
from lib import _ssh_core_locator as _locator  # type: ignore[import-not-found]  # noqa: E402
from lib import reports_root as state_reports_root  # noqa: E402
from lib import warn_legacy_reports  # noqa: E402

# --- Constants --------------------------------------------------------------

SKILL_ROOT = _THIS.parent.parent
#: Pre-GAP-006 location. Kept only so a caller (or a test) can name it; the
#: script never writes here. The migration note in ``lib/state.py`` reports on
#: it.
REPORTS_DIR = SKILL_ROOT / "reports"
#: Explicit override (unit tests set this to a tmp dir; ``--reports-dir`` and
#: ``$SSH_GUARDED_HOME`` both feed :data:`REPORTS_ROOT` instead).
JOBS_DIR: Path | None = None
#: Reports root for this invocation (``--reports-dir`` or ``$SSH_GUARDED_HOME``).
REPORTS_ROOT: Path | None = None
#: project_id for this invocation (``--project-id`` or the env var).
_resolved_project_id: str | None = None
# Resolved through the shared locator so DEVOPS_SSH_CORE_DIR is honoured.
SSH_EXECUTE = _locator.ssh_core_script("ssh_execute.py")

DEFAULT_TAIL_LINES = 200
DEFAULT_WAIT_TIMEOUT_SECONDS = 600
FIVE_MINUTES_SECONDS = 300
SSH_LAUNCH_TIMEOUT_SECONDS = 30
SSH_STATUS_TIMEOUT_SECONDS = 30
SSH_TAIL_TIMEOUT_SECONDS = 60
SSH_CONNECT_TIMEOUT_SECONDS = 8
DEFAULT_REDACT_CATALOG = Path.home() / ".devops" / "redact.json"

STATE_VERSION = 1
JOB_ID_HEX_CHARS = 8
COMMAND_EXCERPT_CHARS = 80
ARTIFACT_FILE_MODE = 0o600

EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_REQUEST_NOT_EXECUTABLE = 60
EXIT_NOHUP_DISPATCH_FAILED = 61
EXIT_PID_CAPTURE_FAILED = 62
EXIT_REMOTE_LOG_UNREACHABLE = 63
EXIT_JOB_VANISHED = 64
EXIT_JOB_STATE_CORRUPT = 65
EXIT_TIMEOUT = 124

FAIL_REQUEST_NOT_EXECUTABLE = "request_not_executable"
FAIL_NOHUP_DISPATCH_FAILED = "nohup_dispatch_failed"
FAIL_PID_CAPTURE_FAILED = "pid_capture_failed"
FAIL_REMOTE_LOG_UNREACHABLE = "remote_log_unreachable"
FAIL_JOB_VANISHED = "job_vanished"
FAIL_JOB_STATE_CORRUPT = "job_state_corrupt"

EXIT_SENTINEL = "__EXEC_DETACHED_EXIT__="
_SENTINEL_RE = re.compile(r"__EXEC_DETACHED_EXIT__=(-?\d+)")


class DetachedError(Exception):
    """Structured failure mapped to the shared JSON envelope."""

    def __init__(
        self,
        exit_code: int,
        failure_class: str | None,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.failure_class = failure_class
        self.message = message
        self.data = data or {}


@dataclass(frozen=True)
class JobState:
    version: int
    job_id: str
    request_id: str
    server: str
    remote_pid: int
    remote_log_path: str
    started_at: str
    command_excerpt: str
    cwd_on_remote: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "request_id": self.request_id,
            "server": self.server,
            "remote_pid": self.remote_pid,
            "remote_log_path": self.remote_log_path,
            "started_at": self.started_at,
            "command_excerpt": self.command_excerpt,
            "cwd_on_remote": self.cwd_on_remote,
        }


# --- Parser -----------------------------------------------------------------


def _build_common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--project-id", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "--reports-dir",
        dest="reports_dir",
        default=argparse.SUPPRESS,
        help=(
            "Override the state root that holds <project_id>/jobs/ "
            "(default: $SSH_GUARDED_HOME/reports)"
        ),
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    p = GuardedArgumentParser(
        prog="exec_detached.py",
        parents=[common],
        description="Fire-and-track long remote commands via nohup + job state.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", parents=[common], help="Launch a command request in the background")
    r.add_argument("--request", required=True, help="Path to a command request artifact")

    s = sub.add_parser("status", parents=[common], help="Probe one detached job")
    s.add_argument("job_id", help="8-hex job id")

    t = sub.add_parser("tail-log", parents=[common], help="Tail a detached job's remote log")
    t.add_argument("job_id", help="8-hex job id")
    t.add_argument("--lines", type=int, default=DEFAULT_TAIL_LINES)
    t.add_argument("--follow", action="store_true", help="Stream via ssh tail -F")
    t.add_argument(
        "--no-redact",
        action="store_true",
        help="Raw output; do not paste into shared transcripts",
    )

    w = sub.add_parser("wait", parents=[common], help="Poll status until the job is terminal")
    w.add_argument("job_id", help="8-hex job id")
    w.add_argument("--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_SECONDS)

    ls = sub.add_parser("list", parents=[common], help="List local detached job state files")
    ls.add_argument("--probe", action="store_true", help="SSH-probe each job before printing")

    add_version_flag(p)
    return p


# --- Envelope / emit --------------------------------------------------------


def _json_result(
    success: bool,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared envelope (GAP-005): six keys, ``error.exit_code`` == top level."""
    payload = data.copy() if data else {}
    payload.setdefault("failure_class", None if success else "remote_error")
    error = None
    if not success:
        error = error_object(
            payload.get("failure_class"),
            stderr.strip() or f"exit {exit_code}",
            exit_code,
        )
    return json_envelope(
        success=success,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        data=payload,
        error=error,
    )


def _success(data: dict[str, Any], stdout: str = "") -> dict[str, Any]:
    data = data.copy()
    data.setdefault("failure_class", None)
    return _json_result(True, EXIT_OK, stdout=stdout, data=data)


def _failure(
    exit_code: int,
    failure_class: str | None,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = data.copy() if data else {}
    payload["failure_class"] = failure_class
    return _json_result(False, exit_code, stderr=message, data=payload)


def _emit(result: dict[str, Any], as_json: bool) -> None:
    emit(result, as_json)


def _human_lines_for_data(data: dict[str, Any]) -> str:
    if "jobs" in data:
        rows = data["jobs"]
        if not rows:
            return "no detached jobs\n"
        lines = []
        for row in rows:
            parts = [
                str(row.get("job_id", "?")),
                str(row.get("state", "unknown")),
                str(row.get("server", "?")),
                str(row.get("started_at", "?")),
                str(row.get("command_excerpt", "")),
            ]
            lines.append("  ".join(parts).rstrip())
        return "\n".join(lines) + "\n"
    if "lines" in data:
        return "\n".join(str(line) for line in data["lines"]) + ("\n" if data["lines"] else "")
    return json.dumps(data, ensure_ascii=False) + "\n"


# --- Path / JSON helpers ----------------------------------------------------


def _quote_remote_path(path: str) -> str:
    """Quote a remote path while preserving shell expansion for leading ``~/``."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        suffix = path[2:]
        return "~/" + shlex.quote(suffix) if suffix else "~/"
    return shlex.quote(path)


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, ARTIFACT_FILE_MODE)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def _jobs_root() -> Path:
    """Directory holding ``<job_id>.json`` for the current invocation.

    Resolution order: an explicit ``JOBS_DIR`` (unit tests), then
    ``--reports-dir`` (``REPORTS_ROOT``), then ``$SSH_GUARDED_HOME``, then the
    project-scoped default ``<reports>/<project_id>/jobs``. Never the plugin
    tree (GAP-006).
    """
    if JOBS_DIR is not None:
        return Path(JOBS_DIR)
    reports = REPORTS_ROOT or state_reports_root()
    project_id = (
        _resolved_project_id
        or os.environ.get("SSH_GUARDED_PROJECT_ID", "")
        or "default"
    )
    return Path(reports) / project_id / "jobs"


def _job_path(job_id: str) -> Path:
    return _jobs_root() / f"{job_id}.json"


def _load_job(job_id: str) -> JobState:
    path = _job_path(job_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise DetachedError(
            EXIT_JOB_STATE_CORRUPT,
            FAIL_JOB_STATE_CORRUPT,
            f"job state file invalid: {path} ({exc})",
        ) from exc
    if not isinstance(raw, dict):
        raise DetachedError(
            EXIT_JOB_STATE_CORRUPT,
            FAIL_JOB_STATE_CORRUPT,
            f"job state file invalid: {path} (top-level is not an object)",
        )
    required = (
        "version",
        "job_id",
        "request_id",
        "server",
        "remote_pid",
        "remote_log_path",
        "started_at",
        "command_excerpt",
        "cwd_on_remote",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise DetachedError(
            EXIT_JOB_STATE_CORRUPT,
            FAIL_JOB_STATE_CORRUPT,
            f"job state file invalid: {path} (missing {', '.join(missing)})",
        )
    try:
        remote_pid = int(raw["remote_pid"])
        version = int(raw["version"])
    except (TypeError, ValueError) as exc:
        raise DetachedError(
            EXIT_JOB_STATE_CORRUPT,
            FAIL_JOB_STATE_CORRUPT,
            f"job state file invalid: {path} (pid/version not integer)",
        ) from exc
    return JobState(
        version=version,
        job_id=str(raw["job_id"]),
        request_id=str(raw["request_id"]),
        server=str(raw["server"]),
        remote_pid=remote_pid,
        remote_log_path=str(raw["remote_log_path"]),
        started_at=str(raw["started_at"]),
        command_excerpt=str(raw["command_excerpt"]),
        cwd_on_remote=str(raw["cwd_on_remote"]),
    )


def _write_job(job: JobState) -> Path:
    path = _job_path(job.job_id)
    _atomic_write_json(path, job.to_dict())
    return path


def _iter_job_files() -> list[Path]:
    root = _jobs_root()
    if not root.exists():
        return []
    return sorted(root.glob("*.json"))


def _find_existing_job(request_id: str) -> JobState | None:
    for path in _iter_job_files():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and raw.get("request_id") == request_id:
            try:
                return _load_job(str(raw.get("job_id") or path.stem))
            except DetachedError:
                continue
    return None


def _command_excerpt(command: str) -> str:
    if len(command) <= COMMAND_EXCERPT_CHARS:
        return command
    return command[:COMMAND_EXCERPT_CHARS] + "..."


# --- SSH helpers ------------------------------------------------------------


def _ssh_execute(server: str, command: str, timeout: int) -> dict[str, Any]:
    argv = [sys.executable, str(SSH_EXECUTE), server, command, "--timeout", str(timeout), "--json"]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _failure(
            EXIT_TIMEOUT,
            "timeout",
            f"ssh_execute timed out after {timeout}s",
            data={"ssh_argv": argv},
        )
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _failure(
            EXIT_GENERIC,
            "remote_error",
            "ssh_execute did not emit JSON on stdout",
            data={"ssh_argv": argv, "returncode": proc.returncode, "stderr": proc.stderr[:4000]},
        )
    if not isinstance(env, dict):
        return _failure(
            EXIT_GENERIC,
            "remote_error",
            "ssh_execute JSON envelope was not an object",
            data={"ssh_argv": argv, "returncode": proc.returncode},
        )
    return env


def _parse_remote_pid(stdout: str) -> int:
    last = next((line.strip() for line in reversed(stdout.splitlines()) if line.strip()), "")
    try:
        pid = int(last)
    except ValueError as exc:
        raise DetachedError(
            EXIT_PID_CAPTURE_FAILED,
            FAIL_PID_CAPTURE_FAILED,
            f"expected integer PID on stdout, got: {last!r}",
            data={"last_stdout_line": last},
        ) from exc
    if pid <= 0:
        raise DetachedError(
            EXIT_PID_CAPTURE_FAILED,
            FAIL_PID_CAPTURE_FAILED,
            f"expected positive integer PID on stdout, got: {last!r}",
            data={"last_stdout_line": last},
        )
    return pid


def _build_nohup_command(command: str, cwd_on_remote: str, remote_log_path: str) -> str:
    if "\n" in command or "\r" in command:
        raise DetachedError(
            EXIT_REQUEST_NOT_EXECUTABLE,
            FAIL_REQUEST_NOT_EXECUTABLE,
            "exec_detached v1.0 requires a single-line command; use a script file for multiline payloads",
            data={"subclass": "command_not_single_line"},
        )
    log_dir = posixpath.dirname(remote_log_path)
    q_log = _quote_remote_path(remote_log_path)
    q_log_dir = _quote_remote_path(log_dir)
    q_cwd = _quote_remote_path(cwd_on_remote)
    body = (
        f"cd {q_cwd}; "
        "cmd_rc=$?; "
        "if [ \"$cmd_rc\" -eq 0 ]; then "
        f"{command}; cmd_rc=$?; "
        "fi; "
        f"printf '\\n{EXIT_SENTINEL}%s\\n' \"$cmd_rc\" >> {q_log}; "
        "exit \"$cmd_rc\""
    )
    return (
        f"mkdir -p {q_log_dir} && "
        f"( nohup bash -c {_shell_quote(body)} > {q_log} 2>&1 < /dev/null & echo $! )"
    )


def _remote_log_for(job_id: str, cwd_on_remote: str) -> str:
    return posixpath.join(cwd_on_remote, ".exec-detached", f"{job_id}.log")


def _status_payload(job: JobState) -> dict[str, Any]:
    probe_cmd = f"kill -0 {job.remote_pid} 2>/dev/null && echo alive || echo dead"
    probe = _ssh_execute(job.server, probe_cmd, SSH_STATUS_TIMEOUT_SECONDS)
    if str(probe.get("stdout", "")).strip() == "alive":
        return {
            "job_id": job.job_id,
            "state": "running",
            "remote_pid": job.remote_pid,
            "server": job.server,
            "failure_class": None,
        }

    tail_cmd = f"tail -n 5 {_quote_remote_path(job.remote_log_path)}"
    tail = _ssh_execute(job.server, tail_cmd, SSH_TAIL_TIMEOUT_SECONDS)
    if int(tail.get("exit_code", 1)) != 0:
        raise DetachedError(
            EXIT_REMOTE_LOG_UNREACHABLE,
            FAIL_REMOTE_LOG_UNREACHABLE,
            f"remote log unreachable: {job.remote_log_path} ({tail.get('stderr', '')})",
            data={"job_id": job.job_id, "remote_log_path": job.remote_log_path},
        )
    match = _SENTINEL_RE.search(str(tail.get("stdout", "")))
    if match:
        return {
            "job_id": job.job_id,
            "state": "exited",
            "exit_code": int(match.group(1)),
            "remote_pid": job.remote_pid,
            "server": job.server,
            "failure_class": None,
        }
    raise DetachedError(
        EXIT_JOB_VANISHED,
        FAIL_JOB_VANISHED,
        f"PID {job.remote_pid} dead and no exit sentinel in {job.remote_log_path}",
        data={"job_id": job.job_id, "remote_pid": job.remote_pid},
    )


# --- Subcommands ------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    global _resolved_project_id
    request_path = Path(args.request).expanduser().resolve()
    try:
        art = RequestArtifact.load(request_path)
        project_id = resolve_project_id(args, dict(os.environ), Path.cwd())
        art.validate(project_id=project_id)
    except ArtifactError as exc:
        raise DetachedError(
            exc.exit_code,
            exc.failure_class,
            exc.message,
        ) from exc

    _resolved_project_id = project_id
    warn_legacy_reports()

    if art.operation != "command":
        raise DetachedError(
            EXIT_REQUEST_NOT_EXECUTABLE,
            FAIL_REQUEST_NOT_EXECUTABLE,
            (
                "exec_detached only runs operation='command'; "
                f"got '{art.operation}'. Use run_request.py for {art.operation}."
            ),
            data={"operation": art.operation},
        )

    try:
        with exclusive_lock(request_path):
            existing = _find_existing_job(art.request_id)
            if existing is not None:
                raise DetachedError(
                    EXIT_REQUEST_NOT_EXECUTABLE,
                    FAIL_REQUEST_NOT_EXECUTABLE,
                    f"request {art.request_id} already dispatched as job {existing.job_id}",
                    data={
                        "subclass": "request_already_executed",
                        "existing_job_id": existing.job_id,
                    },
                )

            job_id = uuid.uuid4().hex[:JOB_ID_HEX_CHARS]
            command = str(art.payload.get("command", ""))
            cwd_on_remote = str(art.payload.get("workdir") or f"~/workspace/{project_id}")
            remote_log_path = _remote_log_for(job_id, cwd_on_remote)
            launcher = _build_nohup_command(command, cwd_on_remote, remote_log_path)
            launched = _ssh_execute(art.server, launcher, SSH_LAUNCH_TIMEOUT_SECONDS)
            if int(launched.get("exit_code", 1)) != 0:
                raise DetachedError(
                    EXIT_NOHUP_DISPATCH_FAILED,
                    FAIL_NOHUP_DISPATCH_FAILED,
                    f"nohup dispatch failed: {str(launched.get('stderr', '')).strip()}",
                    data={"request_id": art.request_id},
                )
            remote_pid = _parse_remote_pid(str(launched.get("stdout", "")))
            started_at = iso_utc_now()
            job = JobState(
                version=STATE_VERSION,
                job_id=job_id,
                request_id=art.request_id,
                server=art.server,
                remote_pid=remote_pid,
                remote_log_path=remote_log_path,
                started_at=started_at,
                command_excerpt=_command_excerpt(command),
                cwd_on_remote=cwd_on_remote,
            )
            _write_job(job)
            return _success(
                {
                    "job_id": job_id,
                    "remote_pid": remote_pid,
                    "remote_log_path": remote_log_path,
                    "server": art.server,
                    "started_at": started_at,
                    "request_id": art.request_id,
                },
                stdout=f"started {job_id} pid={remote_pid} log={remote_log_path}\n",
            )
    except LockHeld as exc:
        raise DetachedError(
            EXIT_REQUEST_NOT_EXECUTABLE,
            FAIL_REQUEST_NOT_EXECUTABLE,
            f"another exec_detached run holds {request_path}.lock (pid {exc.holder_pid})",
            data={"subclass": "lock_held", "holding_pid": exc.holder_pid},
        ) from exc


def _cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    job = _load_job(args.job_id)
    data = _status_payload(job)
    return _success(data, stdout=_human_lines_for_data(data))


def _redact_lines(lines: list[str], no_redact: bool) -> list[str]:
    if no_redact:
        return lines
    redactor = Redactor(DEFAULT_REDACT_CATALOG)
    return [redactor.apply(line)[0] for line in lines]


def _cmd_tail_log(args: argparse.Namespace) -> dict[str, Any]:
    if args.lines < 0:
        raise DetachedError(EXIT_GENERIC, "remote_error", "--lines must be >= 0")
    if args.follow:
        if getattr(args, "json", False):
            raise DetachedError(
                EXIT_GENERIC,
                "remote_error",
                "--json --follow is not supported in v1.0; use non-follow --json or follow without --json",
            )
        return _cmd_tail_follow(args)

    job = _load_job(args.job_id)
    cmd = f"tail -n {args.lines} {_quote_remote_path(job.remote_log_path)}"
    tail = _ssh_execute(job.server, cmd, SSH_TAIL_TIMEOUT_SECONDS)
    if int(tail.get("exit_code", 1)) != 0:
        raise DetachedError(
            EXIT_REMOTE_LOG_UNREACHABLE,
            FAIL_REMOTE_LOG_UNREACHABLE,
            f"remote log unreachable: {job.remote_log_path} ({tail.get('stderr', '')})",
        )
    lines = str(tail.get("stdout", "")).splitlines()
    redacted = _redact_lines(lines, bool(args.no_redact))
    data = {
        "job_id": job.job_id,
        "remote_log_path": job.remote_log_path,
        "lines": redacted,
        "failure_class": None,
    }
    return _success(data, stdout=_human_lines_for_data(data))


def _cmd_tail_follow(args: argparse.Namespace) -> dict[str, Any]:
    job = _load_job(args.job_id)
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
        job.server,
        f"tail -F -n {args.lines} {_quote_remote_path(job.remote_log_path)}",
    ]
    redactor = None if args.no_redact else Redactor(DEFAULT_REDACT_CATALOG)
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                out = redactor.apply(line)[0] if redactor is not None else line
                sys.stdout.write(out)
                sys.stdout.flush()
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)
        return _success({"job_id": job.job_id, "state": "interrupted"}, stdout="")
    if rc != 0:
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        raise DetachedError(
            EXIT_REMOTE_LOG_UNREACHABLE,
            FAIL_REMOTE_LOG_UNREACHABLE,
            f"remote log unreachable: {job.remote_log_path} ({stderr})",
        )
    return _success({"job_id": job.job_id, "state": "follow_complete"}, stdout="")


def _wait_intervals() -> list[int]:
    intervals: list[int] = []
    elapsed = 0
    for _ in range(30):
        intervals.append(1)
        elapsed += 1
    while elapsed < FIVE_MINUTES_SECONDS:
        intervals.append(5)
        elapsed += 5
    while elapsed < DEFAULT_WAIT_TIMEOUT_SECONDS * 4:
        intervals.append(15)
        elapsed += 15
    return intervals


def _cmd_wait(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout < 0:
        raise DetachedError(EXIT_GENERIC, "remote_error", "--timeout must be >= 0")
    deadline = time.monotonic() + args.timeout
    last: dict[str, Any] | None = None
    intervals = _wait_intervals()
    idx = 0
    while True:
        try:
            job = _load_job(args.job_id)
            last = _status_payload(job)
            if last["state"] in {"exited", "vanished"}:
                return _success(last, stdout=_human_lines_for_data(last))
        except DetachedError as exc:
            if exc.failure_class == FAIL_JOB_VANISHED:
                data = exc.data.copy()
                data.update({"state": "vanished", "failure_class": FAIL_JOB_VANISHED})
                return _failure(exc.exit_code, exc.failure_class, exc.message, data=data)
            raise

        now = time.monotonic()
        if now >= deadline:
            data = (last or {"state": "unknown"}).copy()
            data["failure_class"] = None
            return _json_result(
                False,
                EXIT_TIMEOUT,
                stderr=f"wait timed out after {args.timeout}s; last state: {data.get('state')}",
                data=data,
            )
        interval = intervals[idx] if idx < len(intervals) else 30
        idx += 1
        time.sleep(min(interval, max(0.0, deadline - now)))


def _row_from_job_file(path: Path, probe: bool) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"job_id": path.stem, "state": "corrupt"}
    if not isinstance(raw, dict):
        return {"job_id": path.stem, "state": "corrupt"}
    row = {
        "job_id": str(raw.get("job_id", path.stem)),
        "server": str(raw.get("server", "")),
        "started_at": str(raw.get("started_at", "")),
        "command_excerpt": str(raw.get("command_excerpt", "")),
        "state": "unknown",
        "mtime": path.stat().st_mtime,
    }
    if probe:
        try:
            status = _status_payload(_load_job(str(row["job_id"])))
            row["state"] = status["state"]
            if "exit_code" in status:
                row["exit_code"] = status["exit_code"]
        except DetachedError as exc:
            row["state"] = f"probe_failed:{exc.failure_class}"
    return row


def _cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    jobs = [_row_from_job_file(path, args.probe) for path in _iter_job_files()]
    data = {"jobs": jobs, "failure_class": None}
    return _success(data, stdout=_human_lines_for_data(data))


# --- Main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    global _resolved_project_id, REPORTS_ROOT
    early = handle_version("exec_detached.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    _resolved_project_id = (
        getattr(args, "project_id", None)
        or os.environ.get("SSH_GUARDED_PROJECT_ID", "")
        or None
    )
    reports_override = getattr(args, "reports_dir", None)
    if reports_override:
        REPORTS_ROOT = Path(reports_override).expanduser()
    try:
        if args.cmd == "run":
            result = _cmd_run(args)
        elif args.cmd == "status":
            result = _cmd_status(args)
        elif args.cmd == "tail-log":
            result = _cmd_tail_log(args)
        elif args.cmd == "wait":
            result = _cmd_wait(args)
        elif args.cmd == "list":
            result = _cmd_list(args)
        else:
            result = _failure(EXIT_GENERIC, "remote_error", f"unknown subcommand: {args.cmd}")
    except DetachedError as exc:
        result = _failure(exc.exit_code, exc.failure_class, exc.message, data=exc.data)
    _emit(result, as_json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
