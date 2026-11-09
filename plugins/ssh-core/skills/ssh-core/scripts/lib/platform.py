"""Platform abstraction for ssh-core.

Everything POSIX-specific in this skill funnels through here, so a Windows
host gets a defined behaviour instead of a traceback:

    is_windows()            -> bool     (overridable in tests)
    split_command(cmd)      -> list[str] platform-correct argv tokenisation
    detach_kwargs()         -> dict      Popen kwargs for a detached child
    is_alive(pid)           -> bool      "is this PID still running?"
    terminate(pid, force)   -> bool      SIGTERM/SIGKILL vs TerminateProcess
    supports_pty_session()  -> bool
    can_detach()            -> bool
    PTY_UNSUPPORTED_HINT    -> str       why, and what to do instead

The Windows branch deliberately does NOT try to fake a pty: OpenSSH for
Windows is executed via CreateProcess, which cannot run a .bat/.cmd
SSH_ASKPASS helper, so there is no pure-Python way to answer a password
prompt. Password auth therefore stays POSIX-only and says so.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Any

# Test hook: ``set_platform_for_tests("windows")`` lets the POSIX CI exercise
# the Windows branches. ``None`` means "ask the interpreter".
_PLATFORM_OVERRIDE: str | None = None

PTY_UNSUPPORTED_HINT = (
    "password / interactive sessions need a POSIX pty (Linux, macOS, WSL). "
    "On Windows ssh-core uses `ssh -tt` + SSH_ASKPASS instead, which needs no "
    "pty — if you see this message, pass a transport explicitly or check that "
    "the askpass helper directory is writable."
)


def set_platform_for_tests(platform: str | None) -> None:
    """Force ``is_windows()`` to a value; pass None to restore detection."""
    global _PLATFORM_OVERRIDE
    _PLATFORM_OVERRIDE = platform


def is_windows() -> bool:
    if _PLATFORM_OVERRIDE is not None:
        return _PLATFORM_OVERRIDE == "windows"
    return sys.platform == "win32" or os.name == "nt"


def supports_pty_session() -> bool:
    """True when ``pty.fork()`` exists (i.e. not Windows)."""
    if is_windows():
        return False
    try:
        import pty  # noqa: F401
    except ImportError:  # pragma: no cover — exotic POSIX
        return False
    return True


def split_command(command: str) -> list[str]:
    """Tokenise a local-route command for the current platform.

    ``shlex.split`` defaults to POSIX rules, which treats backslash as an
    escape: ``type C:\\\\Users\\\\x`` becomes ``type C:Usersx``. On Windows we
    keep backslashes and strip one layer of surrounding quotes instead, so
    ``"C:\\\\Program Files\\\\Git\\\\bin\\\\bash.exe" -c "echo hi"`` tokenises the way
    cmd.exe would see it.
    """
    if not is_windows():
        return shlex.split(command)
    tokens = shlex.split(command, posix=False)
    out: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        out.append(token)
    return out


def detach_kwargs() -> dict[str, Any]:
    """Popen kwargs that put a child in its own session/process group.

    POSIX: ``start_new_session=True`` (setsid).
    Windows: a detached process group so closing the console does not take
    the tunnel down with it.
    """
    if is_windows():
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        return {"creationflags": 0x00000008 | 0x00000200}
    return {"start_new_session": True}


def can_detach() -> bool:
    """True when this platform can start a long-lived detached child."""
    if is_windows():
        return True  # creationflags path
    return hasattr(os, "setsid")


# --- process control --------------------------------------------------------


def is_alive(pid: int) -> bool:
    """True iff ``pid`` exists.

    POSIX: ``kill(pid, 0)`` is a pure probe.
    Windows: ``os.kill(pid, 0)`` is NOT a probe — CPython maps anything other
    than CTRL_C/CTRL_BREAK to ``TerminateProcess``, so probing would kill the
    tunnel. Use OpenProcess + GetExitCodeProcess instead.
    """
    if is_windows():
        return _win_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def terminate(pid: int, *, force: bool = False) -> bool:
    """Terminate ``pid``. Returns True when the process is gone afterwards.

    POSIX: SIGTERM (then SIGKILL for ``force``).
    Windows: TerminateProcess(pid, 1) — there is no graceful signal for a
    process that was started detached without a message loop.
    """
    if is_windows():
        return _win_terminate(pid)
    import signal

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


# --- Windows implementation (via ctypes) ------------------------------------

_WINAPI_OVERRIDE: Any = None

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def set_winapi_for_tests(winapi: Any) -> None:
    """Inject a stand-in for the ctypes/win32 layer (used by unit tests)."""
    global _WINAPI_OVERRIDE
    _WINAPI_OVERRIDE = winapi


class _KernelAdapter:
    """Small adapter over kernel32 so tests can inject a stand-in.

    The test double only needs ``OpenProcess`` / ``GetExitCode`` /
    ``Terminate`` / ``CloseHandle``.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        self._k = kernel32

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> Any:
        return self._k.OpenProcess(access, inherit, pid)

    def GetExitCode(self, handle: Any) -> int:
        code = self._wintypes.DWORD()
        ok = self._k.GetExitCodeProcess(handle, self._ctypes.byref(code))
        return int(code.value) if ok else -1

    def Terminate(self, handle: Any) -> bool:
        return bool(self._k.TerminateProcess(handle, 1))

    def CloseHandle(self, handle: Any) -> None:
        self._k.CloseHandle(handle)


def _winapi() -> Any:
    if _WINAPI_OVERRIDE is not None:
        return _WINAPI_OVERRIDE
    return _KernelAdapter()


def _win_is_alive(pid: int) -> bool:
    api = _winapi()
    handle = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = api.GetExitCode(handle)
        return code == STILL_ACTIVE
    finally:
        api.CloseHandle(handle)


def _win_terminate(pid: int) -> bool:
    api = _winapi()
    handle = api.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return not _win_is_alive(pid)
    try:
        api.Terminate(handle)
    finally:
        api.CloseHandle(handle)
    return not _win_is_alive(pid)
