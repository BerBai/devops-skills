"""One interactive-session driver, two transports (SSH-09).

Before 1.1 the whole interactive story lived inside ``ssh_session.py`` and was
gated on ``pty.fork``: on Windows every network-device flow therefore died at
``dependency_missing`` even though OpenSSH for Windows can drive a device
perfectly well over pipes with ``-tt``.

This module keeps the *state machine* (password prompts, chained vendor
prompts, paging, ``[Y/N]`` confirmations, prompt detection, graceful device
logout) in one place and gives it two interchangeable transports:

``PtySession``
    POSIX only. ``pty.fork`` gives the child a real controlling terminal, which
    is the only way ``ssh`` will read a password without ``SSH_ASKPASS``.

``PipeSession``
    Any platform. ``ssh -tt`` + pipes; the password is answered through
    :mod:`lib.askpass` (``SSH_ASKPASS_REQUIRE=force``) instead of a terminal.
    Measured against OpenSSH_for_Windows_9.5p1 and OpenSSH 8.2 on Kylin.

Both expose the same surface, so :func:`authenticate`, :func:`run_script`,
:func:`wait_for_first_prompt` and :func:`leave_session` are transport-agnostic.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import select
import struct
import threading
import time
from pathlib import Path
from typing import Any

from . import (
    HOST_KEY_PATTERN,
    KEX_PATTERN,
    MAX_STREAM_BYTES,
    NETWORK_PATTERN,
    classify_failure,
    is_windows,
    terminate,
)
from .askpass import askpass_env, scrub_secret

try:  # POSIX only; Windows has no pty.fork.
    import fcntl
    import pty
    import termios

    _PTY_AVAILABLE = True
except ImportError:  # pragma: no cover — Windows
    _PTY_AVAILABLE = False

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_DEPENDENCY = 3
EXIT_TIMEOUT = 124

# Terminal geometry: wide + tall so pagers do not trigger with fewer than
# ~300 lines of output even on devices that ignore paging-off commands.
PTY_ROWS = 400
PTY_COLS = 300
READ_CHUNK = 4096
# Grace between SIGTERM and SIGKILL when tearing a stuck session down.
KILL_GRACE_S = 2.0
# How long we wait for the shell prompt after authentication.
DEFAULT_STEP_TIMEOUT = 30
MAX_PASSWORD_ATTEMPTS = 3
MAX_EXIT_ATTEMPTS = 3

# Password / passcode prompt at the tail of the buffer. Vendor banners can
# prefix it ("Password for admin@host:", "admin@1.2.3.4's password:").
PASSWORD_PROMPT_RE = re.compile(r"(?i)(?:password|passcode|passwd)[^:\r\n]{0,80}:\s*$")
# Interactive confirmation prompts: `[Y/N]`, `(y/n)`, "continue? [yes/no]".
CONFIRM_PROMPT_RE = re.compile(
    r"(?i)(?:\[y/n\]|\(y/n\)|\(yes/no\)|continue\?|"
    r"\[yes/no\]|please input.*?\(y/n\)|are you sure)"
)
# Host-key trust prompt (only auto-answered when --accept-new-hostkey).
HOSTKEY_PROMPT_RE = re.compile(r"(?i)are you sure you want to continue connecting")
# Default prompt heuristic: `<Switch>`, `[Switch-GigabitEthernet0/0/1]`,
# `admin@host:~$`, `#`, `>` at end of line.
DEFAULT_PROMPT_RE = re.compile(
    r"(?:<[^<>\r\n]{1,80}>|\[[^\[\]\r\n]{1,120}\]|[\w.@:/~-]{0,64}[>$#%])\s*$"
)
# Lines that mean authentication is over (password rejected).
#
# Split in two on purpose: the numeric form ("2 failed attempts") is what VRP
# and Comware print *after* our password, but it also appears in a normal Linux
# MOTD ("There was 1 failed login attempt since the last successful login").
# Applying it before we have typed anything rejected healthy logins, so the
# numeric branch is only consulted once a password has been sent in this
# session.
AUTH_FAILED_RE = re.compile(
    r"(?i)(Permission denied|Authentication failed|Too many authentication failures|"
    r"no more authentication methods)"
)
AUTH_FAILED_DEVICE_RE = re.compile(
    r"(?i)(Authentication failed|\d+\s+(?:failed|invalid)\s+(?:attempts?|logins?))"
)
# Device-side farewells. VRP closes the SSH channel after `quit` without an
# exit-status, so ssh reports 255 even though the logout was clean.
GRACEFUL_CLOSE_RE = re.compile(
    r"(?i)Connection to .* closed|Goodbye|Connection closed by|"
    r"User logged out|session is closing|"
    r"client_loop: send disconnect: Broken pipe"
)


class SessionError(Exception):
    """Structured failure carrying a failure_class + exit code."""

    def __init__(self, failure_class: str, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.exit_code = exit_code
        self.message = message


def _scrub(text: str, secret: str) -> str:
    """Remove any accidental echo of the password from captured output."""
    return scrub_secret(text, secret)


class _BoundedBuffer:
    """A text buffer that keeps at most ``cap`` bytes (FA-18).

    The interactive drivers used to accumulate the whole transcript in a bare
    ``self._buf += text``, so a device that streams (``display`` of a full
    routing table, a paging-off mistake) could grow the process without bound —
    while SKILL.md promised a 10 MB cap for every route. The cap keeps the
    **tail**, which is what prompt matching and troubleshooting need, and counts
    what it dropped.
    """

    def __init__(self, cap: int = MAX_STREAM_BYTES) -> None:
        self.cap = cap
        self._text = ""
        self.bytes_elided = 0

    def append(self, text: str) -> None:
        self._text += text
        encoded = len(self._text.encode("utf-8", errors="replace"))
        while encoded > self.cap:
            excess = encoded - self.cap
            # Drop whole lines from the front when possible; fall back to a
            # byte-wise trim so one enormous line still cannot blow the cap.
            cut = self._text.find("\n", excess)
            if cut < 0:
                cut = min(len(self._text), excess)
            dropped = self._text[: cut + 1]
            self.bytes_elided += len(dropped.encode("utf-8", errors="replace"))
            self._text = self._text[cut + 1:]
            encoded = len(self._text.encode("utf-8", errors="replace"))

    def take(self) -> str:
        text, self._text = self._text, ""
        return text

    def peek(self, start: int = 0) -> str:
        return self._text[start:]

    def push_front(self, text: str) -> None:
        if text:
            self._text = text + self._text

    def replace(self, text: str) -> None:
        self._text = text

    def __len__(self) -> int:
        return len(self._text)

    @property
    def truncated(self) -> bool:
        return self.bytes_elided > 0


# --- POSIX pty transport ----------------------------------------------------


class PtySession:
    """One pty-backed ssh child process (POSIX)."""

    kind = "pty"

    def __init__(
        self,
        argv: list[str],
        *,
        password: str,
        env: dict[str, str] | None = None,
    ) -> None:
        self.argv = argv
        self.password = password
        self.env = dict(env if env is not None else os.environ)
        # Never forward the password to the child: ssh does not need it in
        # its environment, and anything that dumps env would leak it.
        for var in ("SSH_PASSWORD",):
            self.env.pop(var, None)
        self.env.setdefault("TERM", "vt100")
        self.pid: int | None = None
        self.fd: int | None = None
        self.exit_status: int | None = None
        self.graceful_close = False
        # FA-18: bounded (10 MB) instead of an unbounded string; the tail is
        # what prompt matching needs, and the cap is what SKILL.md promises.
        self._buf = _BoundedBuffer()
        self._eof = False
        self.transcript: list[str] = []

    def start(self) -> None:
        if not _PTY_AVAILABLE:
            raise SessionError(
                "dependency_missing",
                EXIT_DEPENDENCY,
                "ssh_session requires POSIX pty support (pty.fork).",
            )
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvpe(self.argv[0], self.argv, self.env)
            except OSError:
                os._exit(127)
        self.pid = pid
        self.fd = fd
        with contextlib.suppress(OSError):
            fcntl.ioctl(
                fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0),
            )

    def is_closed(self) -> bool:
        return self._eof or self.pid is None

    def wait_exit(self, timeout: float) -> int | None:
        if self.pid is None:
            return None
        pid = self.pid
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            done, raw = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                self.pid = None
                self.exit_status = self._decode_status(raw)
                return self.exit_status
            time.sleep(0.05)
        return None

    @staticmethod
    def _decode_status(status: int) -> int:
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return EXIT_FAIL

    def close(self, *, timeout: float = KILL_GRACE_S) -> int:
        """Terminate if needed and return the child's exit status.

        Ordering matters: the pty master is closed **after** the child is
        reaped. Closing it first sends SIGHUP to the session leader, which
        would turn a clean ``exit`` into exit code 129.
        """
        if self.pid is None:
            if self.fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self.fd)
                self.fd = None
            return EXIT_OK if self.exit_status is None else self.exit_status
        pid = self.pid
        status: int | None = self.wait_exit(timeout)
        if status is None:
            with contextlib.suppress(OSError):
                terminate(pid)
            status = self.wait_exit(1.0)
        if status is None:
            with contextlib.suppress(OSError):
                terminate(pid, force=True)
            with contextlib.suppress(ChildProcessError):
                _, raw = os.waitpid(pid, 0)
                status = self._decode_status(raw)
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
        self.pid = None
        self.exit_status = EXIT_FAIL if status is None else status
        return self.exit_status

    def read_available(self, timeout: float) -> str:
        if self.fd is None or self._eof:
            return ""
        try:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            self._eof = True
            return ""
        if not ready:
            return ""
        try:
            chunk = os.read(self.fd, READ_CHUNK)
        except OSError as e:
            if e.errno == errno.EIO:  # Linux pty EOF
                self._eof = True
                return ""
            raise
        if not chunk:
            self._eof = True
            return ""
        text = chunk.decode("utf-8", errors="replace")
        self._buf.append(text)
        return text

    def buffer(self) -> str:
        return self._buf.peek()

    def consume(self) -> str:
        return self._buf.take()

    def push_back(self, text: str) -> None:
        """Return already-consumed text to the buffer (used by the login driver)."""
        self._buf.push_front(text)

    def send(self, text: str) -> None:
        if self.fd is None:
            raise SessionError("remote_error", EXIT_FAIL, "session is closed")
        os.write(self.fd, text.encode("utf-8"))

    def sendline(self, line: str) -> None:
        self.send(line + "\n")

    def close_stdin(self) -> None:
        """Signal EOF on a pty: the canonical end-of-file character (Ctrl-D).

        A pty has no "close just stdin"; writing the terminal's EOF character
        makes the remote command see end-of-input while the session itself
        stays usable for the result (SSHCORE-024).
        """
        if self.fd is None:
            return
        with contextlib.suppress(OSError):
            os.write(self.fd, b"\x04")

    def expect(self, patterns: list[re.Pattern[str]], timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while True:
            tail = self._buf.peek()[-4096:]
            for idx, pattern in enumerate(patterns):
                if pattern.search(tail):
                    return idx
            if self._eof:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.read_available(min(remaining, 0.2))


# --- pipe transport (any platform) ------------------------------------------


class PipeSession:
    """``ssh -tt`` + pipes; password answered through ``SSH_ASKPASS``.

    This is the Windows transport (no ``pty.fork`` there), and it works on
    POSIX too — useful whenever the caller has no terminal at all.

    Differences from :class:`PtySession` that matter:

    * stdout and stderr are separate pipes; both are merged into one buffer so
      the prompt heuristics see everything in arrival order.
    * ``close()`` sends ``quit``/``exit`` first (via :func:`leave_session`),
      then terminates the process — a pipe has no SIGHUP-on-close semantics.
    * The password reaches ssh through the askpass helper, never stdin, so a
      chained vendor prompt is the only thing we type into stdin.
    """

    kind = "pipe"
    #: ssh's own password prompt is answered by the askpass helper, so the
    #: driver must NOT type the secret into stdin (that path is reserved for
    #: vendor chained prompts after ssh's own).
    password_via_helper = True

    def __init__(
        self,
        argv: list[str],
        *,
        password: str,
        env: dict[str, str] | None = None,
        use_askpass: bool = True,
    ) -> None:
        self.argv = argv
        self.password = password
        self._base_env = dict(env if env is not None else os.environ)
        for var in ("SSH_PASSWORD",):
            self._base_env.pop(var, None)
        self._base_env.setdefault("TERM", "vt100")
        self.use_askpass = use_askpass
        self.proc: Any = None
        self.exit_status: int | None = None
        self.graceful_close = False
        self._buf = _BoundedBuffer()
        self._lock = threading.Lock()
        self._eof = False
        self._askpass_ctx: Any = None
        self.transcript: list[str] = []

    def start(self) -> None:
        import subprocess

        env = dict(self._base_env)
        if self.use_askpass:
            self._askpass_ctx = askpass_env(self.password)
            env = self._askpass_ctx.__enter__()
        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=0,
            )
        except OSError:
            self._close_askpass()
            raise
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stream = self.proc.stdout
        try:
            while True:
                chunk = stream.read(READ_CHUNK)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                with self._lock:
                    self._buf.append(text)
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._eof = True

    def is_closed(self) -> bool:
        if self.proc is None:
            return True
        if self.proc.poll() is not None:
            if self._eof:
                return True
            # Give the reader thread a moment to drain the last bytes.
            time.sleep(0.05)
        return self._eof

    def wait_exit(self, timeout: float) -> int | None:
        if self.proc is None:
            return None
        try:
            code = self.proc.wait(timeout=max(0.0, timeout))
        except Exception:
            return None
        self.exit_status = code
        return code

    def close(self, *, timeout: float = KILL_GRACE_S) -> int:
        if self.proc is None:
            self._close_askpass()
            return EXIT_OK if self.exit_status is None else self.exit_status
        status: int | None = self.proc.poll()
        if status is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()
            try:
                status = self.proc.wait(timeout=timeout)
            except Exception:
                with contextlib.suppress(Exception):
                    self.proc.kill()
                with contextlib.suppress(Exception):
                    status = self.proc.wait(timeout=1.0)
        self.exit_status = EXIT_FAIL if status is None else status
        self._close_askpass()
        return self.exit_status

    def _close_askpass(self) -> None:
        if self._askpass_ctx is not None:
            with contextlib.suppress(Exception):
                self._askpass_ctx.__exit__(None, None, None)
            self._askpass_ctx = None

    def read_available(self, timeout: float) -> str:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            start_len = len(self._buf)
        while time.monotonic() < deadline:
            with self._lock:
                if len(self._buf) != start_len or self._eof:
                    break
            time.sleep(0.02)
        with self._lock:
            return self._buf.peek()

    def buffer(self) -> str:
        with self._lock:
            return self._buf.peek()

    def consume(self) -> str:
        with self._lock:
            return self._buf.take()

    def push_back(self, text: str) -> None:
        with self._lock:
            self._buf.push_front(text)

    def send(self, text: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise SessionError("remote_error", EXIT_FAIL, "session is closed")
        try:
            self.proc.stdin.write(text.encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise SessionError(
                "remote_error", EXIT_FAIL, f"session is closed: {e}"
            ) from e

    def sendline(self, line: str) -> None:
        # A bare LF is what a real terminal sends for Enter; ssh forwards the
        # bytes as-is in both transports, and every VRP/Comware/IOS/Linux
        # prompt accepts LF.
        self.send(line + "\n")

    def expect(self, patterns: list[re.Pattern[str]], timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while True:
            tail = self.buffer()[-4096:]
            for idx, pattern in enumerate(patterns):
                if pattern.search(tail):
                    return idx
            with self._lock:
                closed = self._eof
            if closed:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.read_available(min(remaining, 0.2))

    def close_stdin(self) -> None:
        """Give the remote command EOF, not just a closed local descriptor.

        ``ssh -tt`` allocates a remote **pty**, so closing the local stdin pipe
        does not produce end-of-file on the far side — the remote command keeps
        waiting forever and the caller sees a timeout (measured: `ssh -tt
        host cat` was still running 20 s after stdin was closed). Writing the
        terminal's EOF character (Ctrl-D, ``\\x04``) first is what the remote
        line discipline actually interprets as "input ended"; the close that
        follows only cleans up the local handle (SSHCORE-024).
        """
        if self.proc is None or self.proc.stdin is None:
            return
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.write(b"\x04")
            self.proc.stdin.flush()
        with contextlib.suppress(OSError, ValueError):
            self.proc.stdin.close()


def supports_pty() -> bool:
    """True when ``pty.fork`` exists and this is not Windows."""
    if is_windows():
        return False
    return _PTY_AVAILABLE


def supports_interactive() -> bool:
    """True when *some* transport can drive a password-authenticated session.

    Always true: POSIX uses ``pty.fork``, everything else uses ``ssh -tt`` +
    ``SSH_ASKPASS`` (SSH-09). A caller that needs real terminal semantics
    should ask :func:`supports_pty` instead.
    """
    return True


def make_session(
    argv: list[str],
    password: str,
    *,
    prefer_pty: bool = True,
    env: dict[str, str] | None = None,
) -> PtySession | PipeSession:
    """Return the best available transport for this host.

    POSIX prefers the real pty (no askpass helper needed); Windows — and any
    caller that explicitly asks for pipes — gets :class:`PipeSession`.
    """
    if prefer_pty and supports_pty():
        return PtySession(argv, password=password, env=env)
    return PipeSession(argv, password=password, env=env)


# --- authentication ---------------------------------------------------------


def authenticate(
    session: Any,
    timeout: float,
    accept_new_hostkey: bool,
    *,
    host: str = "",
) -> None:
    """Drive login to completion.

    Two prompt sources are in play and they must not be confused:

    * ssh's *own* password prompt. On the pty transport nobody else can answer
      it, so the driver types the secret. On the pipe transport the askpass
      helper already ran, so the text is informational only — typing the secret
      again would send it to the remote shell as a command.
    * a *server-side* prompt (keyboard-interactive, or a VRP ``Password:``
      after the SSH layer). That one has to be typed even on the pipe
      transport.
    """
    prompts = [HOSTKEY_PROMPT_RE, PASSWORD_PROMPT_RE]
    deadline = time.monotonic() + timeout
    if getattr(session, "password_via_helper", False):
        _login_via_askpass(session, deadline)
        return
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tail = session.consume()
            raise SessionError(
                "prompt_timeout",
                EXIT_TIMEOUT,
                f"no password prompt within {timeout:.0f}s; last output:\n{tail[-500:]}",
            )
        idx = session.expect(prompts, min(remaining, 1.0))
        if idx is None:
            if session.is_closed() or session.wait_exit(0.2) is not None:
                text = _scrub(session.consume(), session.password)
                status = session.exit_status or 255
                fclass = classify_failure(status, text) or "remote_error"
                raise SessionError(fclass, status, text.strip() or fclass)
            tail = session.buffer()[-4096:]
            if AUTH_FAILED_RE.search(tail):
                text = session.consume()
                raise SessionError(
                    "auth", 255, _scrub(text, session.password).strip()
                )
            continue
        if idx == 0:
            if not accept_new_hostkey:
                text = session.consume()
                raise SessionError(
                    "host_key",
                    255,
                    "host key is not trusted; rerun with --accept-new-hostkey "
                    "after verifying the fingerprint.\n" + text.strip(),
                )
            session.sendline("yes")
            session.consume()
            continue
        session.consume()
        session.sendline(session.password)
        if _answer_chained_password_prompts(
            session, deadline, max_attempts=MAX_PASSWORD_ATTEMPTS
        ):
            continue
        return


def _login_via_askpass(session: Any, deadline: float) -> None:
    """Wait out a login that ``SSH_ASKPASS`` performs on our behalf.

    The helper answers ssh's own prompt, so the driver's job is to notice when
    authentication is over and *not* to type the secret a second time (that
    would land in the remote shell as a command). "Over" means any of:

    * ssh's prompt was echoed, followed by real output (banner / MOTD / the
      command's own stdout);
    * the child closed the channel after producing output — the usual ``-tt``
      + exec-channel ending, where ssh exits 255 for lack of an exit status;
    * the child failed before producing output (auth rejected, host key,
      network) → classified and raised.

    Output produced *after* the prompt is deliberately left in the buffer: for
    the exec route it is the command's stdout, and throwing it away would make
    ``ssh_execute --auth password`` return an empty result.
    """
    saw_prompt = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            text = _scrub(session.buffer(), session.password)
            raise SessionError(
                "prompt_timeout",
                EXIT_TIMEOUT,
                f"login did not complete within the budget; last output:\n{text[-500:]}",
            )
        tail = session.buffer()[-4096:]
        if HOSTKEY_PROMPT_RE.search(tail):
            text = _scrub(session.buffer(), session.password)
            raise SessionError(
                "host_key",
                255,
                "host key is not trusted; verify the fingerprint and rerun with "
                "--accept-new-hostkey.\n" + text.strip(),
            )
        if AUTH_FAILED_RE.search(tail) or (
            saw_prompt and AUTH_FAILED_DEVICE_RE.search(tail)
        ):
            text = _scrub(session.buffer(), session.password)
            raise SessionError("auth", 255, text.strip())
        if PASSWORD_PROMPT_RE.search(tail):
            saw_prompt = True
            # Drop only the prompt; keep whatever follows for the caller (for
            # the exec route that is the command's own stdout).
            session.consume()
            continue
        if tail.strip():
            # ssh printed something that is not a prompt and not a rejection:
            # the banner / MOTD / shell prompt, i.e. authentication succeeded.
            # (`SSH_ASKPASS_REQUIRE=force` means the prompt itself is often
            # not even echoed, so waiting for it would burn the whole budget.)
            _record_login_banner(session, tail)
            return
        if session.is_closed() or session.wait_exit(0.1) is not None:
            status = session.exit_status if session.exit_status is not None else 255
            if status == 0:
                return
            # Classify against what ssh actually said. Passing "" here made
            # `no matching key exchange method found` (which ssh prints at
            # login time) fall through to `remote_error`, and a network
            # failure lose its class entirely (SSHCORE-003 regression on the
            # interactive routes).
            text = _scrub(session.buffer(), session.password)
            fclass = classify_failure(status, text) or "remote_error"
            raise SessionError(fclass, status, text.strip() or fclass)
        session.read_available(min(0.2, remaining))


def _record_login_banner(session: Any, tail: str) -> None:
    """Remember the pre-auth banner so callers can strip it later (SSHCORE-017).

    On a merged stream (``-tt`` a pty merges stdout/stderr), the greeting is
    indistinguishable from the command's output *after the fact* unless its
    extent is known from this moment: everything before authentication
    finished is transport noise. Stored on the session, not returned, so the
    login driver's contract does not change.
    """
    text = tail.strip()
    if not text:
        return
    try:
        session.login_banner = text
    except AttributeError:  # read-only fake in a test
        pass


def _answer_chained_password_prompts(
    session: Any,
    deadline: float,
    *,
    max_attempts: int,
) -> bool:
    """Answer follow-up password prompts. True if one was answered.

    Waits only while the child is silent: as soon as any non-prompt output
    arrives, authentication is over and the caller must proceed.
    """
    for _ in range(max_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or session.is_closed():
            return False
        if session.expect([PASSWORD_PROMPT_RE], timeout=min(remaining, 0.5)) == 0:
            session.consume()
            session.sendline(session.password)
            continue
        if session.buffer().strip():
            return False
    return False


def wait_for_first_prompt(
    session: Any,
    prompt_re: re.Pattern[str],
    *,
    timeout: float,
) -> None:
    """Wait for the shell prompt after login, or raise the *real* reason."""
    if session.expect([prompt_re], timeout=timeout) is not None:
        return
    tail = _scrub(session.consume(), session.password)
    if session.is_closed() or session.wait_exit(0.5) is not None:
        fclass = classify_failure(session.exit_status or 255, tail) or "remote_error"
        raise SessionError(fclass, session.exit_status or 255, tail.strip() or fclass)
    raise SessionError(
        "prompt_timeout",
        EXIT_TIMEOUT,
        f"no shell prompt within {timeout:.0f}s after login; last output:\n{tail[-500:]}",
    )


# --- command script ---------------------------------------------------------


def run_script(
    session: Any,
    commands: list[str],
    *,
    prompt_re: re.Pattern[str],
    confirm: str,
    step_timeout: float,
    deadline: float,
    capture_output: bool = False,
) -> list[dict]:
    """Send ``commands`` one by one, waiting for the prompt after each."""
    records: list[dict] = []
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SessionError("timeout", EXIT_TIMEOUT, "session budget exhausted")
        started = time.monotonic()
        session.sendline(command)
        idx = session.expect(
            [CONFIRM_PROMPT_RE, prompt_re], timeout=min(step_timeout, remaining)
        )
        if idx == 0:
            prompt_text = session.consume()
            if confirm == "ask":
                raise SessionError(
                    "confirmation_required",
                    EXIT_FAIL,
                    "remote asked for confirmation and --confirm=ask was set:\n"
                    + _scrub(prompt_text, session.password).strip()[-400:],
                )
            session.sendline("y" if confirm == "yes" else "n")
            if session.expect(
                [prompt_re], timeout=min(step_timeout, remaining)
            ) is None:
                output = _scrub(session.consume(), session.password)
                if closed_by_device(output):
                    session.graceful_close = True
                    break
                raise SessionError(
                    "prompt_timeout",
                    EXIT_TIMEOUT,
                    f"no prompt after answering confirmation to {command!r}; "
                    f"last output:\n{output[-500:]}",
                )
        elif idx is None:
            output = _scrub(session.consume(), session.password)
            if closed_by_device(output):
                session.graceful_close = True
                records.append(
                    {
                        "command": command,
                        "view": None,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "output_bytes": len(output),
                        "exit_ok": True,
                    }
                )
                break
            raise SessionError(
                "prompt_timeout",
                EXIT_TIMEOUT,
                f"no prompt within {step_timeout:.0f}s after {command!r}; "
                f"last output:\n{output[-500:]}",
            )
        output = _scrub(session.consume(), session.password)
        duration_ms = int((time.monotonic() - started) * 1000)
        view = last_prompt(output)
        record = {
            "command": command,
            "view": view,
            "duration_ms": duration_ms,
            "output_bytes": len(output),
            "exit_ok": True,
        }
        if capture_output:
            record["output"] = output
        records.append(record)
    return records


def closed_by_device(text: str) -> bool:
    """True when the transcript shows the device ended the session politely."""
    return bool(GRACEFUL_CLOSE_RE.search(text))


def leave_session(session: Any, *, timeout: float) -> None:
    """Ask the remote shell to exit, tolerating nested views and dialects."""
    per_attempt = max(0.5, timeout / MAX_EXIT_ATTEMPTS)
    for command in ("quit", "exit", "quit"):
        if session.is_closed():
            return
        with contextlib.suppress(SessionError):
            session.sendline(command)
        deadline = time.monotonic() + per_attempt
        while time.monotonic() < deadline:
            if session.wait_exit(0.1) is not None:
                session.graceful_close = True
                return
            if session.is_closed():
                return
            if closed_by_device(session.buffer()[-2000:]):
                session.graceful_close = True
                session.consume()  # keep the farewell out of command output
                return
            session.read_available(0.1)


def last_prompt(output: str) -> str | None:
    """Best-effort view guess: the last non-empty line that looks like a prompt."""
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped and DEFAULT_PROMPT_RE.search(stripped):
            return stripped
    return None


def records_show_failure(records: list[dict], *, is_network: bool) -> bool:
    """Did any executed command report a device-level error?

    Device CLIs return 0 for everything, so a failed command is only visible
    in its text (``Error:`` / ``% `` / ``Invalid input``).
    """
    if not records:
        return not is_network
    for record in records:
        text = record.get("output") or ""
        if re.search(r"(?im)^\s*(Error:|% |Invalid input|Unrecognized command)", text):
            return True
    return False


def snapshot_path(directory: Path, alias: str, tag: str) -> Path:
    safe_alias = re.sub(r"[^A-Za-z0-9_.-]+", "_", alias)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / f"{safe_alias}-{tag}-{int(time.time() * 1000)}.txt"


__all__ = [
    "AUTH_FAILED_RE",
    "CONFIRM_PROMPT_RE",
    "DEFAULT_PROMPT_RE",
    "DEFAULT_STEP_TIMEOUT",
    "GRACEFUL_CLOSE_RE",
    "HOSTKEY_PROMPT_RE",
    "PASSWORD_PROMPT_RE",
    "PipeSession",
    "PtySession",
    "SessionError",
    "authenticate",
    "closed_by_device",
    "last_prompt",
    "leave_session",
    "make_session",
    "records_show_failure",
    "run_script",
    "snapshot_path",
    "supports_pty",
    "wait_for_first_prompt",
]
