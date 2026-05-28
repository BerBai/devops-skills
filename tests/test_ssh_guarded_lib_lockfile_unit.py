"""Unit tests for ssh-guarded ``lib/lockfile.py`` — 6+ cases per design D12.

Cases:
- acquire / release roundtrip (POSIX)
- double-acquire raises ``LockHeld`` with holder_pid populated
- Windows / degraded mode via monkeypatched ``_is_posix``
- cleanup on exception
- stale lock semantics (degraded: residual .lock blocks acquire)
- holder_pid is None when nothing in /proc/locks / fuser
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "scripts"
)

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import lockfile as _lockfile_mod  # noqa: E402, I001
from lib.lockfile import LockHeld as _LockHeld  # noqa: E402, I001
from lib.lockfile import exclusive_lock as _exclusive_lock  # noqa: E402, I001

lockfile = _lockfile_mod
LockHeld = _LockHeld
exclusive_lock = _exclusive_lock

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))

# Spec constants
EXPECTED_LOCK_SUFFIX = ".lock"


def test_acquire_release_posix(tmp_path: Path) -> None:
    art = tmp_path / "command-aaaa.json"
    art.write_text("{}")
    with exclusive_lock(art) as info:
        assert info.path.name.endswith(EXPECTED_LOCK_SUFFIX)
        assert info.path.exists()
        assert info.holder_pid == os.getpid()
    # POSIX flock releases on close — sentinel file may remain (LOCK_UN
    # alone doesn't delete it). What we care about is re-acquire works.
    with exclusive_lock(art):
        pass  # re-acquire succeeded


def test_double_acquire_raises_lock_held(tmp_path: Path) -> None:
    art = tmp_path / "command-bbbb.json"
    art.write_text("{}")
    with exclusive_lock(art), pytest.raises(LockHeld), exclusive_lock(art):
        pass


def test_lock_held_carries_holder_pid_or_none(tmp_path: Path) -> None:
    """holder_pid is best-effort; either int or None acceptable per design.

    We spawn a *real* Python child process so the holder_pid genuinely
    differs from ours. The child loops while holding the lock until we
    delete a "release" sentinel file or the timeout fires.
    """
    import subprocess  # noqa: PLC0415 — scoped to this test

    art = tmp_path / "command-cccc.json"
    art.write_text("{}")
    ready_file = tmp_path / "ready"
    release_file = tmp_path / "release"
    release_file.write_text("hold")

    holder_script = f'''
import sys, time
sys.path.insert(0, {str(SCRIPTS_DIR)!r})
from lib.lockfile import exclusive_lock
from pathlib import Path
with exclusive_lock(Path({str(art)!r})):
    Path({str(ready_file)!r}).write_text("ok")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and Path({str(release_file)!r}).exists():
        time.sleep(0.05)
'''
    proc = subprocess.Popen([sys.executable, "-c", holder_script])  # noqa: S603
    try:
        # Wait for child to acquire.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready_file.exists():
            time.sleep(0.05)
        assert ready_file.exists(), "child failed to acquire within 10s"

        with pytest.raises(LockHeld) as exc_info, exclusive_lock(art):
            pass
        # holder_pid is either the child PID or None (Linux /proc/locks vs
        # macOS where /proc isn't available and fuser may not be on PATH).
        holder_pid = exc_info.value.holder_pid
        assert holder_pid is None or isinstance(holder_pid, int)
    finally:
        release_file.unlink(missing_ok=True)
        proc.wait(timeout=5)


def test_cleanup_on_exception(tmp_path: Path) -> None:
    art = tmp_path / "command-dddd.json"
    art.write_text("{}")
    with pytest.raises(RuntimeError), exclusive_lock(art):
        raise RuntimeError("boom")
    # After exception we must still be able to re-acquire.
    with exclusive_lock(art):
        pass


def test_degraded_path_acquire_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force the degraded (Windows-style) branch and verify lifecycle."""
    monkeypatch.setattr(lockfile, "_is_posix", lambda: False)
    art = tmp_path / "command-eeee.json"
    art.write_text("{}")
    lock_path = Path(str(art) + ".lock")
    with exclusive_lock(art) as info:
        assert info.path == lock_path
        assert lock_path.exists()
        # Sentinel contains our PID.
        assert lock_path.read_text().strip() == str(os.getpid())
    # Degraded mode unlinks on release.
    assert not lock_path.exists()


def test_degraded_path_stale_lock_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a stale ``.lock`` file already exists, degraded mode refuses."""
    monkeypatch.setattr(lockfile, "_is_posix", lambda: False)
    art = tmp_path / "command-ffff.json"
    art.write_text("{}")
    stale = Path(str(art) + ".lock")
    stale.write_text("99999\n")
    with pytest.raises(LockHeld) as exc_info, exclusive_lock(art):
        pass
    # Sentinel pid recovered from the stale file.
    assert exc_info.value.holder_pid == 99999  # noqa: PLR2004 — sentinel constant


def test_degraded_path_holder_pid_none_when_unparseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lockfile, "_is_posix", lambda: False)
    art = tmp_path / "command-gggg.json"
    art.write_text("{}")
    stale = Path(str(art) + ".lock")
    stale.write_text("not-a-pid")
    with pytest.raises(LockHeld) as exc_info, exclusive_lock(art):
        pass
    assert exc_info.value.holder_pid is None


# Keep import referenced for linters.
_ = time
