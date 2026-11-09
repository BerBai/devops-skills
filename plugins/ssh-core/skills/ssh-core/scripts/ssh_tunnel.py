#!/usr/bin/env python3
"""ssh_tunnel.py — managed SSH port forwards: local, remote and SOCKS (v1.4.0).

Surface::

    ssh_tunnel.py start <alias>
        --type local|remote|dynamic        # default: local (SSH-13)
        --remote-port N                    # local / remote
        [--local-port N]                   # local binds here; pool 10000-20000
        [--remote-host HOST]               # local: forward target on dst side
        [--bind-address ADDR]              # local: 127.0.0.1
        [--idle-timeout N]                 # seconds of no connections (SSH-15)
        [--connect-timeout N]
        [--no-detach]
        [--ssh-config PATH] [--auth auto|key|password]
        [--accept-new-hostkey]
        [--audit-log|--no-audit] [-o KEY=VALUE] [--json]
    ssh_tunnel.py stop <alias> [--port N] [--type T] [--json]
    ssh_tunnel.py status <alias> [--json]
    ssh_tunnel.py list [--json]
    ssh_tunnel.py _watch <state-file> [--json]     # internal watchdog

What changed from v1.0
----------------------
* ``--type remote`` uses ``-R`` (bastion exposes a port back into your
  network) and ``--type dynamic`` uses ``-D`` (a SOCKS5 proxy), both under the
  same state-file semantics as ``-L`` (SSH-13). ``--reverse`` / ``--dynamic``
  are accepted as spelling sugar.
* ``--idle-timeout`` is real: ``start`` launches a small detached watchdog
  that polls the OS socket table for connections to the forwarded port and
  stops the tunnel after N idle seconds, recording ``idle_stopped_at`` in the
  state file (SSH-15). It used to write ``flag_deferred`` and do nothing.
* Every action is audited (``--audit-log`` / ``$SSH_CORE_AUDIT=on``), and the
  envelope carries the failure only (SSH-25).

Failure classes (shared table, see :mod:`lib.exit_codes`): ``posix_only`` 120,
``port_busy`` 121, ``port_pool_exhausted`` 122, ``ssh_connect_failed`` 123,
``auth`` 100, ``forward_setup_failed`` 125, ``tunnel_not_found`` 126,
``ambiguous_stop`` 127, ``stale_state_only`` 128, ``kill_failed`` 129,
``timeout`` 124.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    append_audit,
    audit_cli_flags,
    audit_enabled,
    can_detach,
    detach_kwargs,
    emit,
    is_alive,
    json_result,
    msys_safe_env,
    read_ssh_config_metadata,
    terminate,
    validate_ssh_option,
)
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AMBIGUOUS_STOP,
    EXIT_AUTH,
    EXIT_FORWARD_SETUP_FAILED,
    EXIT_INVALID_OPTION,
    EXIT_KILL_FAILED,
    EXIT_OK,
    EXIT_PASSWORD_REQUIRED,
    EXIT_POSIX_ONLY,
    EXIT_PORT_BUSY,
    EXIT_PORT_POOL_EXHAUSTED,
    EXIT_REMOTE_ERROR,
    EXIT_SSH_CONNECT_FAILED,
    EXIT_STALE_STATE_ONLY,
    EXIT_TIMEOUT,
    EXIT_TUNNEL_NOT_FOUND,
    exit_code_for,
)
from lib.redact import redactor_for  # noqa: E402

# Shared parent parser: `--json` and the audit switches are accepted before
# *and* after the verb (SSH-16 consistency), and SUPPRESS keeps the subparser
# default from clobbering an already-parsed top-level value.
_JSON_PARENT = argparse.ArgumentParser(add_help=False)
_JSON_PARENT.add_argument(
    "--json", action="store_true", default=argparse.SUPPRESS,
    help="Emit the JSON envelope",
)
_JSON_PARENT.add_argument(
    "--audit-log", dest="audit", action="store_true", default=argparse.SUPPRESS,
    help="Append a JSONL audit record under $TMPDIR/ssh-core/",
)
_JSON_PARENT.add_argument(
    "--no-audit", dest="audit", action="store_false", default=argparse.SUPPRESS,
    help="Do not write audit records",
)

# --- Constants --------------------------------------------------------------

PORT_POOL_MIN = 10000
PORT_POOL_MAX = 20000
PORT_POOL_RETRIES = 5
KILL_GRACE_S = 3
KILL_FINAL_S = 1
EARLY_FAILURE_GRACE_S = 1
#: How long a `-R` forward is given to fail loudly before it is called up.
REMOTE_SETTLE_S = 1.5
DEFAULT_CONNECT_TIMEOUT = 8
DEFAULT_IDLE_TIMEOUT = 1800
DEFAULT_REMOTE_HOST = "localhost"
DEFAULT_BIND_ADDRESS = "127.0.0.1"
WATCH_INTERVAL_S = 2.0
TUNNEL_TYPES = ("local", "remote", "dynamic")

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
FAIL_INVALID_OPTION = "invalid_option"
FAIL_PASSWORD_REQUIRED = "password_required"
FAIL_REMOTE_ERROR = "remote_error"

_EXIT_CODES: dict[str, int] = {
    FAIL_POSIX_ONLY: EXIT_POSIX_ONLY,
    FAIL_PORT_BUSY: EXIT_PORT_BUSY,
    FAIL_PORT_POOL_EXHAUSTED: EXIT_PORT_POOL_EXHAUSTED,
    FAIL_SSH_CONNECT_FAILED: EXIT_SSH_CONNECT_FAILED,
    FAIL_AUTH: EXIT_AUTH,
    FAIL_FORWARD_SETUP_FAILED: EXIT_FORWARD_SETUP_FAILED,
    FAIL_TUNNEL_NOT_FOUND: EXIT_TUNNEL_NOT_FOUND,
    FAIL_AMBIGUOUS_STOP: EXIT_AMBIGUOUS_STOP,
    FAIL_STALE_STATE_ONLY: EXIT_STALE_STATE_ONLY,
    FAIL_KILL_FAILED: EXIT_KILL_FAILED,
    FAIL_TIMEOUT: EXIT_TIMEOUT,
    FAIL_INVALID_OPTION: EXIT_INVALID_OPTION,
    FAIL_PASSWORD_REQUIRED: EXIT_PASSWORD_REQUIRED,
    FAIL_REMOTE_ERROR: EXIT_REMOTE_ERROR,
}


class _TunnelError(Exception):
    """Structured failure with class + exit code + message."""

    def __init__(self, failure_class: str, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.exit_code = exit_code
        self.message = message


# --- Parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="ssh_tunnel.py",
        parents=[_JSON_PARENT],
        description=(
            "Manage SSH port forwards (-L local, -R remote, -D SOCKS) as "
            "detached processes with a state file, an optional idle watchdog "
            "and audit records."
        ),
    )
    p.add_argument("--version", "-V", action="store_true", help="Print version")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", parents=[_JSON_PARENT], help="Start a new forward")
    s.add_argument("alias")
    s.add_argument(
        "--type", dest="tunnel_type", choices=TUNNEL_TYPES, default="local",
        help="local = -L (default), remote = -R, dynamic = -D (SOCKS5)",
    )
    s.add_argument("--reverse", dest="tunnel_type", action="store_const", const="remote",
                   help="Alias for --type remote")
    s.add_argument("--dynamic", dest="tunnel_type", action="store_const", const="dynamic",
                   help="Alias for --type dynamic")
    s.add_argument("--remote-port", type=int, default=None,
                   help="Port on the remote side (local/remote types)")
    s.add_argument("--local-port", type=int, default=None,
                   help=f"Local port (default: pick from {PORT_POOL_MIN}-{PORT_POOL_MAX})")
    s.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST,
                   help=(
                       f"Forward target (default: {DEFAULT_REMOTE_HOST}). For "
                       "--type remote this is resolved on THIS side, so pass "
                       "127.0.0.1 when the local service binds IPv4 only"
                   ))
    s.add_argument("--bind-address", default=None,
                   help=(
                       "Address to bind. Default: 127.0.0.1 for -L/-D, "
                       "localhost for -R"
                   ))
    s.add_argument(
        "--idle-timeout", type=int, default=None,
        help=(
            "Stop the tunnel after N seconds without a connection "
            f"(default: {DEFAULT_IDLE_TIMEOUT}; 0 disables the watchdog)"
        ),
    )
    s.add_argument("--connect-timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT)
    s.add_argument("--no-detach", action="store_true",
                   help="Debug: run ssh in the foreground; no state file")
    s.add_argument("-o", "--ssh-option", action="append", default=[], metavar="KEY=VALUE")
    s.add_argument("--auth", choices=("auto", "key", "password"), default="auto")
    s.add_argument("--password-env", default="SSH_PASSWORD")
    s.add_argument("--accept-new-hostkey", action="store_true")
    s.add_argument("--ssh-config", default=None)

    st = sub.add_parser("stop", parents=[_JSON_PARENT], help="Stop an existing forward")
    st.add_argument("alias")
    st.add_argument("--port", type=int, default=None)
    st.add_argument("--type", dest="tunnel_type", choices=TUNNEL_TYPES, default=None)

    sts = sub.add_parser(
        "status", parents=[_JSON_PARENT], help="Show forwards for this alias"
    )
    sts.add_argument("alias")

    ls = sub.add_parser(
        "list", parents=[_JSON_PARENT], help="Show all active forwards"
    )

    w = sub.add_parser("_watch", parents=[_JSON_PARENT], help=argparse.SUPPRESS)
    w.add_argument("state_file")
    w.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT)
    return p


# --- State file primitives --------------------------------------------------


@dataclass
class _StateEntry:
    """One ``$TMPDIR/ssh_tunnel/<digest>.json``."""

    pid: int
    local_port: int
    remote_host: str
    remote_port: int
    bind_address: str
    alias: str
    started_at: str
    tunnel_type: str = "local"
    idle_timeout: int = 0
    watchdog_pid: int | None = None
    last_activity: str | None = None
    idle_stopped_at: str | None = None
    path: Path = field(default_factory=Path)


def _state_dir() -> Path:
    base = Path(tempfile.gettempdir()) / "ssh_tunnel"
    base.mkdir(mode=0o700, exist_ok=True)
    return base


def _state_path(alias: str, port: int, tunnel_type: str = "local") -> Path:
    """State file keyed by (alias, type, *listener* port).

    For ``-L``/``-D`` the listener is the local port; for ``-R`` it is the port
    the far side listens on (``--remote-port``). Keying ``-R`` by the local
    service port instead made two tunnels that expose different services
    through the same local port overwrite each other's state file.
    """
    digest = hashlib.md5(f"{alias}-{tunnel_type}-{port}".encode()).hexdigest()
    return _state_dir() / f"{digest}.json"


def _listener_port(entry: "_StateEntry") -> int:
    return entry.remote_port if entry.tunnel_type == "remote" else entry.local_port


def _write_state(entry: _StateEntry) -> None:
    payload = {
        "pid": entry.pid,
        "local_port": entry.local_port,
        "remote_host": entry.remote_host,
        "remote_port": entry.remote_port,
        "bind_address": entry.bind_address,
        "alias": entry.alias,
        "started_at": entry.started_at,
        "type": entry.tunnel_type,
        "idle_timeout": entry.idle_timeout,
        "watchdog_pid": entry.watchdog_pid,
        "last_activity": entry.last_activity,
        "idle_stopped_at": entry.idle_stopped_at,
    }
    path = _state_path(
        entry.alias, _listener_port(entry), entry.tunnel_type
    )
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, path)
    entry.path = path


def _read_state(path: Path) -> _StateEntry | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return _StateEntry(
            pid=int(raw["pid"]),
            local_port=int(raw["local_port"]),
            remote_host=str(raw.get("remote_host") or ""),
            remote_port=int(raw.get("remote_port") or 0),
            bind_address=str(raw.get("bind_address") or ""),
            alias=str(raw["alias"]),
            started_at=str(raw.get("started_at") or ""),
            tunnel_type=str(raw.get("type") or "local"),
            idle_timeout=int(raw.get("idle_timeout") or 0),
            watchdog_pid=raw.get("watchdog_pid"),
            last_activity=raw.get("last_activity"),
            idle_stopped_at=raw.get("idle_stopped_at"),
            path=path,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _scan_state() -> list[_StateEntry]:
    base = _state_dir()
    out: list[_StateEntry] = []
    for p in sorted(base.glob("*.json")):
        entry = _read_state(p)
        if entry is not None:
            out.append(entry)
    return out


def _pid_alive(pid: int) -> bool:
    return is_alive(pid)


# --- Port allocator ---------------------------------------------------------


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _alloc_local_port(explicit: int | None, bind_address: str = "127.0.0.1") -> int:
    if explicit is not None:
        if not _port_free(explicit, bind_address if bind_address != "0.0.0.0" else "127.0.0.1"):
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
        f"no free port in pool {PORT_POOL_MIN}-{PORT_POOL_MAX} after "
        f"{PORT_POOL_RETRIES} attempts",
    )


# --- ssh argv ---------------------------------------------------------------


def _posix() -> bool:
    return can_detach()


def _forward_flag(
    tunnel_type: str, bind_address: str, local_port: int,
    remote_host: str, remote_port: int,
) -> list[str]:
    """Build the ``-L`` / ``-R`` / ``-D`` argument pair for this tunnel type.

    The port *names* follow OpenSSH's own grammar, and the two directions read
    opposite ways:

    * ``-L bind:local_port:remote_host:remote_port`` — listen here, dial there.
    * ``-R bind:remote_port:remote_host:local_port`` — listen *there* on
      ``remote_port``, dial ``remote_host:local_port`` from this side. So
      ``--remote-port`` is the listener on the far host and ``--local-port`` is
      the service being exposed. Swapping them (the first implementation) makes
      ssh try to bind the service port on the remote host, which either fails
      ("remote port forwarding failed for listen port 22") or, worse, steals it.
    * ``-D bind:local_port`` — SOCKS5 proxy.
    """
    if tunnel_type == "local":
        return ["-L", f"{bind_address}:{local_port}:{remote_host}:{remote_port}"]
    if tunnel_type == "remote":
        return ["-R", f"{bind_address}:{remote_port}:{remote_host}:{local_port}"]
    if tunnel_type == "dynamic":
        return ["-D", f"{bind_address}:{local_port}"]
    raise _TunnelError(
        FAIL_INVALID_OPTION, EXIT_INVALID_OPTION,
        f"unknown tunnel type {tunnel_type!r}; expected one of {TUNNEL_TYPES}",
    )


def _build_ssh_argv(
    alias: str,
    local_port: int,
    remote_host: str,
    remote_port: int,
    bind_address: str,
    connect_timeout: int,
    ssh_options: list[str] | None = None,
    *,
    tunnel_type: str = "local",
    password_auth: bool = False,
    accept_new_hostkey: bool = False,
) -> list[str]:
    argv = [
        "ssh",
        "-N",
        *_forward_flag(tunnel_type, bind_address, local_port, remote_host, remote_port),
        "-o", "ExitOnForwardFailure=yes",
    ]
    if password_auth:
        argv += [
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "UpdateHostKeys=no",
        ]
    else:
        argv += ["-o", "BatchMode=yes"]
    # FA-09: the flag used to be wired into the password branch only, so a
    # first-contact key-auth tunnel still failed with host_key(101) even though
    # the caller passed --accept-new-hostkey.
    if accept_new_hostkey:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    elif password_auth:
        argv += ["-o", "StrictHostKeyChecking=yes"]
    argv += [
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
    ]
    for option in ssh_options or []:
        argv += ["-o", option]
    argv += ["--", alias]
    return argv


from lib.askpass import askpass_env  # noqa: E402


def _spawn_ssh(
    argv: list[str], *, detach: bool, env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "env": env if env is not None else msys_safe_env(),
    }
    if detach:
        kwargs.update(detach_kwargs())
    return subprocess.Popen(argv, **kwargs)


def _wait_for_forward(
    proc: subprocess.Popen[bytes],
    connect_timeout: int,
    *,
    probe_host: str = "127.0.0.1",
    probe_port: int | None = None,
) -> tuple[bool, str]:
    """Wait until the forward is usable, ssh dies, or the budget expires.

    Polls the local port (for ``-L``/``-D``) so a working tunnel returns in a
    few hundred milliseconds instead of always burning ``connect_timeout + 1``
    seconds (B7 in the Windows report).

    ``-R`` has no local listener to probe, so it uses a short settle window
    instead: ``ExitOnForwardFailure=yes`` makes ssh exit within a fraction of a
    second when the remote side refuses the bind, which is the failure this
    needs to catch. Waiting the full ``connect_timeout`` there only made
    reverse tunnels feel slow (measured 9 s before this change).
    """
    if probe_port is None:
        deadline = time.monotonic() + REMOTE_SETTLE_S
    else:
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
        if probe_port is not None and _listening(probe_host, probe_port):
            _close_stderr_quietly(proc)
            return True, ""
        time.sleep(0.1)
    _close_stderr_quietly(proc)
    return True, ""


def _close_stderr_quietly(proc: subprocess.Popen[bytes]) -> None:
    if proc.stderr is not None:
        with contextlib.suppress(Exception):
            proc.stderr.close()


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _validate_options(options: list[str]) -> str | None:
    for option in options:
        reason = validate_ssh_option(option)
        if reason is not None:
            return reason
    return None


def _classify_start_failure(stderr: str) -> tuple[str, int]:
    from lib import AUTH_PATTERN, NETWORK_PATTERN

    if NETWORK_PATTERN.search(stderr):
        return FAIL_SSH_CONNECT_FAILED, _EXIT_CODES[FAIL_SSH_CONNECT_FAILED]
    if AUTH_PATTERN.search(stderr):
        return FAIL_AUTH, _EXIT_CODES[FAIL_AUTH]
    return FAIL_FORWARD_SETUP_FAILED, _EXIT_CODES[FAIL_FORWARD_SETUP_FAILED]


def _terminate_proc(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            proc.kill()


# --- kill semantics ---------------------------------------------------------


def _kill_with_grace(pid: int) -> str:
    if not _pid_alive(pid):
        return "stale"

    def _try_kill(*, force: bool = False) -> bool:
        try:
            return bool(terminate(pid, force=force))
        except (OSError, ProcessLookupError, PermissionError):
            return False

    if _try_kill() and not _pid_alive(pid):
        return "killed"
    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "killed"
        time.sleep(0.1)
    _try_kill(force=True)
    deadline = time.monotonic() + KILL_FINAL_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return "killed"
        time.sleep(0.1)
    return "kill_failed"


# --- envelope helpers -------------------------------------------------------


def _emit(envelope: dict[str, Any], as_json: bool, redactor: Any = None) -> int:
    emit(envelope, as_json=as_json, redactor=redactor)
    return int(envelope["exit_code"])


def _hint_line(text: str) -> str:
    if not text:
        return ""
    return text if text.endswith("\n") else text + "\n"


# --- auth resolution --------------------------------------------------------


def _resolve_auth(args: argparse.Namespace) -> str:
    requested = getattr(args, "auth", "auto")
    if requested != "auto":
        return requested
    meta = read_ssh_config_metadata(
        getattr(args, "alias", ""), getattr(args, "ssh_config", None)
    )
    if meta.get("auth", "").lower() == "password":
        return "password"
    if meta.get("device-type", "").lower() == "network":
        return "password"
    return "key"


def _resolve_password(args: argparse.Namespace, auth: str) -> str | None:
    if auth != "password":
        return None
    return os.environ.get(getattr(args, "password_env", "SSH_PASSWORD"))


# --- start ------------------------------------------------------------------


def _cmd_start(args: argparse.Namespace) -> int:  # noqa: PLR0911, PLR0912, PLR0915
    tunnel_type = getattr(args, "tunnel_type", "local") or "local"
    if tunnel_type in ("local", "dynamic") and args.remote_port is None:
        if tunnel_type == "local":
            return _emit(
                _failure_envelope_start(
                    args, _TunnelError(
                        FAIL_INVALID_OPTION, EXIT_INVALID_OPTION,
                        "--remote-port is required for --type local (add "
                        "--type dynamic for a SOCKS proxy, which needs none)",
                    ),
                    elapsed=0.0, tunnel_type=tunnel_type,
                ),
                args.json,
            )
    if tunnel_type == "remote":
        # -R <bind>:<remote_listen>:<target_host>:<target_port>
        if args.remote_port is None:
            return _emit(
                _failure_envelope_start(
                    args, _TunnelError(
                        FAIL_INVALID_OPTION, EXIT_INVALID_OPTION,
                        "--type remote needs --remote-port: the port the remote "
                        "side listens on",
                    ),
                    elapsed=0.0, tunnel_type=tunnel_type,
                ),
                args.json,
            )

    bind_address = args.bind_address or (
        "localhost" if tunnel_type == "remote" else DEFAULT_BIND_ADDRESS
    )
    args.bind_address = bind_address
    remote_port = args.remote_port or 0

    local_port = args.local_port if args.local_port is not None else 22
    if args.no_detach:
        return _cmd_start_no_detach(args, tunnel_type)
    if not _posix():
        return _emit(
            json_result(
                success=False,
                exit_code=_EXIT_CODES[FAIL_POSIX_ONLY],
                stderr=_hint_line(
                    "ssh_tunnel start could not detach on this platform; run it "
                    "under a shell that supports background processes"
                ),
                data={"alias": args.alias, "failure_class": FAIL_POSIX_ONLY},
            ),
            args.json,
        )

    start_time = time.monotonic()
    try:
        if tunnel_type == "remote":
            # -R does not bind anything locally: --local-port names the service
            # being exposed, which is expected to be *in use* on this host. The
            # only local requirement is that --remote-port is free to be
            # allocated when omitted (it is on the far side, so we cannot probe
            # it here either).
            local_port = args.local_port if args.local_port is not None else random.randint(
                PORT_POOL_MIN, PORT_POOL_MAX
            )
        else:
            local_port = _alloc_local_port(args.local_port, bind_address)
    except _TunnelError as e:
        return _emit(
            _failure_envelope_start(args, e, elapsed=time.monotonic() - start_time,
                                    tunnel_type=tunnel_type),
            args.json,
        )

    option_refusal = _validate_options(args.ssh_option)
    if option_refusal is not None:
        err = _TunnelError(FAIL_INVALID_OPTION, EXIT_INVALID_OPTION, option_refusal)
        return _emit(
            _failure_envelope_start(args, err, elapsed=time.monotonic() - start_time,
                                    tunnel_type=tunnel_type),
            args.json,
        )

    auth = _resolve_auth(args)
    password = _resolve_password(args, auth)
    if auth == "password" and password is None:
        err = _TunnelError(
            FAIL_PASSWORD_REQUIRED, _EXIT_CODES[FAIL_PASSWORD_REQUIRED],
            f"--auth password needs ${args.password_env} exported "
            "(never pass passwords on the command line)",
        )
        return _emit(
            _failure_envelope_start(args, err, elapsed=time.monotonic() - start_time,
                                    tunnel_type=tunnel_type),
            args.json,
        )

    argv = _build_ssh_argv(
        args.alias, local_port, args.remote_host, remote_port,
        bind_address, args.connect_timeout, args.ssh_option,
        tunnel_type=tunnel_type,
        password_auth=(auth == "password"),
        accept_new_hostkey=args.accept_new_hostkey,
    )

    probe_port = local_port if tunnel_type in ("local", "dynamic") else None
    try:
        if password is not None:
            with askpass_env(password) as env:
                proc = _spawn_ssh(argv, detach=True, env=env)
                alive, stderr_text = _wait_for_forward(
                    proc, args.connect_timeout, probe_port=probe_port
                )
        else:
            proc = _spawn_ssh(argv, detach=True)
            alive, stderr_text = _wait_for_forward(
                proc, args.connect_timeout, probe_port=probe_port
            )
    except OSError as e:
        err = _TunnelError(
            FAIL_REMOTE_ERROR, _EXIT_CODES[FAIL_REMOTE_ERROR],
            f"failed to spawn ssh: {e}",
        )
        return _emit(
            _failure_envelope_start(args, err, elapsed=time.monotonic() - start_time,
                                    tunnel_type=tunnel_type),
            args.json,
        )

    if not alive:
        fclass, exit_code = _classify_start_failure(stderr_text)
        err = _TunnelError(fclass, exit_code, stderr_text.strip() or "ssh exited early")
        with contextlib.suppress(Exception):
            proc.wait(timeout=1)
        return _emit(
            _failure_envelope_start(
                args, err, elapsed=time.monotonic() - start_time,
                raw_stderr=stderr_text, tunnel_type=tunnel_type,
            ),
            args.json,
        )

    idle_timeout = args.idle_timeout if args.idle_timeout is not None else DEFAULT_IDLE_TIMEOUT
    entry = _StateEntry(
        pid=proc.pid,
        local_port=local_port,
        remote_host=args.remote_host,
        remote_port=remote_port,
        bind_address=bind_address,
        alias=args.alias,
        started_at=datetime.now(timezone.utc).isoformat(),
        tunnel_type=tunnel_type,
        idle_timeout=max(0, idle_timeout),
        last_activity=datetime.now(timezone.utc).isoformat(),
    )
    try:
        _write_state(entry)
    except OSError as e:
        _terminate_proc(proc)
        err = _TunnelError(
            FAIL_REMOTE_ERROR, _EXIT_CODES[FAIL_REMOTE_ERROR],
            f"failed to write state file: {e}",
        )
        return _emit(
            _failure_envelope_start(args, err, elapsed=time.monotonic() - start_time,
                                    tunnel_type=tunnel_type),
            args.json,
        )

    watchdog_started = False
    if entry.idle_timeout > 0:
        watchdog_started = _start_watchdog(entry)
        if watchdog_started:
            try:
                _write_state(entry)
            except OSError:
                watchdog_started = False

    elapsed = time.monotonic() - start_time
    envelope = _success_envelope_start(
        args, entry, elapsed=elapsed, watchdog_started=watchdog_started,
    )
    _audit(args, "start", envelope)
    return _emit(envelope, args.json, redactor_for(args.alias, args.ssh_config))


def _start_watchdog(entry: _StateEntry) -> bool:
    """Spawn the detached idle watchdog for ``entry`` (SSH-15)."""
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_watch",
        str(entry.path),
        "--idle-timeout",
        str(entry.idle_timeout),
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=msys_safe_env(),
            **detach_kwargs(),
        )
    except OSError:
        return False
    entry.watchdog_pid = proc.pid
    return True


def _success_envelope_start(
    args: argparse.Namespace, entry: _StateEntry, *,
    elapsed: float, watchdog_started: bool,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "alias": entry.alias,
        "type": entry.tunnel_type,
        "pid": entry.pid,
        "local_port": entry.local_port,
        "remote_host": entry.remote_host,
        "remote_port": entry.remote_port,
        "bind_address": entry.bind_address,
        "state_file": str(entry.path),
        "started_at": entry.started_at,
        "duration_ms": int(round(elapsed * 1000)),
        "failure_class": None,
        "idle_timeout": entry.idle_timeout,
        "idle_watchdog": watchdog_started,
    }
    if entry.tunnel_type == "dynamic":
        human = (
            f"SOCKS proxy up: {entry.bind_address}:{entry.local_port} via "
            f"{entry.alias} (pid {entry.pid})\n"
        )
    elif entry.tunnel_type == "remote":
        human = (
            f"remote forward up: {entry.alias} listens on "
            f"{entry.bind_address}:{entry.remote_port} -> "
            f"{entry.remote_host}:{entry.local_port} here (pid {entry.pid})\n"
        )
    else:
        human = (
            f"forward up: {entry.alias}:{entry.local_port} -> "
            f"{entry.remote_host}:{entry.remote_port} (pid {entry.pid})\n"
        )
    stderr_text = ""
    if entry.idle_timeout > 0 and not watchdog_started:
        stderr_text = _hint_line(
            "idle watchdog could not be started; the tunnel will run until `stop`"
        )
    return json_result(
        success=True, exit_code=0, stdout=human, stderr=stderr_text, data=data,
    )


def _failure_envelope_start(
    args: argparse.Namespace, err: _TunnelError, *,
    elapsed: float = 0.0, raw_stderr: str = "", tunnel_type: str = "local",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "alias": args.alias,
        "type": tunnel_type,
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


def _cmd_start_no_detach(args: argparse.Namespace, tunnel_type: str) -> int:
    try:
        local_port = _alloc_local_port(args.local_port, args.bind_address)
    except _TunnelError as e:
        return _emit(
            _failure_envelope_start(args, e, elapsed=0.0, tunnel_type=tunnel_type),
            args.json,
        )
    option_refusal = _validate_options(args.ssh_option)
    if option_refusal is not None:
        err = _TunnelError(FAIL_INVALID_OPTION, EXIT_INVALID_OPTION, option_refusal)
        return _emit(
            _failure_envelope_start(args, err, elapsed=0.0, tunnel_type=tunnel_type),
            args.json,
        )
    argv = _build_ssh_argv(
        args.alias, local_port, args.remote_host, args.remote_port or 0,
        args.bind_address, args.connect_timeout, args.ssh_option,
        tunnel_type=tunnel_type,
    )
    if args.json:
        emit(
            json_result(
                success=True,
                exit_code=0,
                stdout="",
                stderr="",
                data={
                    "alias": args.alias,
                    "type": tunnel_type,
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
        proc = subprocess.Popen(argv, env=msys_safe_env())
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


# --- idle watchdog ----------------------------------------------------------


def _connections_on_port(port: int) -> int:
    """Count ESTABLISHED sockets touching ``port`` (best effort).

    Uses ``netstat`` on Windows and ``ss``/``netstat`` on POSIX; a host
    without any of them reports 0, which means "idle" — the watchdog then
    behaves like a plain timer, still better than the old no-op.
    """
    if _connections_windows(port) is not None:
        return _connections_windows(port) or 0
    for argv in (["ss", "-Htan"], ["netstat", "-an"]):
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        count = 0
        needle = f":{port}"
        for line in proc.stdout.splitlines():
            if needle not in line:
                continue
            if "ESTAB" in line or "ESTABLISHED" in line:
                count += 1
        return count
    return 0


def _connections_windows(port: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5, check=False,
            **{"encoding": "utf-8", "errors": "replace"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    count = 0
    needle = f":{port}"
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local, remote, state = parts[1], parts[2], parts[3]
        if state.upper() != "ESTABLISHED":
            continue
        if local.endswith(needle) or remote.endswith(needle):
            count += 1
    return count


def _remote_connections(entry: _StateEntry) -> int:
    """Count established sockets for a ``-R`` listener on the far host."""
    command = (
        f"(ss -Htan 2>/dev/null || netstat -an 2>/dev/null) | "
        f"grep -c ':{entry.remote_port} .*ESTAB' || true"
    )
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--",
        entry.alias, command,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=False,
            env=msys_safe_env(),
            **{"encoding": "utf-8", "errors": "replace"},
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    with contextlib.suppress(ValueError):
        return int((proc.stdout or "0").strip().splitlines()[-1] or 0)
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Detached idle monitor: stop the tunnel after ``--idle-timeout`` seconds."""
    state_file = Path(args.state_file)
    entry = _read_state(state_file)
    if entry is None:
        return EXIT_OK
    if args.idle_timeout <= 0:
        # FA-27: `--idle-timeout 0` means "no watchdog". Clamping it to 1 made a
        # hand-started `_watch` kill a perfectly healthy tunnel one second in;
        # `start` never spawns the watchdog for 0, so exiting here is the only
        # behaviour consistent with the documented flag.
        return EXIT_OK
    idle_timeout = max(1, args.idle_timeout)
    last_seen = time.monotonic()
    while True:
        time.sleep(WATCH_INTERVAL_S)
        if not _pid_alive(entry.pid):
            return EXIT_OK
        active = _connections_on_port(entry.local_port)
        if entry.tunnel_type == "remote":
            # A -R listener lives on the far host, so only the far host can
            # count its connections. Ask it over a short-lived ssh instead of
            # pretending the local socket table saw anything.
            active = _remote_connections(entry)
        if active > 0:
            last_seen = time.monotonic()
            entry.last_activity = datetime.now(timezone.utc).isoformat()
            with contextlib.suppress(OSError):
                _write_state(entry)
            continue
        if time.monotonic() - last_seen >= idle_timeout:
            outcome = _kill_with_grace(entry.pid)
            entry.idle_stopped_at = datetime.now(timezone.utc).isoformat()
            entry.last_activity = entry.last_activity or entry.started_at
            with contextlib.suppress(OSError):
                _write_state(entry)
            with contextlib.suppress(OSError):
                state_file.unlink()
            if audit_enabled(None, default=False):
                append_audit(
                    entry.alias,
                    {
                        "host": entry.alias,
                        "route": "tunnel",
                        "action": "idle_stop",
                        "type": entry.tunnel_type,
                        "local_port": entry.local_port,
                        "remote_port": entry.remote_port,
                        "idle_timeout": idle_timeout,
                        "outcome": outcome,
                        "success": outcome == "killed",
                    },
                )
            return EXIT_OK


# --- stop -------------------------------------------------------------------


def _cmd_stop(args: argparse.Namespace) -> int:
    entries = [e for e in _scan_state() if e.alias == args.alias]
    if getattr(args, "tunnel_type", None):
        entries = [e for e in entries if e.tunnel_type == args.tunnel_type]
    if args.port is not None:
        entries = [e for e in entries if _listener_port(e) == args.port]
    if not entries:
        return _emit(
            _failure_envelope_stop(
                args, FAIL_TUNNEL_NOT_FOUND,
                message=(
                    f"no forward found for alias {args.alias!r}"
                    + (f" on port {args.port}" if args.port is not None else "")
                ),
            ),
            args.json,
        )
    if args.port is None and len(entries) > 1:
        ports = [_listener_port(e) for e in entries]
        return _emit(
            _failure_envelope_stop(
                args, FAIL_AMBIGUOUS_STOP,
                message=(
                    f"multiple forwards for alias {args.alias!r} on ports {ports}; "
                    "pass --port (and --type) to disambiguate"
                ),
                ports_present=ports,
            ),
            args.json,
        )

    stopped: list[int] = []
    files_removed: list[str] = []
    errors: list[dict[str, Any]] = []
    for entry in entries:
        if entry.watchdog_pid:
            with contextlib.suppress(Exception):
                terminate(int(entry.watchdog_pid))
        outcome = _kill_with_grace(entry.pid)
        with contextlib.suppress(FileNotFoundError):
            entry.path.unlink()
            files_removed.append(str(entry.path))
        if outcome == "killed":
            stopped.append(_listener_port(entry))
        elif outcome == "stale":
            errors.append(
                {
                    "failure_class": FAIL_STALE_STATE_ONLY,
                    "exit_code": _EXIT_CODES[FAIL_STALE_STATE_ONLY],
                    "local_port": entry.local_port,
                }
            )
        else:
            errors.append(
                {
                    "failure_class": FAIL_KILL_FAILED,
                    "exit_code": _EXIT_CODES[FAIL_KILL_FAILED],
                    "local_port": entry.local_port,
                }
            )
    if errors:
        worst = next(
            (e for e in errors if e["failure_class"] == FAIL_KILL_FAILED), errors[0]
        )
        envelope = _failure_envelope_stop(
            args, str(worst["failure_class"]),
            message=(
                f"could not fully stop forward on port {worst['local_port']}: "
                f"{worst['failure_class']}"
            ),
            ports=stopped, files_removed=files_removed, errors=errors,
        )
        _audit(args, "stop", envelope)
        return _emit(envelope, args.json)
    envelope = _success_envelope_stop(args, stopped, files_removed)
    _audit(args, "stop", envelope)
    return _emit(envelope, args.json)


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
        "type": entry.tunnel_type,
        "pid": entry.pid,
        "local_port": entry.local_port,
        "remote_host": entry.remote_host,
        "remote_port": entry.remote_port,
        "bind_address": entry.bind_address,
        "started_at": entry.started_at,
        "running": _pid_alive(entry.pid),
        "idle_timeout": entry.idle_timeout,
        "watchdog_pid": entry.watchdog_pid,
        "watchdog_running": bool(entry.watchdog_pid and _pid_alive(int(entry.watchdog_pid))),
        "last_activity": entry.last_activity,
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
        arrow = {
            "local": f"{f['alias']}:{f['local_port']} -> "
                     f"{f['remote_host']}:{f['remote_port']}",
            "remote": f"{f['alias']}:{f['bind_address']}:{f['local_port']} -> "
                      f"{f['remote_host']}:{f['remote_port']}",
            "dynamic": f"SOCKS {f['bind_address']}:{f['local_port']} via {f['alias']}",
        }.get(f["type"], f"{f['alias']}:{f['local_port']}")
        human_lines.append(f"{arrow} (pid {f['pid']}, {state})")
    human = "\n".join(human_lines) + ("\n" if human_lines else "")
    return json_result(
        success=True, exit_code=0, stdout=human, stderr="",
        data={"forwards": forwards, "failure_class": None},
    )


# --- audit ------------------------------------------------------------------


def _audit(args: argparse.Namespace, action: str, result: dict) -> None:
    if not audit_enabled(getattr(args, "audit", None), default=False):
        return
    data = result.get("data") or {}
    append_audit(
        getattr(args, "alias", "tunnel"),
        {
            "host": getattr(args, "alias", ""),
            "route": "tunnel",
            "action": action,
            "type": data.get("type") or getattr(args, "tunnel_type", None),
            "local_port": data.get("local_port") or data.get("ports_stopped"),
            "remote_port": data.get("remote_port"),
            "success": result.get("success"),
            "exit_code": result.get("exit_code"),
            "failure_class": data.get("failure_class"),
        },
    )


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_tunnel.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    # `--json` may appear before or after the verb; the per-subparser flag has
    # its own default, so normalise from argv as the single source of truth.
    args.json = bool(getattr(args, "json", False)) or "--json" in argv
    redactor = redactor_for(getattr(args, "alias", "tunnel"),
                            getattr(args, "ssh_config", None))
    try:
        if args.cmd == "start":
            return _cmd_start(args)
        if args.cmd == "stop":
            return _cmd_stop(args)
        if args.cmd == "status":
            return _cmd_status(args)
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "_watch":
            return _cmd_watch(args)
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
            redactor,
        )
    except Exception as e:  # noqa: BLE001 — outer catch-all per contract
        return _emit(
            json_result(
                success=False,
                exit_code=_EXIT_CODES[FAIL_REMOTE_ERROR],
                stderr=_hint_line(f"unexpected error: {e}"),
                data={"failure_class": FAIL_REMOTE_ERROR},
            ),
            getattr(args, "json", False),
            redactor,
        )


if __name__ == "__main__":
    sys.exit(guarded_main("ssh_tunnel.py", main))
