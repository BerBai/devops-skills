"""Small compatibility shims so the suite runs on the declared Python floor.

``tempfile.TemporaryDirectory(ignore_cleanup_errors=True)`` is **3.10+**
(FA-08). The suite used it in 54 places, which silently made the "CI leg on
3.8" claim false: every one of those tests raised ``TypeError`` on 3.8 while
the shipped scripts were fine.

:func:`temp_dir` passes the keyword only when the running interpreter supports
it and swallows the cleanup error otherwise, so the observable behaviour is the
same from 3.8 upwards. :func:`write_text` does the same job for
``Path.write_text(..., newline="")``, which is also 3.10+.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

#: True when ``TemporaryDirectory`` understands ``ignore_cleanup_errors``.
HAVE_IGNORE_CLEANUP_ERRORS = sys.version_info >= (3, 10)


class _PortableTemporaryDirectory:
    """``TemporaryDirectory`` with best-effort cleanup on older Pythons."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._ignore = bool(kwargs.pop("ignore_cleanup_errors", False))
        if HAVE_IGNORE_CLEANUP_ERRORS:
            # floor-ok: the guard above is the whole point of this shim, which
            # exists so the suite itself runs on 3.8 (FA-08).
            self._impl: Any = tempfile.TemporaryDirectory(
                *args, **kwargs, ignore_cleanup_errors=self._ignore
            )
        else:
            self._impl = tempfile.TemporaryDirectory(*args, **kwargs)

    def __enter__(self) -> str:
        return self._impl.__enter__()

    def __exit__(self, *exc: Any) -> None:
        if HAVE_IGNORE_CLEANUP_ERRORS:
            self._impl.__exit__(*exc)
            return
        try:
            self._impl.__exit__(*exc)
        except OSError:
            # Same contract as ignore_cleanup_errors=True: a file still open on
            # Windows must not turn a passing assertion into an error.
            if not self._ignore:
                raise

    def cleanup(self) -> None:
        if HAVE_IGNORE_CLEANUP_ERRORS:
            self._impl.cleanup()
            return
        try:
            self._impl.cleanup()
        except OSError:
            if not self._ignore:
                raise

    @property
    def name(self) -> str:
        return self._impl.name


def temp_dir(*args: Any, **kwargs: Any) -> _PortableTemporaryDirectory:
    """Drop-in replacement for ``tempfile.TemporaryDirectory``."""
    return _PortableTemporaryDirectory(*args, **kwargs)


def write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` with LF endings on any supported Python."""
    with open(path, "w", encoding=encoding, newline="") as handle:
        handle.write(text)


__all__ = ["HAVE_IGNORE_CLEANUP_ERRORS", "temp_dir", "write_text"]
