"""Platform-neutral local execution primitives for remote-debug (LS-01).

Every script in this plugin takes ``local`` as a host and, until this module
existed, ran the *remote* one-liner through the local platform shell:

    port_check.py  local --target db --ports 5432
        -> cmd /c 'if command -v nc >/dev/null 2>&1; then nc -zv ...'
        -> cmd: "-v was unexpected at this time."  (exit 1)
        -> classified as "filtered"

That last line is the defect this module removes. ``cmd.exe`` cannot parse
``if command -v nc``, the probe **never ran**, and the non-zero exit fell
through ``_classify_status``'s catch-all to ``filtered`` — indistinguishable
from a real firewall drop. A reachability check that answers "filtered" when
it means "I could not look" is worse than no answer at all.

Two mechanisms, used together:

:func:`run_shell`
    Runs a POSIX one-liner through the platform shell (``sh -c`` / ``cmd /c``)
    and *detects* the "the command never ran" class of failure — shell syntax
    errors and missing binaries — instead of letting the non-zero exit be
    re-interpreted as a probe result. Anything that cannot be run says so:

    ====================  =========================  =====  ====================
    situation             failure_class              exit   caller sees
    ====================  =========================  =====  ====================
    binary not on PATH    ``dependency_missing``        3   source-error
    shell syntax error    ``local_shell_error``         3   source-error
    shell itself missing  ``dependency_missing``        3   source-error
    command timed out     ``timeout``                 124   timeout
    ran, non-zero exit    ``None``                      *   caller classifies
    ====================  =========================  =====  ====================

:func:`tcp_probe` / :func:`udp_probe`
    Do the *port check* natively. There is no portable ``nc`` and no portable
    ``/dev/tcp``; ``socket.connect_ex`` is both, on every platform Python runs
    on. The envelope is shaped to read like ``nc -zv`` output so
    ``port_check._classify_status`` classifies a local cell with the exact same
    code path as a remote one — one classifier, no per-platform fork.

:func:`tail_file` / :func:`tail_file_follow`
    Read the file natively (no ``tail`` binary: Windows has none), and spell
    the path the way the platform spells paths: ``C:/Windows/win.ini`` and
    ``file://local/C:/Windows/win.ini`` both resolve on Windows.

    A source the platform genuinely cannot serve — a POSIX-absolute path on a
    Windows local route — is a **skip**, not a failure: ``skipped: true``,
    ``skipped_reason: "posix_path_on_windows"``, ``failure_class: None``, exit
    0. It used to surface as ``local_not_found`` (``\\etc\\hostname``), which
    blamed the operator for a platform mismatch. A skip is neither ok nor
    failed, so a caller aggregating several hosts must leave it out of the
    pass/fail decision — the same rule ``ssh_cluster`` uses (BUG-010 /
    OPT-004).

The probes never raise: a local diagnostic that crashes is a diagnostic that
told you nothing.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import subprocess
import sys
import time
from typing import Any

#: Exit code used across the marketplace for "a prerequisite is absent".
EXIT_DEPENDENCY_MISSING = 3

#: Exit code for a wrapper-level timeout (GNU ``timeout(1)`` convention).
EXIT_TIMEOUT = 124

#: Success.
EXIT_OK = 0

#: Generic failure.
EXIT_FAIL = 1

LOCAL_HOST = "local"


def utf8_stdio() -> None:
    """Force UTF-8 on this interpreter's stdout/stderr.

    Windows defaults a *piped* stdout to the ANSI code page (cp936 on a
    Chinese install). ``json.dump(..., ensure_ascii=False)`` then writes GBK
    bytes, and every consumer that reads the envelope as UTF-8 sees ``\\ufffd``
    where the Chinese text was. The JSON parsed fine, so nothing looked broken
    — the data was simply wrong. Mirrors ``ssh-core/lib/__init__._utf8_stdio``
    (LS-04).

    Safe to call on every emit: on an already-UTF-8 stream this is a no-op.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")

# --- "the command never ran" detection --------------------------------------
#
# Matched against stdout+stderr of a *local shell* invocation. These strings
# mean the probe did not execute, so they must never be re-read as a probe
# result. Both dialects are listed because the wrapper picks the shell by
# platform and a POSIX shell also runs under MSYS/Git-Bash on Windows.

MISSING_BINARY_PATTERNS: tuple[str, ...] = (
    # cmd.exe
    "is not recognized as an internal or external command",
    "is not recognized as an internal or external command,",
    # PowerShell (when a caller wraps a script in powershell -Command)
    "the term '",
    # sh / bash (POSIX exit 127)
    "command not found",
    "not found:",
)

SHELL_SYNTAX_ERROR_PATTERNS: tuple[str, ...] = (
    # cmd.exe
    "was unexpected at this time",
    "syntax of the command is incorrect",
    "the syntax of the command is incorrect",
    # sh / bash
    "syntax error",
    "unexpected token",
    "unexpected end of file",
)

#: Exit code ``sh`` uses for "command not found"; also what our own probes use
#: to report a missing netcat.
EXIT_COMMAND_NOT_FOUND = 127


def is_windows() -> bool:
    """True on Windows. Kept overridable so tests can exercise both branches."""
    return os.name == "nt" or os.sys.platform == "win32"


def shell_argv(command: str, *, windows: bool | None = None) -> list[str]:
    """Return the argv that runs ``command`` through this platform's shell."""
    win = is_windows() if windows is None else windows
    if win:
        return ["cmd", "/c", command]
    return ["sh", "-c", command]


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


def _combined(stdout: str, stderr: str) -> str:
    return (stdout or "") + "\n" + (stderr or "")


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(pat in lowered for pat in patterns)


def classify_shell_failure(
    returncode: int, stdout: str, stderr: str
) -> str | None:
    """Return a failure_class when the command provably never ran.

    ``None`` means "it ran and exited ``returncode``"; the caller is then free
    to interpret the output (which for a port probe is a real answer).
    """
    if returncode == 0:
        return None
    combined = _combined(stdout, stderr)
    if _matches(combined, SHELL_SYNTAX_ERROR_PATTERNS):
        return "local_shell_error"
    if returncode == EXIT_COMMAND_NOT_FOUND:
        return "dependency_missing"
    if _matches(combined, MISSING_BINARY_PATTERNS):
        return "dependency_missing"
    return None


def local_unsupported(name: str, command: str, reason: str) -> dict[str, Any]:
    """Envelope for "this platform cannot run that one-liner at all"."""
    return _envelope(
        False,
        EXIT_DEPENDENCY_MISSING,
        stderr=(
            f"{reason}. Point the command at a remote alias instead of "
            f"'local', or run it on a POSIX host.\n"
        ),
        data={
            "route": "local",
            "failure_class": "local_shell_error",
            "unsupported_command": command,
            "reason": reason,
        },
    )


def run_shell(
    name: str,
    command: str,
    timeout: int,
    *,
    windows: bool | None = None,
) -> dict[str, Any]:
    """Run ``command`` through the platform shell; classify honestly.

    ``windows`` is a test hook; production always detects.

    The child is decoded as UTF-8 with replacement, never as the ANSI code
    page: a Chinese Windows returns GBK bytes from ``date``/PowerShell, and the
    default ``text=True`` decode raised ``UnicodeDecodeError`` *inside the
    reader thread* -- which surfaces as a bare ``Exception in thread`` and an
    empty envelope, i.e. another silent "no answer".
    """
    argv = shell_argv(command, windows=windows)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=f"{name}: local command timed out after {timeout}s\n",
            data={"route": "local", "failure_class": "timeout"},
        )
    except FileNotFoundError as exc:
        return _envelope(
            False,
            EXIT_DEPENDENCY_MISSING,
            stderr=f"{name}: local shell unavailable: {exc}\n",
            data={"route": "local", "failure_class": "dependency_missing"},
        )
    except OSError as exc:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"{name}: local command failed to start: {exc}\n",
            data={"route": "local", "failure_class": "local_shell_error"},
        )

    failure = classify_shell_failure(proc.returncode, proc.stdout, proc.stderr)
    data: dict[str, Any] = {
        "route": "local",
        "failure_class": failure,
        "shell": argv[0],
    }
    if failure is not None:
        data["shell_returncode"] = proc.returncode
    return _envelope(
        proc.returncode == 0,
        proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        data=data,
    )


# --- native TCP / UDP probes -------------------------------------------------


#: ``errno`` names Windows uses that the ``errno`` module does not map back to
#: a POSIX name. Without these, a refused port on Windows fell through to the
#: generic branch and read as a timeout.
WSA_ERRORS: dict[int, str] = {
    10013: "EACCES",        # WSAEACCES
    10035: "EINPROGRESS",   # WSAEWOULDBLOCK (connect in progress / expired)
    10051: "ENETUNREACH",   # WSAENETUNREACH
    10054: "ECONNRESET",    # WSAECONNRESET
    10060: "ETIMEDOUT",     # WSAETIMEDOUT
    10061: "ECONNREFUSED",  # WSAECONNREFUSED
    10065: "EHOSTUNREACH",  # WSAEHOSTUNREACH
}

#: POSIX names that mean "the port answered and said no".
_REFUSED_NAMES = frozenset({"ECONNREFUSED", "ECONNRESET"})
#: POSIX names that mean "nothing came back before the budget".
_TIMEOUT_NAMES = frozenset(
    {"ETIMEDOUT", "EINPROGRESS", "EHOSTUNREACH", "ENETUNREACH"}
)
#: POSIX names that mean "the local stack refused to send".
_DENIED_NAMES = frozenset({"EACCES", "EPERM"})


def _errno_name(code: int) -> str:
    """POSIX name for ``code``, covering the Windows WSA numbering."""
    if code in WSA_ERRORS:
        return WSA_ERRORS[code]
    return errno.errorcode.get(code, "")


def _errno_text(code: int) -> str:
    name = _errno_name(code)
    return f"{name} ({code})" if name else f"error ({code})"



def _connect_once(
    family: int,
    socktype: int,
    proto: int,
    sockaddr: Any,
    timeout: float,
) -> tuple[int, str | None]:
    """One ``connect_ex`` attempt. Returns ``(rc, os_error_text)``."""
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex(sockaddr), None
    except OSError as exc:  # resolution/family mismatch inside connect
        return (getattr(exc, "errno", None) or errno.EHOSTUNREACH), str(exc)
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def tcp_probe(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Native equivalent of ``nc -zv -w <timeout> <host> <port>``.

    Returns an envelope whose stdout reads like nc's, so the shared
    ``_classify_status`` handles it without a platform branch.
    """
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=(
                f"nc: getaddrinfo for host \"{host}\" port {port}: {exc}\n"
            ),
            data={
                "route": "local",
                "probe": "tcp",
                "failure_class": "local_host_error",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )
    if not infos:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"nc: no address for host \"{host}\" port {port}\n",
            data={
                "route": "local",
                "probe": "tcp",
                "failure_class": "local_host_error",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )

    last_rc = errno.ECONNREFUSED
    last_error: str | None = None
    for family, socktype, proto, _canon, sockaddr in infos:
        rc, os_error = _connect_once(family, socktype, proto, sockaddr, timeout)
        if os_error is not None:
            last_error = os_error
        last_rc = rc
        if rc == 0:
            return _envelope(
                True,
                0,
                stdout=(
                    f"Connection to {host} {port} port [tcp/*] succeeded!\n"
                ),
                data={
                    "route": "local",
                    "probe": "tcp",
                    "failure_class": None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )

    detail = _errno_text(last_rc) if last_error is None else last_error
    name = _errno_name(last_rc)
    if name in _REFUSED_NAMES:
        text = (
            f"nc: connect to {host} port {port} (tcp) failed: "
            f"Connection refused\n"
        )
    elif name in _TIMEOUT_NAMES:
        text = (
            f"nc: connect to {host} port {port} (tcp) failed: "
            f"Operation timed out\n"
        )
    elif name in _DENIED_NAMES:
        text = (
            f"nc: connect to {host} port {port} (tcp) failed: "
            f"Permission denied\n"
        )
    else:
        text = (
            f"nc: connect to {host} port {port} (tcp) failed: {detail}\n"
        )
    return _envelope(
        False,
        EXIT_FAIL,
        stderr=text,
        data={
            "route": "local",
            "probe": "tcp",
            "failure_class": None,
            "errno": last_rc,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        },
    )


def udp_probe(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Native equivalent of ``nc -zu -w <timeout> <host> <port>``.

    UDP has no handshake, so the only two observable outcomes are:

    * an ICMP port-unreachable, surfaced by the kernel as ``ECONNREFUSED`` on
      a *connected* socket — the port is definitively closed;
    * silence — open **or** dropped by a firewall. Reported as a success with
      empty output, which ``_classify_status`` renders ``open-or-filtered``.

    This is the same semantic the remote ``nc -zu`` path uses; the difference
    is that it works on a box with no netcat.
    """
    started = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except socket.gaierror as exc:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"nc: getaddrinfo for host \"{host}\" port {port}: {exc}\n",
            data={
                "route": "local",
                "probe": "udp",
                "failure_class": "local_host_error",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )

    family, socktype, proto, _canon, sockaddr = infos[0]
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout)
    try:
        sock.connect(sockaddr)
        with contextlib.suppress(OSError):
            sock.send(b"\x00")
        try:
            sock.recv(1)
        except ConnectionResetError:
            # Windows surfaces ICMP port-unreachable as WSAECONNRESET.
            return _envelope(
                False,
                EXIT_FAIL,
                stderr=(
                    f"nc: read udp {host}:{port}: Connection refused\n"
                ),
                data={
                    "route": "local",
                    "probe": "udp",
                    "failure_class": None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )
        except (TimeoutError, socket.timeout):
            return _envelope(
                True,
                0,
                stdout="",
                data={
                    "route": "local",
                    "probe": "udp",
                    "failure_class": None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )
        except OSError as exc:
            if getattr(exc, "errno", None) in (errno.ECONNREFUSED, 10054):
                return _envelope(
                    False,
                    EXIT_FAIL,
                    stderr=f"nc: read udp {host}:{port}: Connection refused\n",
                    data={
                        "route": "local",
                        "probe": "udp",
                        "failure_class": None,
                        "elapsed_ms": round(
                            (time.monotonic() - started) * 1000
                        ),
                    },
                )
            return _envelope(
                True,
                0,
                stdout="",
                data={
                    "route": "local",
                    "probe": "udp",
                    "failure_class": None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )
        # Something answered: the port is definitely not dropped.
        return _envelope(
            True,
            0,
            stdout="",
            data={
                "route": "local",
                "probe": "udp",
                "failure_class": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )
    except OSError as exc:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"nc: connect to {host} port {port} (udp) failed: {exc}\n",
            data={
                "route": "local",
                "probe": "udp",
                "failure_class": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def port_probe(
    host: str, port: int, timeout: float, *, udp: bool = False
) -> dict[str, Any]:
    """Dispatch to :func:`tcp_probe` / :func:`udp_probe`."""
    if udp:
        return udp_probe(host, port, timeout)
    return tcp_probe(host, port, timeout)


# --- native file tail --------------------------------------------------------

#: ``skipped_reason`` for "the local implementation cannot serve this source on
#: this platform". Stable and documented (``remote-debug/SKILL.md``): a POSIX
#: absolute path handed to a Windows local route. Deliberately not a
#: ``failure_class`` -- nothing failed, the route cannot apply.
SKIP_POSIX_PATH_ON_WINDOWS = "posix_path_on_windows"

#: Filesystem URL scheme an operator may type (log-aggregator's spelling).
FILE_URL_PREFIX = "file://"

#: ``file://local/<path>`` -- the local authority of that scheme.
FILE_URL_LOCAL_PREFIX = FILE_URL_PREFIX + "local/"


def local_path_payload(path: str, *, windows: bool | None = None) -> str:
    """Return the filesystem path this host should open for *path*.

    Two spellings beyond a plain native path are accepted:

    ``file://local/C:/Windows/win.ini``
        The log-aggregator spelling. ``source_spec`` there normalises every
        ``file://`` payload to start with ``/`` (correct on a POSIX host), so a
        drive-letter payload has to lose that slash again -- the same rule as
        ``log-aggregator/lib/fetchers._local_path_payload``. A ``file://`` URL
        naming any other authority is a remote host and is left untouched.
    ``C:/Windows/win.ini`` / ``C:\\Windows\\win.ini``
        A drive-letter path. It is already native on Windows and is returned
        unchanged; nothing is joined onto it.

    A POSIX absolute path is returned unchanged on every platform. On Windows
    it is then *skipped* by :func:`tail_file` rather than being reinterpreted
    as ``\\etc\\hostname``; see :func:`is_posix_path_on_windows`.
    """
    win = is_windows() if windows is None else windows
    payload = path
    if payload.startswith(FILE_URL_LOCAL_PREFIX):
        # Keep an already-absolute payload absolute; the drive-letter rule
        # below removes the leading slash again where it does not apply.
        rest = payload[len(FILE_URL_LOCAL_PREFIX):]
        payload = rest if rest.startswith("/") else "/" + rest
    elif payload.startswith(FILE_URL_PREFIX):
        remainder = payload[len(FILE_URL_PREFIX):]
        if not remainder.startswith("/"):
            return payload  # `file://<other-host>/...`: not this route
        # `file:///C:/...`: empty authority, native payload follows.
        payload = remainder
    if (
        win
        and len(payload) > 3
        and payload[0] == "/"
        and payload[1].isalpha()
        and payload[2] == ":"
    ):
        return payload[1:]
    return payload


def is_posix_path_on_windows(path: str, *, windows: bool | None = None) -> bool:
    """True when *path* is POSIX-absolute and this local route is Windows.

    ``/etc/hostname`` on Windows used to become ``\\etc\\hostname`` and be
    reported ``local_not_found`` -- the operator was blamed for a platform
    mismatch. ``//server/share`` is a UNC path and is *not* a POSIX path, so it
    still resolves normally.
    """
    win = is_windows() if windows is None else windows
    if not win:
        return False
    payload = local_path_payload(path, windows=True)
    if not payload.startswith("/"):
        return False
    return not payload.startswith("//")


def local_skip(reason: str, detail: str) -> dict[str, Any]:
    """Envelope for "this source is not servable on this platform".

    A skip is neither a success nor a failure: nothing was read, so
    ``success`` is False, but ``skipped``/``skipped_reason`` say why and
    ``failure_class`` stays ``None`` so an aggregator can leave the host out of
    the pass/fail decision instead of turning the run red (mirrors
    ``ssh_cluster``'s ``skipped`` semantics, BUG-010).

    ``detail`` is the one-line human explanation, already prefixed the way the
    other local route messages are (``tail_log: ...``) so a direct caller sees
    the same text a multi-host run surfaces.
    """
    return _envelope(
        False,
        EXIT_OK,
        stderr=f"{detail}\n",
        data={
            "route": "local",
            "skipped": True,
            "skipped_reason": reason,
            "failure_class": None,
        },
    )


def _skip_posix_path_on_windows(path: str) -> dict[str, Any]:
    return local_skip(
        SKIP_POSIX_PATH_ON_WINDOWS,
        (
            f"tail_log: skipped {path!r}: it is a POSIX-absolute path and the "
            "local host is Windows, so there is no such file to read; pass a "
            "Windows path such as C:/Windows/win.ini (or "
            "file://local/C:/Windows/win.ini), or name a remote POSIX host"
        ),
    )


def tail_file(
    path: str,
    lines: int,
    *,
    max_bytes: int = 4 * 1024 * 1024,
    windows: bool | None = None,
) -> dict[str, Any]:
    """Native equivalent of ``tail -n <lines> <path>``.

    Reads at most ``max_bytes`` from the end (a log can be gigabytes; ``tail``
    seeks too), so this stays cheap on a live production file.

    ``windows`` is a test hook (as in :func:`run_shell`); production detects.
    A POSIX-absolute path on Windows returns a *skip*, not a failure.
    """
    from pathlib import Path

    win = is_windows() if windows is None else windows
    if is_posix_path_on_windows(path, windows=win):
        return _skip_posix_path_on_windows(path)

    target = Path(local_path_payload(path, windows=win))
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            start = max(0, size - max_bytes)
            handle.seek(start)
            blob = handle.read()
    except FileNotFoundError:
        return _envelope(
            False,
            EXIT_FAIL,
            # No `tail` involved any more, so the message must not blame one:
            # a stale "tail: cannot open" made an operator look for a missing
            # binary that the code no longer calls.
            stderr=f"tail_log: cannot open {str(target)!r}: no such file\n",
            data={"route": "local", "failure_class": "local_not_found"},
        )
    except PermissionError:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"tail_log: cannot open {str(target)!r}: permission denied\n",
            data={"route": "local", "failure_class": "permission"},
        )
    except OSError as exc:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"tail_log: cannot open {str(target)!r}: {exc}\n",
            data={"route": "local", "failure_class": "local_io_error"},
        )

    text = blob.decode("utf-8", errors="replace")
    if start > 0:
        # We joined mid-line; drop the fragment so the first emitted line is
        # whole, exactly like `tail` seeking to a line boundary.
        _, sep, tail_text = text.partition("\n")
        text = tail_text if sep else ""
    rows = text.splitlines()
    if lines > 0:
        rows = rows[-lines:]
    return _envelope(
        True,
        0,
        stdout="\n".join(rows) + ("\n" if rows else ""),
        data={"route": "local", "failure_class": None},
    )


def tail_file_follow(
    path: str,
    lines: int,
    timeout_s: float,
    *,
    poll_s: float = 0.5,
    max_bytes: int = 4 * 1024 * 1024,
    windows: bool | None = None,
) -> dict[str, Any]:
    """Native ``tail -F`` bounded by ``timeout_s`` (the ``--follow-timeout``).

    Rotation-tolerant by construction: each poll re-``stat``s the file and
    re-reads from the recorded offset, and a shrinking file (truncate/rotate)
    resets the offset to 0 -- the same guarantee ``-F`` gives. A platform skip
    from :func:`tail_file` is passed through unchanged.
    """
    from pathlib import Path

    win = is_windows() if windows is None else windows
    first = tail_file(path, lines, max_bytes=max_bytes, windows=win)
    if not first.get("success"):
        return first
    target = Path(local_path_payload(path, windows=win))

    collected: list[str] = []
    if first.get("stdout"):
        collected.extend((first["stdout"]).splitlines())

    deadline = time.monotonic() + timeout_s
    try:
        offset = target.stat().st_size
    except OSError:
        offset = 0

    while time.monotonic() < deadline:
        time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
        try:
            size = target.stat().st_size
        except OSError:
            continue
        if size < offset:  # truncated or rotated
            offset = 0
        if size == offset:
            continue
        try:
            with target.open("rb") as handle:
                handle.seek(offset)
                blob = handle.read(max_bytes)
        except OSError:
            continue
        offset += len(blob)
        new_text = blob.decode("utf-8", errors="replace")
        collected.extend(new_text.splitlines())

    return _envelope(
        True,
        0,
        stdout="\n".join(collected) + ("\n" if collected else ""),
        data={
            "route": "local",
            "failure_class": None,
            "follow": True,
            "follow_seconds": timeout_s,
        },
    )


__all__ = [
    "EXIT_COMMAND_NOT_FOUND",
    "EXIT_DEPENDENCY_MISSING",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_TIMEOUT",
    "FILE_URL_LOCAL_PREFIX",
    "FILE_URL_PREFIX",
    "LOCAL_HOST",
    "MISSING_BINARY_PATTERNS",
    "SHELL_SYNTAX_ERROR_PATTERNS",
    "SKIP_POSIX_PATH_ON_WINDOWS",
    "classify_shell_failure",
    "is_posix_path_on_windows",
    "is_windows",
    "local_path_payload",
    "local_skip",
    "local_unsupported",
    "port_probe",
    "run_shell",
    "shell_argv",
    "tail_file",
    "tail_file_follow",
    "tcp_probe",
    "udp_probe",
    "utf8_stdio",
]
