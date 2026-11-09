"""askpass.py — cross-platform ``SSH_ASKPASS`` plumbing.

Why this module exists
----------------------
OpenSSH reads a password from a **controlling terminal**, never a pipe. That
is fine when a human is watching (``ssh_session.py`` uses ``pty.fork`` for
exactly that), but it makes every headless flow impossible: an agent, a CI
job, ``subprocess`` with ``stdin=DEVNULL``, a detached tunnel, and **Windows**
(which has no ``pty.fork`` at all) all fail with "Permission denied" before
the password is ever sent.

OpenSSH has an escape hatch built in: ``SSH_ASKPASS`` +
``SSH_ASKPASS_REQUIRE=force`` runs an arbitrary helper program to collect the
secret. The helper reads it from a 0600 file inside a 0700 private directory
and prints it on stdout. The password therefore never appears in argv, in the
environment, in the JSON envelope, or in the audit log — it only exists on
disk for the lifetime of the call and is deleted in a ``finally``.

Public surface::

    askpass_env(password) -> context manager yielding an env dict
    run_with_password(argv, password=..., timeout=...) -> PasswordRunResult
    scrub_secret(text, secret) -> str
    AskpassUnavailable                    -> OSError with .failure_class/.as_data()

Where the scratch directory comes from (review A2)
--------------------------------------------------
The helper needs a private writable directory. Before 1.5 there were only two
candidates (``%TEMP%`` and ``~/.cache``) and the fallback raised a bare
``OSError`` — the one place in the skill that could print a traceback instead
of an envelope. Now:

1. ``$SSH_CORE_ASKPASS_DIR`` — the exact directory, used as-is;
2. ``$SSH_CORE_ASKPASS_HOME/ssh-core-askpass`` — a parent to create under;
3. ``$TMPDIR/ssh-core-askpass``;
4. ``~/.cache/ssh-core-askpass``;
5. ``$DSH_HOME/tmp/ssh-core-askpass`` — the DSH sandbox keeps this writable
   even when ``%TEMP%`` subdirectories are denied;
6. ``./.ssh-core-askpass`` — last resort, still 0700/ACL-restricted.

On Windows the mode argument is not trusted: ``mkdir(mode=0o700)`` produces a
protected DACL that can omit the current user entirely, so the directory is
created normally and tightened with ``icacls <dir> /grant <user>:(OI)(CI)F``.
Failures raise :class:`AskpassUnavailable`, which every script turns into
``failure_class: "askpass_unavailable"`` (exit 114).

Platform notes
--------------
* POSIX: the helper is ``#!/bin/sh`` + ``cat``.
* Windows: the helper is a ``.cmd`` running ``type``. Measured against
  OpenSSH_for_Windows_9.5p1: ``CreateProcessW`` runs a batch file fine, and
  ``type`` reproduces the secret byte-for-byte (including UTF-8 and
  ``&^%!()`` characters), which a ``set /p`` round-trip would not.
* OpenSSH < 8.4 ignores ``SSH_ASKPASS_REQUIRE``; ``DISPLAY`` + no tty is the
  historical trigger, so both are set here.

See:
    references/troubleshooting.md — "password_required", "no_tty_for_password"
    references/safety.md          — hard rule 2 (no credentials in argv)
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import (
    BoundedCapture,
    CHILD_TEXT_KWARGS,
    drain_stream,
    msys_safe_env,
    write_text_lf,
)

# Override the scratch directory (tests, locked-down hosts, sandboxes).
ASKPASS_DIR_ENV = "SSH_CORE_ASKPASS_DIR"
#: Second-chance override: a *parent* directory the helper directory is created
#: under. Useful when only one directory on the box is writable and the caller
#: wants to keep the per-host layout.
ASKPASS_HOME_ENV = "SSH_CORE_ASKPASS_HOME"
ASKPASS_DIR_NAME = "ssh-core-askpass"
SECRET_FILENAME = "secret"
HELPER_FILENAME = "askpass"
# Exit code used by the wrappers when the child had to be killed.
EXIT_TIMEOUT = 124


class AskpassUnavailable(OSError):
    """No writable scratch directory could be staged for the helper.

    Carries ``failure_class`` / ``exit_code`` so every caller can turn it into
    the shared JSON envelope instead of letting an ``OSError`` traceback escape
    (review A2: this was the only path in the whole skill that could print a
    raw traceback on stdout).
    """

    failure_class = "askpass_unavailable"
    exit_code = 114  # EXIT_ASKPASS_UNAVAILABLE (lib/exit_codes)

    def __init__(self, message: str, *, tried: list[str] | None = None) -> None:
        super().__init__(message)
        self.tried = list(tried or [])

    def as_data(self) -> dict[str, object]:
        return {
            "failure_class": self.failure_class,
            "askpass_dir_env": ASKPASS_DIR_ENV,
            "askpass_home_env": ASKPASS_HOME_ENV,
            "askpass_tried": self.tried,
        }


def _candidate_dirs() -> Iterator[Path]:
    """Directories to try, most appropriate first.

    Order (review A2): explicit ``SSH_CORE_ASKPASS_DIR``, then anything under
    ``SSH_CORE_ASKPASS_HOME``, the system temp dir, ``~/.cache``, the DSH home
    (``$DSH_HOME/tmp``) and finally the current directory. The last two exist
    because a Windows sandbox can deny writes under ``%TEMP%`` while leaving
    the profile directory (where DSH keeps its own state) writable.
    """
    override = os.environ.get(ASKPASS_DIR_ENV)
    if override:
        yield Path(override).expanduser()
    home_override = os.environ.get(ASKPASS_HOME_ENV)
    if home_override:
        yield Path(home_override).expanduser() / ASKPASS_DIR_NAME
    yield Path(tempfile.gettempdir()) / ASKPASS_DIR_NAME
    home = os.path.expanduser("~")
    if home and home != "~":
        yield Path(home) / ".cache" / ASKPASS_DIR_NAME
    dsh_home = os.environ.get("DSH_HOME")
    if dsh_home:
        yield Path(dsh_home) / "tmp" / ASKPASS_DIR_NAME
    yield Path.cwd() / f".{ASKPASS_DIR_NAME}"


def _restrict_windows_acl(directory: Path) -> None:
    """Give the current user full control of ``directory`` (Windows only).

    ``mkdir(mode=0o700)`` is a no-op on Windows *except* that Python translates
    a restrictive mode into a protected DACL that does not necessarily grant
    the creator write access (review A2 measured a directory whose ACL held
    only SYSTEM / Administrators / OWNER RIGHTS — the skill locked itself out
    of its own scratch dir). ``icacls`` is the supported way to fix that, and
    it is present on every Windows since Vista.
    """
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["icacls", str(directory), "/grant", f"{user}:(OI)(CI)F", "/q"],
            capture_output=True,
            timeout=15,
            check=False,
            env=msys_safe_env(),
        )


def _make_private_dir(path: Path) -> None:
    """Create ``path`` 0700 without trusting ``mode`` (see A2)."""
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=True)
        _restrict_windows_acl(path)
    else:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            path.chmod(0o700)


def _ensure_base_dir() -> Path:
    """Create (0700) and return the first writable scratch directory."""
    last_error: OSError | None = None
    tried: list[str] = []
    for candidate in _candidate_dirs():
        tried.append(str(candidate))
        try:
            _make_private_dir(candidate)
            probe = candidate / f".probe-{os.getpid()}"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError as exc:  # read-only / sandboxed location
            last_error = exc
    raise AskpassUnavailable(
        "ssh-core: no writable directory for the SSH_ASKPASS helper "
        f"(tried {tried}); set ${ASKPASS_DIR_ENV} to a private writable "
        f"directory (or ${ASKPASS_HOME_ENV} to a parent of one). "
        f"Last error: {last_error}",
        tried=tried,
    )


def _write_helper(directory: Path) -> Path:
    """Write the platform-specific askpass helper into ``directory``."""
    if os.name == "nt":
        helper = directory / f"{HELPER_FILENAME}.cmd"
        # %~dp0 = directory of the batch file (with trailing separator).
        # `type` streams the file verbatim: no CRLF is appended, unlike echo.
        write_text_lf(
            helper,
            "@echo off\r\n"
            f'type "%~dp0{SECRET_FILENAME}"\r\n',
            encoding="ascii",
        )
        return helper

    helper = directory / f"{HELPER_FILENAME}.sh"
    write_text_lf(
        helper,
        "#!/bin/sh\n"
        f'exec cat "$(dirname "$0")/{SECRET_FILENAME}"\n',
    )
    with contextlib.suppress(OSError):
        helper.chmod(0o700)
    return helper


@contextlib.contextmanager
def askpass_env(password: str) -> Iterator[dict[str, str]]:
    """Yield an environment that makes ``ssh`` answer the prompt with ``password``.

    A fresh private directory per call (0600 secret, 0700 dir) avoids both a
    password-derived filename and the cross-call deletion race that a shared
    filename would create.
    """
    base = _ensure_base_dir()
    work = Path(tempfile.mkdtemp(prefix="call-", dir=str(base)))
    with contextlib.suppress(OSError):
        work.chmod(0o700)
    if os.name == "nt":
        # mkdtemp inherits the parent's ACL, but the parent may have needed the
        # icacls repair; do it again for the per-call dir (review A2).
        _restrict_windows_acl(work)
    secret = work / SECRET_FILENAME
    helper: Path | None = None
    try:
        # write_text_lf rather than Path.write_text(newline=""): the keyword is
        # 3.10+ while the declared floor is 3.8 (FA-08). The secret must be
        # byte-exact, so no newline translation is allowed on any version.
        write_text_lf(secret, password)
        with contextlib.suppress(OSError):
            secret.chmod(0o600)
        helper = _write_helper(work)

        env = msys_safe_env()
        env["SSH_ASKPASS"] = str(helper)
        # 8.4+: never fall back to the tty. Older clients need DISPLAY.
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", ":0")
        yield env
    finally:
        for path in (secret, helper):
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink()
        with contextlib.suppress(OSError):
            work.rmdir()


@dataclass
class PasswordRunResult:
    """Outcome of one password-answered ssh invocation."""

    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float
    timed_out: bool

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _run(
    argv: list[str], timeout: float, env: dict[str, str]
) -> tuple[int, BoundedCapture, BoundedCapture, float, bool]:
    """Spawn with ``stdin=DEVNULL`` and bounded streaming capture.

    ``DEVNULL`` is load-bearing: it removes any chance of ssh blocking on a
    terminal read instead of asking the helper.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        **CHILD_TEXT_KWARGS,
    )
    out_sink = BoundedCapture()
    err_sink = BoundedCapture()
    readers = [
        _thread(drain_stream, proc.stdout, out_sink),
        _thread(drain_stream, proc.stderr, err_sink),
    ]
    for thread in readers:
        thread.start()

    timed_out = False
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        returncode = EXIT_TIMEOUT

    for thread in readers:
        thread.join(timeout=5)
    elapsed = round(time.monotonic() - start, 3)
    return returncode, out_sink, err_sink, elapsed, timed_out


def _thread(target, *args):  # type: ignore[no-untyped-def]
    import threading

    return threading.Thread(target=target, args=args, daemon=True)


def scrub_secret(text: str, secret: str) -> str:
    """Remove an accidental echo of ``secret`` from captured output."""
    if not text or not secret:
        return text
    return text.replace(secret, "[REDACTED]")


def run_with_password(
    argv: list[str],
    *,
    password: str,
    timeout: float = 120,
) -> PasswordRunResult:
    """Run ``argv`` with the password answered through ``SSH_ASKPASS``."""
    with askpass_env(password) as env:
        returncode, out, err, elapsed, timed_out = _run(argv, timeout, env)
    return PasswordRunResult(
        returncode=returncode,
        stdout=scrub_secret(out.text(), password),
        stderr=scrub_secret(err.text(), password),
        elapsed_s=elapsed,
        timed_out=timed_out,
    )


__all__ = [
    "ASKPASS_DIR_ENV",
    "ASKPASS_HOME_ENV",
    "AskpassUnavailable",
    "PasswordRunResult",
    "askpass_env",
    "run_with_password",
    "scrub_secret",
]
