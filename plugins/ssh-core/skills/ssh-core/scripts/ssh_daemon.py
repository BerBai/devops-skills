#!/usr/bin/env python3
"""ssh_daemon.py — one long-lived SSH session per alias, shared by callers.

v0.2 shipped this file as ``unimplemented()``; v1.1 implements it (SSH-16) with
the design that actually works on every platform, including Windows where
OpenSSH's ``ControlMaster`` needs a POSIX socket and therefore cannot be used.

How it works
------------
``start`` spawns a detached ``ssh_daemon.py _serve`` process. The server opens
**one** ``ssh -T <alias> sh`` child and keeps it alive, then answers requests
on a loopback TCP port. Each request runs one command inside that single
session using unique begin/exit markers, so 200 calls cost one handshake
instead of 200.

Request/response protocol (loopback only, token-authenticated)
--------------------------------------------------------------
4-byte big-endian length prefix + UTF-8 JSON body::

    request : {"token": "...", "command": "...", "timeout": 30}
    response: {"exit_code": 0, "stdout": "...", "stderr": "...", "reused": true}

The token lives in the 0600 state file (``$TMPDIR/ssh_daemon/<md5>.json``)
together with the pid, port, start time and last-use time. Only the owning
user can read it, so another local account cannot drive the session.

Subcommands::

    ssh_daemon.py start <alias> [--idle-timeout N] [--no-detach]
        [--connect-timeout N] [--auth auto|key|password]
        [--password-env VAR] [--accept-new-hostkey] [--ssh-config PATH]
        [--json]
    ssh_daemon.py run <alias> "<command>" [--timeout N] [--json]
    ssh_daemon.py status <alias> [--json]
    ssh_daemon.py stop <alias> [--json]
    ssh_daemon.py list [--json]

``--json`` may appear before *or* after the subcommand, like every other
script in this skill.

Exit codes: 0 ok, 2 argv, 3 dependency, 100 auth, 101 host key, 102 network,
107 invalid `-o` option, 124 timeout, 200 daemon not running,
202 protocol error (see :mod:`lib.exit_codes`).

``--idle-timeout`` has a 30 s floor: a smaller value is clamped up, because a
daemon that dies between ``start`` and the first ``run`` is never what a caller
wants. The effective value is reported in ``data.idle_timeout`` (FA-28).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    MAX_STREAM_BYTES,
    append_audit,
    audit_cli_flags,
    audit_enabled,
    classify_failure,
    detach_kwargs,
    emit,
    is_alive,
    json_result,
    merged_ssh_config,
    msys_safe_env,
    read_ssh_config_metadata,
    terminate,
    validate_ssh_option,
)
from lib.askpass import askpass_env  # noqa: E402
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AUTH,
    EXIT_DAEMON_NOT_RUNNING,
    EXIT_DAEMON_PROTOCOL,
    EXIT_DEPENDENCY_MISSING,
    EXIT_HOST_KEY,
    EXIT_INTERNAL,
    EXIT_INVALID_ARGUMENTS,
    EXIT_INVALID_OPTION,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_TIMEOUT,
    exit_code_for,
)
from lib.redact import redactor_for  # noqa: E402

STATE_DIR_NAME = "ssh_daemon"
DEFAULT_IDLE_TIMEOUT = 1800
#: A daemon that exits before the caller can use it is a bug, not a feature, so
#: ``--idle-timeout`` is clamped to this floor (FA-28: undocumented until now).
MIN_IDLE_TIMEOUT = 30
CONNECT_GRACE = 20
MARK_BEGIN = "__SSHD_BEGIN__"
MARK_EXIT = "__SSHD_EXIT__"
#: stderr is captured through a separate marker pair + a remote temp file, so
#: `echo E >&2` lands in ``stderr`` and not in ``stdout`` (FA-13).
MARK_ERR = "__SSHD_ERR__"
MARK_ERR_END = "__SSHD_ERREND__"
RECV_CHUNK = 65536


# --- state file -------------------------------------------------------------


def _state_dir() -> Path:
    base = Path(tempfile.gettempdir()) / STATE_DIR_NAME
    base.mkdir(mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        base.chmod(0o700)
    return base


def _state_path(alias: str) -> Path:
    digest = hashlib.md5(alias.encode("utf-8")).hexdigest()
    return _state_dir() / f"{digest}.json"


def _read_state(alias: str) -> dict[str, Any] | None:
    path = _state_path(alias)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or "port" not in raw:
        return None
    raw["_path"] = str(path)
    return raw


def _write_state(alias: str, payload: dict[str, Any]) -> Path:
    path = _state_path(alias)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, path)
    return path


def _clear_state(alias: str) -> None:
    with contextlib.suppress(OSError):
        _state_path(alias).unlink()


def _probe(port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


# --- wire protocol ----------------------------------------------------------


def _send(sock: socket.socket, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        block = sock.recv(count - len(chunks))
        if not block:
            raise ConnectionError("daemon closed the connection")
        chunks.extend(block)
    return bytes(chunks)


def _recv(sock: socket.socket) -> dict[str, Any]:
    header = _recv_exact(sock, 4)
    (length,) = struct.unpack(">I", header)
    return json.loads(_recv_exact(sock, length).decode("utf-8"))


# --- the server side --------------------------------------------------------


class _RemoteShell:
    """One long-lived ``ssh -T <alias> sh`` session driven by markers."""

    def __init__(self, argv: list[str], password: str | None) -> None:
        self.argv = argv
        self.password = password
        self.proc: subprocess.Popen[bytes] | None = None
        # FA-18: a bounded byte buffer. The daemon used to grow a bare
        # ``bytearray`` forever, so one chatty command could keep every byte of
        # its output alive until the session ended.
        self._buf = bytearray()
        self.bytes_elided = 0
        self._lock = threading.Lock()
        self._askpass: Any = None

    def start(self) -> None:
        env = msys_safe_env()
        if self.password:
            # A detached-ish daemon has no terminal, so ssh would abort at the
            # password prompt. The 0600 SSH_ASKPASS helper answers it exactly
            # like the exec and tunnel routes do (SSHCORE-001 follow-up); the
            # secret is deleted when the shell is closed.
            self._askpass = askpass_env(self.password)
            env = self._askpass.__enter__()
        self.proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            bufsize=0,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            block = self.proc.stdout.read(RECV_CHUNK)
            if not block:
                return
            with self._lock:
                self._buf.extend(block)
                if len(self._buf) > MAX_STREAM_BYTES:
                    excess = len(self._buf) - MAX_STREAM_BYTES
                    del self._buf[:excess]
                    self.bytes_elided += excess

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def run(self, command: str, timeout: float) -> dict[str, Any]:
        """Run one command in the shared shell and split stdout from stderr.

        Three things are load-bearing here (FA-13/FA-14):

        * the command runs in a **subshell** — ``( … )`` — so a stray ``exit``
          ends the subshell and the caller still sees its status, instead of
          tearing down the session;
        * stderr is redirected to a remote temp file and echoed between its own
          markers, so ``echo E >&2`` no longer disappears (it used to be merged
          into stdout and then discarded);
        * ``</dev/null`` is applied to the command, so ``cat``/``read``/``ssh``
          get EOF instead of eating the rest of the protocol stream.
        """
        if not self.alive():
            raise ConnectionError("ssh session is gone")
        assert self.proc is not None and self.proc.stdin is not None
        marker_id = secrets.token_hex(4)
        begin = f"{MARK_BEGIN}{marker_id}"
        end = f"{MARK_EXIT}{marker_id}"
        err_begin = f"{MARK_ERR}{marker_id}"
        err_end = f"{MARK_ERR_END}{marker_id}"
        script = (
            f"printf '\\n{begin}\\n'\n"
            f"_sshd_err=$(mktemp 2>/dev/null || echo /tmp/.sshd_err.$$)\n"
            f"( {command}\n) < /dev/null 2> \"$_sshd_err\"\n"
            f"_sshd_rc=$?\n"
            f"printf '\\n{err_begin}\\n'\n"
            f"cat \"$_sshd_err\" 2>/dev/null\n"
            f"printf '\\n{err_end}\\n'\n"
            f"rm -f \"$_sshd_err\" 2>/dev/null\n"
            f"printf '\\n{end}=%s\\n' \"$_sshd_rc\"\n"
        )
        with self._lock:
            self._buf.clear()
            self.bytes_elided = 0
        self.proc.stdin.write(script.encode("utf-8"))
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        pattern_end = f"{end}=".encode()
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ConnectionError(
                    "the command ended the daemon's ssh session; the daemon "
                    "exited (check that the remote shell is still usable)"
                )
            with self._lock:
                data = bytes(self._buf)
            start = data.find(begin.encode())
            finish = data.find(pattern_end, start + 1 if start >= 0 else 0)
            if start >= 0 and finish >= 0:
                line_end = data.find(b"\n", finish)
                if line_end > 0:
                    body = data[start + len(begin) + 1:finish]
                    code_text = data[finish + len(pattern_end):line_end].strip()
                    try:
                        code = int(code_text.decode() or "0")
                    except ValueError:
                        code = 0
                    out_bytes, err_bytes = _split_streams(
                        body, err_begin.encode(), err_end.encode()
                    )
                    response: dict[str, Any] = {
                        "exit_code": code,
                        "stdout": out_bytes.decode("utf-8", "replace").rstrip("\n"),
                        "stderr": err_bytes.decode("utf-8", "replace").rstrip("\n"),
                    }
                    if self.bytes_elided:
                        # FA-18: same `truncated` / `bytes_elided` contract the
                        # exec route has, now that this buffer is bounded too.
                        response["truncated"] = True
                        response["bytes_elided"] = self.bytes_elided
                    return response
            time.sleep(0.02)
        return {
            "exit_code": EXIT_TIMEOUT,
            "stdout": "",
            "stderr": f"daemon: no result within {timeout:.0f}s",
        }

    def close(self) -> None:
        if self.proc is None:
            self._close_askpass()
            return
        with contextlib.suppress(Exception):
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        with contextlib.suppress(Exception):
            self.proc.terminate()
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=3)
        self._close_askpass()

    def _close_askpass(self) -> None:
        if self._askpass is not None:
            with contextlib.suppress(Exception):
                self._askpass.__exit__(None, None, None)
            self._askpass = None


def _split_streams(
    body: bytes, err_begin: bytes, err_end: bytes
) -> tuple[bytes, bytes]:
    """Split a marked transcript into ``(stdout, stderr)`` bytes."""
    start = body.find(err_begin)
    if start < 0:
        return body, b""
    finish = body.find(err_end, start + len(err_begin))
    stdout = body[:start]
    stderr = body[start + len(err_begin):] if finish < 0 else body[
        start + len(err_begin):finish
    ]
    return stdout, stderr.lstrip(b"\n")
def _serve(args: argparse.Namespace) -> int:
    """Run the daemon in the foreground until idle/EOF (``_serve`` verb)."""
    alias = args.alias
    auth = _resolve_auth(alias, args.auth, args.ssh_config)
    password = os.environ.get(args.password_env) if auth == "password" else None
    if auth == "password" and not password:
        return EXIT_INVALID_ARGUMENTS
    # FA-02: the daemon used to import validate_ssh_option and never call it, so
    # `ssh_daemon.py start -o ProxyCommand=...` really executed a local command.
    # The check runs here (the process that spawns ssh) *and* in start() so a
    # direct `_serve` invocation cannot skip it either.
    refusal = _validate_options(args.ssh_option)
    if refusal is not None:
        sys.stderr.write(f"ssh_daemon: {refusal}\n")
        return EXIT_INVALID_OPTION

    argv = _ssh_argv_for_shell(
        alias, args.connect_timeout, args.ssh_config,
        args.ssh_option, args.accept_new_hostkey,
        password_auth=(auth == "password"),
    )
    shell = _RemoteShell(argv, password)

    try:
        shell.start()
    except OSError as e:
        sys.stderr.write(f"ssh_daemon: cannot spawn ssh: {e}\n")
        return EXIT_DEPENDENCY_MISSING

    # Wait for the session to accept input: a failed login closes it at once.
    deadline = time.monotonic() + CONNECT_GRACE
    while time.monotonic() < deadline:
        if not shell.alive():
            return _classify_connect_failure(shell)
        with contextlib.suppress(Exception):
            probe = shell.run("true", timeout=5)
            if probe["exit_code"] == 0:
                break
        time.sleep(0.2)
    else:
        shell.close()
        return EXIT_NETWORK

    token = secrets.token_hex(24)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    idle_timeout = max(MIN_IDLE_TIMEOUT, int(args.idle_timeout))
    state = {
        "alias": alias,
        "pid": os.getpid(),
        "port": port,
        "token": token,
        "started_at": time.time(),
        "last_used_at": time.time(),
        "idle_timeout": idle_timeout,
        "idle_timeout_requested": int(args.idle_timeout),
        "auth": auth,
    }
    _write_state(alias, state)
    server.settimeout(1.0)
    idle_deadline = time.monotonic() + idle_timeout
    try:
        while True:
            if time.monotonic() > idle_deadline:
                break
            try:
                conn, _ = server.accept()
            except socket.timeout:
                if not shell.alive():
                    break
                continue
            except OSError:
                break
            with conn:
                try:
                    request = _recv(conn)
                except (ConnectionError, json.JSONDecodeError, OSError):
                    continue
                if not secrets.compare_digest(str(request.get("token", "")), token):
                    _send(conn, {"exit_code": EXIT_PERMISSION, "stdout": "",
                                 "stderr": "daemon: bad token"})
                    continue
                command = str(request.get("command", ""))
                timeout = float(request.get("timeout", 120))
                try:
                    response = shell.run(command, timeout)
                except ConnectionError as e:
                    # FA-14: say *why* the session is gone and stop pretending
                    # the next call will work. The caller sees
                    # `daemon_not_running`, not a 5 s timeout + protocol error.
                    _send(conn, {
                        "exit_code": EXIT_DAEMON_NOT_RUNNING,
                        "stdout": "",
                        "stderr": f"daemon: {e}; run `stop` then `start` again",
                        "failure_class": "daemon_not_running",
                    })
                    break
                response["reused"] = True
                state["last_used_at"] = time.time()
                with contextlib.suppress(OSError):
                    _write_state(alias, state)
                _send(conn, response)
                idle_deadline = time.monotonic() + idle_timeout
    finally:
        with contextlib.suppress(OSError):
            server.close()
        shell.close()
        _clear_state(alias)
    return EXIT_OK


def _classify_connect_failure(shell: _RemoteShell) -> int:
    if shell.proc is None:
        return EXIT_NETWORK
    text = ""
    with contextlib.suppress(Exception):
        if shell.proc.stdout is not None:
            text = shell.proc.stdout.read().decode("utf-8", "replace")
    fclass = classify_failure(shell.proc.returncode or 255, text)
    return exit_code_for(fclass, default=EXIT_NETWORK)


def _resolve_auth(alias: str, requested: str, ssh_config: str | None) -> str:
    if requested != "auto":
        return requested
    meta = read_ssh_config_metadata(alias, ssh_config)
    if meta.get("auth", "").lower() == "password":
        return "password"
    return "key"


def _validate_options(options: list[str]) -> str | None:
    """Refuse any ``-o`` option that can execute local commands (FA-02)."""
    for option in options:
        reason = validate_ssh_option(option)
        if reason is not None:
            return reason
    return None


def _ssh_argv_for_shell(
    alias: str,
    connect_timeout: int,
    ssh_config: str | None,
    ssh_options: list[str],
    accept_new_hostkey: bool,
    password_auth: bool = False,
) -> list[str]:
    """Build the ``ssh -T <alias> sh`` argv for the long-lived shell.

    ``password_auth`` matters: this builder used to hardcode ``BatchMode=yes``
    while ``--auth password`` was accepted, so a password-only host produced
    ``BatchMode=yes`` + a rejected key and the daemon reported ``network``
    (102) — the same defect class that SSHCORE-001 fixed for exec and
    SSHCORE-025-follow-up fixed for tunnels, still live here. The secret
    reaches ssh through ``SSH_ASKPASS`` (see ``_RemoteShell.start``), never
    argv and never the environment dump.
    """
    refusal = _validate_options(ssh_options)
    if refusal is not None:
        raise ValueError(refusal)
    argv = ["ssh", "-T"]
    if password_auth:
        argv += [
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "UpdateHostKeys=no",
            "-o", "BatchMode=no",
        ]
    else:
        argv += ["-o", "BatchMode=yes"]
    argv += [
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]
    if accept_new_hostkey:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    elif password_auth:
        argv += ["-o", "StrictHostKeyChecking=yes"]
    config_path = merged_ssh_config(ssh_config)
    if config_path:
        argv += ["-F", config_path]
    for option in ssh_options:
        argv += ["-o", option]
    argv += ["--", alias, "sh"]
    return argv


# --- client side ------------------------------------------------------------


def _request(alias: str, command: str, timeout: float) -> dict[str, Any]:
    state = _read_state(alias)
    if state is None:
        return {
            "success": False,
            "exit_code": EXIT_DAEMON_NOT_RUNNING,
            "failure_class": "daemon_not_running",
            "stderr": f"ssh_daemon: no daemon for {alias}; run `ssh_daemon.py start {alias}`",
        }
    if not _probe(int(state["port"]), timeout=2):
        return {
            "success": False,
            "exit_code": EXIT_DAEMON_NOT_RUNNING,
            "failure_class": "daemon_not_running",
            "stderr": (
                f"ssh_daemon: state file for {alias} exists but nothing is "
                "listening; the daemon probably died. Run `stop` then `start`."
            ),
        }
    try:
        with socket.create_connection(("127.0.0.1", int(state["port"])), timeout=5) as sock:
            _send(sock, {"token": state["token"], "command": command, "timeout": timeout})
            response = _recv(sock)
    except (OSError, ConnectionError, json.JSONDecodeError) as e:
        return {
            "success": False,
            "exit_code": EXIT_DAEMON_PROTOCOL,
            "failure_class": "daemon_protocol",
            "stderr": f"ssh_daemon: protocol error: {e}",
        }
    code = int(response.get("exit_code", EXIT_INTERNAL))
    return {
        "success": code == 0,
        "exit_code": code,
        "stdout": response.get("stdout", ""),
        "stderr": response.get("stderr", ""),
        # FA-14: the server now names the class when the ssh child died, so the
        # client does not have to invent `daemon_protocol` for it.
        "failure_class": response.get("failure_class"),
        "response": response,
    }


# --- commands ---------------------------------------------------------------


def _cmd_start(args: argparse.Namespace) -> dict[str, Any]:
    # FA-02: refuse before anything is spawned (the child re-checks as well).
    refusal = _validate_options(args.ssh_option)
    if refusal is not None:
        return json_result(
            success=False,
            exit_code=EXIT_INVALID_OPTION,
            stderr=f"ssh_daemon: {refusal}\n",
            data={
                "alias": args.alias,
                "failure_class": "invalid_option",
                "ssh_option_refused": refusal,
            },
        )
    state = _read_state(args.alias)
    if state is not None and _probe(int(state["port"])):
        return json_result(
            success=True,
            exit_code=EXIT_OK,
            stdout=f"daemon already running for {args.alias} on 127.0.0.1:{state['port']}\n",
            data={
                "alias": args.alias,
                "port": state["port"],
                "pid": state["pid"],
                "already_running": True,
                "failure_class": None,
            },
        )
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_serve",
        args.alias,
        "--idle-timeout", str(args.idle_timeout),
        "--connect-timeout", str(args.connect_timeout),
        "--auth", args.auth,
        "--password-env", args.password_env,
    ]
    if args.accept_new_hostkey:
        argv.append("--accept-new-hostkey")
    if args.ssh_config:
        argv += ["--ssh-config", args.ssh_config]
    for option in args.ssh_option:
        argv += ["-o", option]
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": msys_safe_env(),
    }
    if not args.no_detach:
        kwargs.update(detach_kwargs())
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as e:
        return json_result(
            success=False,
            exit_code=EXIT_DEPENDENCY_MISSING,
            stderr=f"ssh_daemon: cannot start the server process: {e}\n",
            data={"alias": args.alias, "failure_class": "dependency_missing"},
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = _read_state(args.alias)
        if state is not None and _probe(int(state["port"])):
            effective_idle = int(state.get("idle_timeout") or args.idle_timeout)
            return json_result(
                success=True,
                exit_code=EXIT_OK,
                stdout=(
                    f"daemon up for {args.alias} on 127.0.0.1:{state['port']} "
                    f"(pid {state['pid']}, idle {effective_idle}s)\n"
                ),
                data={
                    "alias": args.alias,
                    "port": state["port"],
                    "pid": state["pid"],
                    "idle_timeout": effective_idle,
                    "idle_timeout_requested": args.idle_timeout,
                    "idle_timeout_floor": MIN_IDLE_TIMEOUT,
                    "failure_class": None,
                },
            )
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return json_result(
        success=False,
        exit_code=EXIT_NETWORK,
        stderr=(
            f"ssh_daemon: the server for {args.alias} did not come up "
            "(check the alias, the password variable, and connectivity)\n"
        ),
        data={"alias": args.alias, "failure_class": "network"},
    )


def _cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    outcome = _request(args.alias, args.command, args.timeout)
    data = {
        "alias": args.alias,
        "route": "daemon",
        "command": args.command,
        "failure_class": outcome.get("failure_class"),
        "reused": bool(outcome.get("response", {}).get("reused")),
    }
    return json_result(
        success=bool(outcome.get("success")),
        exit_code=int(outcome.get("exit_code", EXIT_INTERNAL)),
        stdout=outcome.get("stdout", ""),
        stderr=outcome.get("stderr", ""),
        data=data,
    )


def _cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    state = _read_state(args.alias)
    if state is None:
        return json_result(
            success=False,
            exit_code=EXIT_DAEMON_NOT_RUNNING,
            stderr=f"ssh_daemon: no daemon for {args.alias}\n",
            data={"alias": args.alias, "failure_class": "daemon_not_running"},
        )
    running = _probe(int(state["port"]))
    return json_result(
        success=running,
        exit_code=EXIT_OK if running else EXIT_DAEMON_NOT_RUNNING,
        stdout=(
            f"{args.alias}: 127.0.0.1:{state['port']} pid {state['pid']} "
            f"{'running' if running else 'stale'}\n"
        ),
        stderr="" if running else "ssh_daemon: state file is stale\n",
        data={
            "alias": args.alias,
            "port": state["port"],
            "pid": state["pid"],
            "running": running,
            "started_at": state.get("started_at"),
            "last_used_at": state.get("last_used_at"),
            "idle_timeout": state.get("idle_timeout"),
            "idle_timeout_requested": state.get("idle_timeout_requested"),
            "failure_class": None if running else "daemon_not_running",
        },
    )


def _cmd_stop(args: argparse.Namespace) -> dict[str, Any]:
    state = _read_state(args.alias)
    if state is None:
        return json_result(
            success=False,
            exit_code=EXIT_DAEMON_NOT_RUNNING,
            stderr=f"ssh_daemon: no daemon for {args.alias}\n",
            data={"alias": args.alias, "failure_class": "daemon_not_running"},
        )
    pid = int(state["pid"])
    stopped = False
    if is_alive(pid):
        stopped = bool(terminate(pid))
    _clear_state(args.alias)
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=f"stopped daemon for {args.alias} (pid {pid})\n",
        data={"alias": args.alias, "pid": pid, "stopped": stopped, "failure_class": None},
    )


def _cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    daemons: list[dict[str, Any]] = []
    for path in sorted(_state_dir().glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        port = int(raw.get("port") or 0)
        daemons.append(
            {
                "alias": raw.get("alias"),
                "port": port,
                "pid": raw.get("pid"),
                "running": bool(port) and _probe(port),
                "started_at": raw.get("started_at"),
                "last_used_at": raw.get("last_used_at"),
            }
        )
    human = "".join(
        f"{d['alias']}: 127.0.0.1:{d['port']} pid {d['pid']} "
        f"{'running' if d['running'] else 'stale'}\n"
        for d in daemons
    )
    return json_result(
        success=True, exit_code=EXIT_OK, stdout=human or "(no daemons)\n",
        data={"daemons": daemons, "failure_class": None},
    )


# --- CLI --------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                   help="Emit the shared JSON envelope")
    p.add_argument("--audit-log", dest="audit", action="store_true",
                   default=argparse.SUPPRESS,
                   help="Append a JSONL audit record under $TMPDIR/ssh-core/")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)
    p = SSHArgumentParser(
        prog="ssh_daemon.py",
        description=(
            "Keep one SSH session per alias alive and run commands through it, "
            "so repeated calls cost no extra handshake (works on Windows too)."
        ),
        parents=[common],
    )
    p.add_argument("--version", "-V", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", parents=[common], help="Start (or reattach to) a daemon")
    s.add_argument("alias")
    s.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT,
                   help=(
                       f"Exit after this many idle seconds "
                       f"(default: {DEFAULT_IDLE_TIMEOUT}; values below "
                       f"{MIN_IDLE_TIMEOUT} are clamped up)"
                   ))
    s.add_argument("--no-detach", action="store_true")
    s.add_argument("--connect-timeout", type=int, default=15)
    s.add_argument("--auth", choices=("auto", "key", "password"), default="auto")
    s.add_argument("--password-env", default="SSH_PASSWORD")
    s.add_argument("--accept-new-hostkey", action="store_true")
    s.add_argument("--ssh-config", default=None)
    s.add_argument("-o", "--ssh-option", action="append", default=[], metavar="KEY=VALUE")

    r = sub.add_parser("run", parents=[common], help="Run one command through the daemon")
    r.add_argument("alias")
    r.add_argument("command")
    r.add_argument("--timeout", type=int, default=120)

    st = sub.add_parser("status", parents=[common], help="Daemon health for one alias")
    st.add_argument("alias")

    sp = sub.add_parser("stop", parents=[common], help="Stop a daemon and remove its state")
    sp.add_argument("alias")

    sub.add_parser("list", parents=[common], help="List known daemons")

    serve = sub.add_parser("_serve", help=argparse.SUPPRESS)
    serve.add_argument("alias")
    serve.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT)
    serve.add_argument("--connect-timeout", type=int, default=15)
    serve.add_argument("--auth", choices=("auto", "key", "password"), default="auto")
    serve.add_argument("--password-env", default="SSH_PASSWORD")
    serve.add_argument("--accept-new-hostkey", action="store_true")
    serve.add_argument("--ssh-config", default=None)
    serve.add_argument("-o", "--ssh-option", action="append", default=[], metavar="KEY=VALUE")
    return p


def _audit(args: argparse.Namespace, result: dict) -> None:
    if not audit_enabled(getattr(args, "audit", None), default=False):
        return
    alias = getattr(args, "alias", "daemon")
    append_audit(
        alias,
        {
            "host": alias,
            "route": "daemon",
            "action": getattr(args, "cmd", "?"),
            "command": getattr(args, "command", None),
            "success": result.get("success"),
            "exit_code": result.get("exit_code"),
            "failure_class": (result.get("data") or {}).get("failure_class"),
        },
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_daemon.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    # `--json` may appear before or after the subcommand; SUPPRESS means the
    # attribute is absent unless the caller passed it somewhere.
    as_json = bool(getattr(args, "json", False)) or "--json" in argv

    if args.cmd == "_serve":
        return _serve(args)
    try:
        if args.cmd == "start":
            result = _cmd_start(args)
        elif args.cmd == "run":
            result = _cmd_run(args)
        elif args.cmd == "status":
            result = _cmd_status(args)
        elif args.cmd == "stop":
            result = _cmd_stop(args)
        elif args.cmd == "list":
            result = _cmd_list(args)
        else:  # pragma: no cover — argparse enforces the choices
            result = json_result(
                success=False, exit_code=EXIT_INVALID_ARGUMENTS,
                stderr=f"ssh_daemon: unknown command {args.cmd}\n",
                data={"failure_class": "invalid_arguments"},
            )
    except Exception as e:  # noqa: BLE001 — never raise to the caller
        result = json_result(
            success=False, exit_code=EXIT_INTERNAL,
            stderr=f"ssh_daemon: unexpected error: {e}\n",
            data={"failure_class": "internal"},
        )
    _audit(args, result)
    emit(result, as_json=as_json, redactor=redactor_for(getattr(args, "alias", "daemon")))
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(guarded_main("ssh_daemon.py", main))
