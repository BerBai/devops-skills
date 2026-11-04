"""Shared loaders for the synced ssh-core 1.4.0 test suite."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"


if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_script(name: str):
    """Import ``scripts/<name>.py`` without importing across plugin packages."""
    cached = sys.modules.get(f"sshcore_test_{name}")
    if cached is not None:
        return cached
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"sshcore_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
