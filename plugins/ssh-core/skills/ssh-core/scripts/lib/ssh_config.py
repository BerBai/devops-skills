"""ssh_config.py — small, dependency-free helpers for ``~/.ssh/config`` edits.

``ssh_config_manager.py`` owns the full CRUD surface (subcommands, backups,
JSON envelopes), but two call sites need the *same* safe edit without
spawning it::

* ``deploy_pubkey.py`` — after a successful key install, flip the alias from
  ``# auth: password`` to ``# auth: key`` and add ``IdentityFile`` +
  ``IdentitiesOnly yes``, so the next ``ssh_execute.py`` uses the key.
* future callers that want to adopt key auth for one alias.

Doing it here (instead of shelling out) keeps the file edit atomic, keeps the
backup convention, and avoids re-parsing the whole document twice. The
parsing itself is delegated to ``ssh_config_manager.parse_config`` — the
module is imported by path because the scripts directory is not a package.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


def _load_config_manager(scripts_dir: Path) -> ModuleType:
    """Import ``ssh_config_manager.py`` by path (once, cached in sys.modules)."""
    name = "ssh_core_config_manager"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = scripts_dir / "ssh_config_manager.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover — broken install
        raise ImportError(f"cannot import ssh_config_manager from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class AdoptOutcome:
    """Result of :func:`adopt_key_auth`."""

    changed: bool
    alias_known: bool
    changed_fields: list[str]
    backup_path: str | None = None
    message: str = ""


def adopt_key_auth(
    alias: str,
    identity_file: str,
    *,
    ssh_config: str | None = None,
    scripts_dir: Path | None = None,
) -> AdoptOutcome:
    """Point ``alias`` at key auth: ``# auth: key`` + ``IdentityFile``.

    Behaviour:

    * Unknown alias in a *missing* config file: nothing is written (we never
      create ``~/.ssh/config`` behind the user's back) and
      ``alias_known=False``.
    * Unknown alias in an *existing* config: nothing is written — adding a
      ``Host`` block without ``HostName``/``User`` would be guesswork.
    * ``# auth: password`` or ``interactive``: flipped to ``key``, and a
      stale ``IdentityFile`` is replaced by the new one.
    * ``IdentitiesOnly yes`` is added when the block has no explicit
      directive; the manager's renderer already does this for any host with
      an ``IdentityFile``, so re-rendering is enough.
    * Every write goes through ``atomic_write_with_backup`` (``.bak.<ts>``).

    Raises ``OSError`` / ``PermissionError`` from the write, and lets
    ``ssh_config_manager``'s ``ConfigParseError`` propagate: the caller turns
    those into a warning, never into a failed deploy.
    """
    scripts_dir = scripts_dir or Path(__file__).resolve().parent.parent
    module = _load_config_manager(scripts_dir)
    config_path = Path(ssh_config or "~/.ssh/config").expanduser()
    if not config_path.is_file():
        return AdoptOutcome(
            changed=False,
            alias_known=False,
            changed_fields=[],
            message=(
                f"{config_path} does not exist; skipped writing an ssh config "
                f"entry for {alias}"
            ),
        )

    # Same lock the config manager uses: adopting key auth while an agent
    # edits the same file must not lose either write (SSH-03).
    from .configfile import ConfigLock

    with ConfigLock(config_path):
        return _adopt_locked(module, config_path, alias, identity_file)


def _adopt_locked(
    module: ModuleType,
    config_path: Path,
    alias: str,
    identity_file: str,
) -> AdoptOutcome:
    doc = module.parse_config(
        config_path.read_text(encoding="utf-8-sig"), config_path
    )
    host = doc.find(alias)
    if host is None:
        return AdoptOutcome(
            changed=False,
            alias_known=False,
            changed_fields=[],
            message=(
                f"alias {alias} is not in {config_path}; skipped writing an "
                "entry (HostName/User would have to be guessed)"
            ),
        )

    changed: list[str] = []
    if (host.auth or "").lower() != "key":
        host.auth = "key"
        changed.append("auth")
    if host.identity_file != identity_file:
        host.identity_file = identity_file
        changed.append("identity_file")

    if not changed:
        return AdoptOutcome(
            changed=False,
            alias_known=True,
            changed_fields=[],
            message=f"{alias} already uses key auth with {identity_file}",
        )

    backup = module.atomic_write_with_backup(config_path, module.render(doc))
    return AdoptOutcome(
        changed=True,
        alias_known=True,
        changed_fields=changed,
        backup_path=str(backup),
        message=(
            f"{alias} now uses key auth ({identity_file}); "
            f"fields: {','.join(changed)}; backup: {Path(backup).name}"
        ),
    )


def chmod_private(path: Path) -> None:
    """Best-effort 0600 on a secret-ish file (no-op where unsupported)."""
    with contextlib.suppress(OSError):
        path.chmod(0o600)


__all__ = ["AdoptOutcome", "adopt_key_auth", "chmod_private"]
