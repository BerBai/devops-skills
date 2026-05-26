#!/usr/bin/env python3
"""ssh_tunnel.py - managed local->remote SSH port forwards (v1.0).

v1.0 surface::

    ssh_tunnel.py start <alias>
        --remote-port N
        [--local-port N]              from pool 10000-20000 if omitted
        [--remote-host HOST]          default: localhost (forward target on dst side)
        [--bind-address ADDR]         default: 127.0.0.1
        [--idle-timeout N]            accepted but NO-OP in v1.0 (D2 deferred)
        [--connect-timeout N]         default: 8 seconds (mirrors ssh_execute)
        [--no-detach]                 debug: foreground, no state file
        [--json]
    ssh_tunnel.py stop <alias> [--port N] [--json]
    ssh_tunnel.py status <alias> [--json]
    ssh_tunnel.py list [--json]

Implementation notes:
    - Detach mechanism is Python-side ``subprocess.Popen(start_new_session=True)``
      (POSIX-only). ``Popen.pid`` is the detached ssh PID directly; no
      ``lsof`` / ``pgrep`` lookup needed. This is preferred over OpenSSH
      ``-fN`` whose own fork hides the daemon PID. POSIX-only because
      ``os.setsid`` does not exist on Windows.
    - ssh argv uses ``-N -L <bind>:<lport>:<rhost>:<rport>`` with
      ``ExitOnForwardFailure=yes`` so a forward-setup failure (bind race,
      remote-side refusal) exits ssh immediately and is caught by the
      parent's poll-loop.
    - State file at ``$TMPDIR/ssh_tunnel/<md5(alias-port)>.json`` (mode
      0o600, dir 0o700). Keyed by ``(alias, local_port)`` so the same
      alias can hold multiple forwards on different local ports.
    - ``--idle-timeout`` is accepted (surface stability) but is a no-op
      in v1.0; a stderr warning + ``data.flag_deferred=["--idle-timeout"]``
      mark the deferral. v1.1 will reintroduce idle exit, likely via
      ControlMaster integration.
    - ``--no-detach`` runs ssh foreground (no ``start_new_session``,
      no state file). Intended for interactive debugging only.

Failure classes (per-script namespace; do not extend lib.classify_failure):
    ``posix_only`` 30, ``port_busy`` 31, ``port_pool_exhausted`` 32,
    ``ssh_connect_failed`` 33, ``auth`` 34, ``forward_setup_failed`` 35,
    ``tunnel_not_found`` 36, ``ambiguous_stop`` 37, ``stale_state_only`` 38,
    ``kill_failed`` 39, ``timeout`` 124 (reserved), ``remote_error`` 1.

See:
    .trellis/tasks/05-24-ssh-core-ssh-tunnel/{prd,design,implement}.md
    .trellis/spec/backend/error-handling.md (exit-code namespace)
    references/workflows.md section 4 (port-forward tunnel)
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    AUTH_PATTERN,
    NETWORK_PATTERN,
    emit,
    json_result,
    msys_safe_env,
)

# --- Constants --------------------------------------------------------------

PORT_POOL_MIN = 10000
PORT_POOL_MAX = 20000
PORT_POOL_RETRIES = 5
KILL_GRACE_S = 3
KILL_FINAL_S = 1
EARLY_FAILURE_GRACE_S = 1
DEFAULT_CONNECT_TIMEOUT = 8
DEFAULT_IDLE_TIMEOUT = 1800
DEFAULT_REMOTE_HOST = "localhost"
DEFAULT_BIND_ADDRESS = "127.0.0.1"

# Failure classes (per-script namespace).
FAIL_POSIX_ONLY = "posix_only"
FAIL_PORT_BUSY = "port_busy"
FAIL_PORT_POOL_EXHAUSTED = "port_pool_exhausted"
FAIL_SSH_CONNECT_FAILED = "ssh_connect_failed"
FAIL_AUTH = "auth"
FAIL_FORWARD_SETUP_FAILED = "forward_setup_failed"
FAIL_TUNNEL_NOT_FOUND = "tunnel_not_found"
FAIL_AMBIGUOUS_STOP = "ambiguous_stop"
FAIL_STALE_STATE_ONLY = "stale_state_only"
FAIL_KILL_FAILED = "kill_failed"
FAIL_TIMEOUT = "timeout"
FAIL_REMOTE_ERROR = "remote_error"

# Exit codes - per-script range 30-39 registered in error-handling.md (2026-05-25).
_EXIT_CODES: dict[str, int] = {
    FAIL_POSIX_ONLY: 30,
    FAIL_PORT_BUSY: 31,
    FAIL_PORT_POOL_EXHAUSTED: 32,
    FAIL_SSH_CONNECT_FAILED: 33,
    FAIL_AUTH: 34,
    FAIL_FORWARD_SETUP_FAILED: 35,
    FAIL_TUNNEL_NOT_FOUND: 36,
    FAIL_AMBIGUOUS_STOP: 37,
    FAIL_STALE_STATE_ONLY: 38,
    FAIL_KILL_FAILED: 39,
    FAIL_TIMEOUT: 124,
    FAIL_REMOTE_ERROR: 1,
}


class _TunnelError(Exception):
    """Internal sentinel: a structured failure with class + exit code + message.

    Raised by helper functions (port allocator, classifiers) and caught
    by the subcommand body so each subcommand has exactly one place to
    map the sentinel to the failure envelope.
    """

    def __init__(self, failure_class: str, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.exit_code = exit_code
        self.message = message


# --- Parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser + 4 subparsers per PRD D9."""
    p = argparse.ArgumentParser(
        prog="ssh_tunnel.py",
        description=(
            "Manage local->remote SSH port forwards via a Python-side detached "
            "subprocess.Popen(start_new_session=True). v1.0 ships start/stop/"
            "status/list; --idle-timeout is no-op (deferred to v1.1)."
        ),
    )
    p.add_argument("--json", action="store_true", help="Emit the shared JSON envelope on stdout")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="Start a new port forward")
    s.add_argument("alias", help="SSH alias from ~/.ssh/config")
    s.add_argument("--remote-port", type=int, required=True, help="Remote port to forward")
    s.add_argument(
        "--local-port", type=int, default=None,
        help=f"Local port to bind (default: pick from pool {PORT_POOL_MIN}-{PORT_POOL_MAX})",
    )
    s.add_argument(
        "--remote-host", default=DEFAULT_REMOTE_HOST,
        help=f"Remote-side forward target host (default: {DEFAULT_REMOTE_HOST})",
    )
    s.add_argument(
        "--bind-address", default=DEFAULT_BIND_ADDRESS,
        help=f"Local bind address (default: {DEFAULT_BIND_ADDRESS})",
    )
    s.add_argument(
        "--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT,
        help="ACCEPTED but NO-OP in v1.0; tunnel runs until `stop` (v1.1 will revive)",
    )
    s.add_argument(
        "--connect-timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT,
        help=f"SSH ConnectTimeout in seconds (default: {DEFAULT_CONNECT_TIMEOUT})",
    )
    s.add_argument(
        "--no-detach", action="store_true",
        help="Debug: run ssh in the foreground; no state file is written",
    )
    s.add_argument("--json", action="store_true", help="Emit the shared JSON envelope on stdout")

    st = sub.add_parser("stop", help="Stop an existing port forward")
    st.add_argument("alias", help="SSH alias whose forward(s) to stop")
    st.add_argument(
        "--port", type=int, default=None,
        help="Local port of the forward; required if more than one exists for this alias",
    )
    st.add_argument("--json", action="store_true", help="Emit the shared JSON envelope on stdout")

    sts = sub.add_parser("status", help="Show forwards for this alias")
    sts.add_argument("alias", help="SSH alias whose forward(s) to show")
    sts.add_argument("--json", action="store_true", help="Emit the shared JSON envelope on stdout")

    ls = sub.add_parser("list", help="Show all active forwards")
    ls.add_argument("--json", action="store_true", help="Emit the shared JSON envelope on stdout")

    return p


# --- State file primitives --------------------------------------------------


@dataclass
class _StateEntry:
    """In-memory representation of one ``$TMPDIR/ssh_tunnel/<digest>.json``."""

    pid: int
    local_port: int
    remote_host: str
    remote_port: int
    bind_address: str
    alias: str
    started_at: str
    path: Path = field(default_factory=Path)


def _state_dir() -> Path:
    base = Path(tempfile.gettempdir()) / "ssh_tunnel"
    base.mkdir(mode=0o700, exist_ok=True)
    return base


def _state_path(alias: str, port: int) -> Path:
    digest = hashlib.md5(f"{alias}-{port}".encode()).hexdigest()
    return _state_dir() / f"{digest}.json"


def _write_state(entry: _StateEntry) -> None:
    """Atomic write: ``.tmp`` then ``replace``; final mode 0o600."""
    payload = {
        "pid": entry.pid,
        "local_port": entry.local_port,
        "remote_host": entry.remote_host,
        "remote_port": entry.remote_port,
        "bind_address": entry.bind_address,
        "alias": entry.alias,
        "started_at": entry.started_at,
    }
    path = _state_path(entry.alias, entry.local_port)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    entry.path = path


def _read_state(path: Path) -> _StateEntry | None:
    """Best-effort read; corrupt / missing-field files return ``None``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return _StateEntry(
            pid=int(raw["pid"]),
            local_port=int(raw["local_port"]),
            remote_host=str(raw["remote_host"]),
            remote_port=int(raw["remote_port"]),
            bind_address=str(raw["bind_address"]),
            alias=str(raw["alias"]),
            started_at=str(raw["started_at"]),
            path=path,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _scan_state() -> list[_StateEntry]:
    """Enumerate all parseable state files in ``$TMPDIR/ssh_tunnel/``."""
    base = _state_dir()
    out: list[_StateEntry] = []
    for p in sorted(base.glob("*.json")):
        entry = _read_state(p)
        if entry is not None:
            out.append(entry)
    return out


def _pid_alive(pid: int) -> bool:
    """POSIX-portable "is this PID alive?" probe via ``os.kill(pid, 0)``."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user; treat as alive.
        return True
    return True


# --- Port allocator ---------------------------------------------------------


def _port_free(port: int) -> bool:
    """Transient bind probe on 127.0.0.1; close = free at this instant."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _alloc_local_port(explicit: int | None) -> int:
    """Return either ``explicit`` (after free-check) or a random pool port.

    Raises ``_TunnelError(port_busy)`` for an explicitly-named port that
    is in use; ``_TunnelError(port_pool_exhausted)`` if all 5 random
    retries fail. A race between this probe and the subsequent ssh bind
    is unavoidable but caught downstream by ``ExitOnForwardFailure=yes``.
    """
    if explicit is not None:
        if not _port_free(explicit):
            raise _TunnelError(
                FAIL_PORT_BUSY, _EXIT_CODES[FAIL_PORT_BUSY],
                f"local port {explicit} is already in use",
            )
        return explicit
    for _ in range(PORT_POOL_RETRIES):
        port = random.randint(PORT_POOL_MIN, PORT_POOL_MAX)
        if _port_free(port):
            return port
    raise _TunnelError(
        FAIL_PORT_POOL_EXHAUSTED, _EXIT_CODES[FAIL_PORT_POOL_EXHAUSTED],
        f"no free port found in pool {PORT_POOL_MIN}-{PORT_POOL_MAX} after "
        f"{PORT_POOL_RETRIES} attempts",
    )


# --- Detach ssh spawn -------------------------------------------------------


def _posix() -> bool:
    """``True`` iff this platform supports ``start_new_session=True``."""
    return sys.platform != "win32" and hasattr(os, "setsid")


def _build_ssh_argv(
    alias: str, local_port: int, remote_host: str, remote_port: int,
    bind_address: str, connect_timeout: int,
) -> list[str]:
    """Compose argv for a detached ``ssh -N -L`` forward."""
    return [
        "ssh",
        "-N",
        "-L", f"{bind_address}:{local_port}:{remote_host}:{remote_port}",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
        alias,
    ]


def _spawn_ssh(argv: list[str], *, detach: bool) -> subprocess.Popen[bytes]:
    """Spawn ssh; with ``detach=True`` the child gets its own session."""
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "env": msys_safe_env(),
    }
    if detach:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _wait_for_forward(
    proc: subprocess.Popen[bytes], connect_timeout: int,
) -> tuple[bool, str]:
    """Wait up to ``connect_timeout + 1s`` for the forward to settle.

    Returns ``(alive_at_deadline, stderr_drained)``. If ssh exits within
    the window, ``alive_at_deadline`` is False and stderr is drained.
    If it stays alive, we close the stderr pipe on the parent side; the
    child keeps writing into kernel-buffered memory but never blocks.
    """
    deadline = time.monotonic() + connect_timeout + EARLY_FAILURE_GRACE_S
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            stderr_bytes = b""
            if proc.stderr is not None:
                try:
                    stderr_bytes = proc.stderr.read() or b""
                except OSError:
                    stderr_bytes = b""
            return False, stderr_bytes.decode("utf-8", "replace")
        time.sleep(0.1)
    if proc.stderr is not None:
        with contextlib.suppress(Exception):
            proc.stderr.close()
    return True, ""


def _classify_start_failure(stderr: str) -> tuple[str, int]:
    """Map drained ssh stderr to ``(failure_class, exit_code)``.

    Precedence: network -> auth -> forward_setup_failed. The
    network/auth regexes are reused from ``lib`` so behaviour stays in
    lockstep with the rest of the ssh-core surface.
    """
    if NETWORK_PATTERN.search(stderr):
        return FAIL_SSH_CONNECT_FAILED, _EXIT_CODES[FAIL_SSH_CONNECT_FAILED]
    if AUTH_PATTERN.search(stderr):
        return FAIL_AUTH, _EXIT_CODES[FAIL_AUTH]
    return FAIL_FORWARD_SETUP_FAILED, _EXIT_CODES[FAIL_FORWARD_SETUP_FAILED]


def _terminate_proc(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort cleanup of a spawned ssh on the start-path."""
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            proc.kill()


# --- Kill semantics ---------------------------------------------------------


def _kill_with_grace(pid: int) -> str:  # noqa: PLR0911 — distinct outcomes per branch
    """SIGTERM -> wait 3 s -> SIGKILL -> wait 1 s. Returns outcome marker.

    Outcomes: ``"stale"`` (pid already dead at entry), ``"killed"``
    (SIGTERM or SIGKILL succeeded), ``"kill_failed"`` (both signals
    failed or the process is still alive after both).
    """
    if not _pid_alive(pid):
        return "stale"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "stale"
    except PermissionError:
        return "kill_failed"

    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "killed"
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "killed"
    except PermissionError:
        return "kill_failed"

    deadline = time.monotonic() + KILL_FINAL_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "killed"
        time.sleep(0.1)
    return "kill_failed"


# --- Envelope helpers -------------------------------------------------------


def _emit(envelope: dict[str, Any], as_json: bool) -> int:
    """Emit envelope on the right channel and return its exit code."""
    emit(envelope, as_json=as_json)
    return int(envelope["exit_code"])


def _hint_line(text: str) -> str:
    """Normalise a one-line hint to end with ``\\n``."""
    if not text:
        return ""
    return text if text.endswith("\n") else text + "\n"


# --- start subcommand -------------------------------------------------------


def _cmd_start(args: argparse.Namespace) -> int:  # noqa: PLR0911 — distinct failure paths
    if args.no_detach:
        return _cmd_start_no_detach(args)

    if not _posix():
        return _emit(
            json_result(
                success=False,
                exit_code=_EXIT_CODES[FAIL_POSIX_ONLY],
                stderr=_hint_line(
                    "ssh_tunnel start requires POSIX (start_new_session/os.setsid); "
                    "Windows is not supported in v1.0"
                ),
                data={
                    "alias": args.alias,
                    "failure_class": FAIL_POSIX_ONLY,
                },
            ),
            args.json,
        )

    # --idle-timeout deferral marker (PRD D2): set whenever the user passed
    # the flag, regardless of value. argparse can't tell explicit-default
    # from default-default, so we inspect sys.argv directly. This is
    # acceptable because the flag is a strict no-op in v1.0 — over-warning
    # is fine; under-warning would silently break PRD D2.
    idle_user_set = _idle_timeout_user_set()

    # Allocate local port (may raise _TunnelError).
    start_time = time.monotonic()
    try:
        local_port = _alloc_local_port(args.local_port)
    except _TunnelError as e:
        elapsed = time.monotonic() - start_time
        return _emit(
            _failure_envelope_start(args, e, elapsed=elapsed),
            args.json,
        )

    argv = _build_ssh_argv(
        args.alias, local_port, args.remote_host, args.remote_port,
        args.bind_address, args.connect_timeout,
    )

    try:
        proc = _spawn_ssh(argv, detach=True)
    except OSError as e:
        elapsed = time.monotonic() - start_time
        err = _TunnelError(
            FAIL_REMOTE_ERROR, _EXIT_CODES[FAIL_REMOTE_ERROR],
            f"failed to spawn ssh: {e}",
        )
        return _emit(_failure_envelope_start(args, err, elapsed=elapsed), args.json)

    alive, stderr_text = _wait_for_forward(proc, args.connect_timeout)
    if not alive:
        fclass, exit_code = _classify_start_failure(stderr_text)
        err = _TunnelError(fclass, exit_code, stderr_text.strip() or "ssh exited early")
        # Process already died; just reap.
        with contextlib.suppress(Exception):
            proc.wait(timeout=1)
        elapsed = time.monotonic() - start_time
        return _emit(
            _failure_envelope_start(
                args, err, elapsed=elapsed, raw_stderr=stderr_text,
            ),
            args.json,
        )

    # Forward is up — persist state.
    started_at = datetime.now(timezone.utc).isoformat()
    entry = _StateEntry(
        pid=proc.pid,
        local_port=local_port,
        remote_host=args.remote_host,
        remote_port=args.remote_port,
        bind_address=args.bind_address,
        alias=args.alias,
        started_at=started_at,
    )
    try:
        _write_state(entry)
    except OSError as e:
        # Couldn't persist; tear down so we don't leak a tunnel without state.
        _terminate_proc(proc)
        elapsed = time.monotonic() - start_time
        err = _TunnelError(
            FAIL_REMOTE_ERROR, _EXIT_CODES[FAIL_REMOTE_ERROR],
            f"failed to write state file: {e}",
        )
        return _emit(_failure_envelope_start(args, err, elapsed=elapsed), args.json)

    elapsed = time.monotonic() - start_time
    return _emit(
        _success_envelope_start(args, entry, idle_user_set=idle_user_set, elapsed=elapsed),
        args.json,
    )


def _idle_timeout_user_set() -> bool:
    """Best-effort: was ``--idle-timeout`` present on the command line?

    Kept as a tiny helper so unit tests can monkeypatch ``sys.argv``
    without rebuilding the parser.
    """
    return any(a == "--idle-timeout" or a.startswith("--idle-timeout=") for a in sys.argv)


def _success_envelope_start(
    args: argparse.Namespace, entry: _StateEntry, *,
    idle_user_set: bool, elapsed: float,
) -> dict[str, Any]:
    """Build the success envelope for ``start`` per PRD D10."""
    data: dict[str, Any] = {
        "alias": entry.alias,
        "pid": entry.pid,
        "local_port": entry.local_port,
        "remote_host": entry.remote_host,
        "remote_port": entry.remote_port,
        "bind_address": entry.bind_address,
        "state_file": str(entry.path),
        "started_at": entry.started_at,
        "duration_ms": int(round(elapsed * 1000)),
        "failure_class": None,
    }
    stderr_text = ""
    if idle_user_set:
        data["flag_deferred"] = ["--idle-timeout"]
        stderr_text = _hint_line(
            "--idle-timeout is a no-op in v1.0; tunnel will run until `stop` "
            "(v1.1 will reintroduce idle exit via ControlMaster integration)"
        )
    human = (
        f"forward up: {entry.alias}:{entry.local_port} -> "
        f"{entry.remote_host}:{entry.remote_port} (pid {entry.pid})\n"
    )
    return json_result(
        success=True, exit_code=0, stdout=human, stderr=stderr_text, data=data,
    )


def _failure_envelope_start(
    args: argparse.Namespace, err: _TunnelError, *,
    elapsed: float = 0.0, raw_stderr: str = "",
) -> dict[str, Any]:
    """Build a failure envelope for ``start`` per PRD D10."""
    data: dict[str, Any] = {
        "alias": args.alias,
        "remote_host": getattr(args, "remote_host", DEFAULT_REMOTE_HOST),
        "remote_port": getattr(args, "remote_port", None),
        "bind_address": getattr(args, "bind_address", DEFAULT_BIND_ADDRESS),
        "local_port": getattr(args, "local_port", None),
        "duration_ms": int(round(elapsed * 1000)),
        "failure_class": err.failure_class,
        "raw_stderr": raw_stderr,
    }
    return json_result(
        success=False,
        exit_code=err.exit_code,
        stderr=_hint_line(err.message),
        data=data,
    )


# --- start --no-detach (debug) ---------------------------------------------


def _cmd_start_no_detach(args: argparse.Namespace) -> int:
    """Foreground run for debugging: spawn ssh as a normal child + wait()."""
    # Port allocation still applies so the user gets a real port number.
    try:
        local_port = _alloc_local_port(args.local_port)
    except _TunnelError as e:
        return _emit(
            _failure_envelope_start(args, e, elapsed=0.0),
            args.json,
        )

    argv = _build_ssh_argv(
        args.alias, local_port, args.remote_host, args.remote_port,
        args.bind_address, args.connect_timeout,
    )

    if args.json:
        # Emit one line announcing the parameters; the foreground wait
        # produces no further envelope (PRD D8).
        emit(
            json_result(
                success=True,
                exit_code=0,
                stdout="",
                stderr="",
                data={
                    "alias": args.alias,
                    "local_port": local_port,
                    "remote_host": args.remote_host,
                    "remote_port": args.remote_port,
                    "bind_address": args.bind_address,
                    "no_detach": True,
                    "failure_class": None,
                },
            ),
            as_json=True,
        )

    try:
        proc = subprocess.Popen(
            argv,
            stdin=None,
            stdout=None,
            stderr=None,
            env=msys_safe_env(),
        )
    except OSError as e:
        sys.stderr.write(f"failed to spawn ssh: {e}\n")
        return 1

    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            rc = proc.wait(timeout=2)
        return 130
    return int(rc)


# --- stop subcommand --------------------------------------------------------


def _cmd_stop(args: argparse.Namespace) -> int:
    entries = [e for e in _scan_state() if e.alias == args.alias]
    if args.port is not None:
        entries = [e for e in entries if e.local_port == args.port]
    if not entries:
        return _emit(
            _failure_envelope_stop(
                args,
                FAIL_TUNNEL_NOT_FOUND,
                message=(
                    f"no forward found for alias {args.alias!r}"
                    + (f" on port {args.port}" if args.port is not None else "")
                ),
            ),
            args.json,
        )

    if args.port is None and len(entries) > 1:
        ports = [e.local_port for e in entries]
        return _emit(
            _failure_envelope_stop(
                args,
                FAIL_AMBIGUOUS_STOP,
                message=(
                    f"multiple forwards for alias {args.alias!r} on ports "
                    f"{ports}; pass --port to disambiguate"
                ),
                ports_present=ports,
            ),
            args.json,
        )

    stopped: list[int] = []
    files_removed: list[str] = []
    errors: list[dict[str, Any]] = []
    for entry in entries:
        outcome = _kill_with_grace(entry.pid)
        try:
            entry.path.unlink()
            files_removed.append(str(entry.path))
        except FileNotFoundError:
            pass
        except OSError:
            # Permission issue or rmdir race; record but proceed.
            pass

        if outcome == "killed":
            stopped.append(entry.local_port)
        elif outcome == "stale":
            errors.append(
                {
                    "failure_class": FAIL_STALE_STATE_ONLY,
                    "exit_code": _EXIT_CODES[FAIL_STALE_STATE_ONLY],
                    "local_port": entry.local_port,
                }
            )
        else:  # kill_failed
            errors.append(
                {
                    "failure_class": FAIL_KILL_FAILED,
                    "exit_code": _EXIT_CODES[FAIL_KILL_FAILED],
                    "local_port": entry.local_port,
                }
            )

    if errors:
        # Pick the worst error to surface (kill_failed > stale).
        worst = next(
            (e for e in errors if e["failure_class"] == FAIL_KILL_FAILED),
            errors[0],
        )
        envelope = _failure_envelope_stop(
            args,
            str(worst["failure_class"]),
            message=(
                f"could not fully stop forward on port {worst['local_port']}: "
                f"{worst['failure_class']}"
            ),
            ports=stopped,
            files_removed=files_removed,
            errors=errors,
        )
        return _emit(envelope, args.json)

    return _emit(
        _success_envelope_stop(args, stopped, files_removed),
        args.json,
    )


def _success_envelope_stop(
    args: argparse.Namespace, stopped: list[int], files_removed: list[str],
) -> dict[str, Any]:
    data = {
        "alias": args.alias,
        "ports_stopped": stopped,
        "state_files_removed": files_removed,
        "failure_class": None,
    }
    human = (
        f"stopped forward(s) for {args.alias}: ports {stopped}\n"
        if stopped
        else f"stopped forward(s) for {args.alias} (no live ports)\n"
    )
    return json_result(success=True, exit_code=0, stdout=human, stderr="", data=data)


def _failure_envelope_stop(
    args: argparse.Namespace, fclass: str, *,
    message: str,
    ports: list[int] | None = None,
    files_removed: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
    ports_present: list[int] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "alias": args.alias,
        "ports_stopped": ports or [],
        "state_files_removed": files_removed or [],
        "failure_class": fclass,
    }
    if errors:
        data["errors"] = errors
    if ports_present is not None:
        data["ports_present"] = ports_present
    return json_result(
        success=False,
        exit_code=_EXIT_CODES[fclass],
        stderr=_hint_line(message),
        data=data,
    )


# --- status / list ---------------------------------------------------------


def _describe(entry: _StateEntry) -> dict[str, Any]:
    return {
        "alias": entry.alias,
        "pid": entry.pid,
        "local_port": entry.local_port,
        "remote_host": entry.remote_host,
        "remote_port": entry.remote_port,
        "bind_address": entry.bind_address,
        "started_at": entry.started_at,
        "running": _pid_alive(entry.pid),
        "state_file": str(entry.path),
    }


def _cmd_status(args: argparse.Namespace) -> int:
    forwards = [_describe(e) for e in _scan_state() if e.alias == args.alias]
    return _emit(_listing_envelope(forwards), args.json)


def _cmd_list(args: argparse.Namespace) -> int:
    forwards = [_describe(e) for e in _scan_state()]
    return _emit(_listing_envelope(forwards), args.json)


def _listing_envelope(forwards: list[dict[str, Any]]) -> dict[str, Any]:
    human_lines = []
    for f in forwards:
        state = "running" if f["running"] else "stale"
        human_lines.append(
            f"{f['alias']}:{f['local_port']} -> {f['remote_host']}:{f['remote_port']} "
            f"(pid {f['pid']}, {state})"
        )
    human = "\n".join(human_lines) + ("\n" if human_lines else "")
    return json_result(
        success=True,
        exit_code=0,
        stdout=human,
        stderr="",
        data={"forwards": forwards, "failure_class": None},
    )


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 — distinct cmd dispatch
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "start":
            return _cmd_start(args)
        if args.cmd == "stop":
            return _cmd_stop(args)
        if args.cmd == "status":
            return _cmd_status(args)
        if args.cmd == "list":
            return _cmd_list(args)
        return 1
    except _TunnelError as e:
        return _emit(
            json_result(
                success=False,
                exit_code=e.exit_code,
                stderr=_hint_line(e.message),
                data={"failure_class": e.failure_class},
            ),
            getattr(args, "json", False),
        )
    except Exception as e:  # noqa: BLE001 — outer catch-all per error-handling.md Pillar 1
        return _emit(
            json_result(
                success=False,
                exit_code=_EXIT_CODES[FAIL_REMOTE_ERROR],
                stderr=_hint_line(f"unexpected error: {e}"),
                data={"failure_class": FAIL_REMOTE_ERROR},
            ),
            getattr(args, "json", False),
        )


if __name__ == "__main__":
    sys.exit(main())
