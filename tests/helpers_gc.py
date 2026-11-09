"""Loader for the ssh-guarded ``lib`` package in tests.

``lib`` is a per-plugin package name (ssh-core, iac-state, k8s-debug,
log-aggregator and ssh-guarded each ship one). Importing it bare from a test
file would pick whichever copy happened to be importable first, so the gc tests
load ssh-guarded's copy explicitly and hand it back.

``load_state`` does the same for ``lib.state``, which owns the reports-root
resolution the GAP-006 tests assert on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARDED_SCRIPTS = (
    REPO_ROOT / "plugins" / "ssh-guarded" / "skills" / "ssh-guarded" / "scripts"
)

_PKG_NAME = "ssh_guarded_lib"


def _load_package() -> ModuleType:
    """Import ssh-guarded's ``lib`` under a private name."""
    if _PKG_NAME in sys.modules:
        return sys.modules[_PKG_NAME]
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        GUARDED_SCRIPTS / "lib" / "__init__.py",
        submodule_search_locations=[str(GUARDED_SCRIPTS / "lib")],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


def load_gc() -> ModuleType:
    """Import ssh-guarded's ``lib.gc`` without disturbing the global ``lib``."""
    cached = sys.modules.get("ssh_guarded_lib_gc")
    if cached is not None:
        return cached

    _load_package()
    gc = sys.modules[f"{_PKG_NAME}.gc"]
    sys.modules["ssh_guarded_lib_gc"] = gc
    return gc


def load_state() -> ModuleType:
    """Import ssh-guarded's ``lib.state`` (reports-root resolution)."""
    cached = sys.modules.get("ssh_guarded_lib_state")
    if cached is not None:
        return cached

    _load_package()
    state = sys.modules[f"{_PKG_NAME}.state"]
    sys.modules["ssh_guarded_lib_state"] = state
    return state
