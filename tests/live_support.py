"""Live-suite guards, one implementation for every integration module.

Before this module each of the 26 ``*_integration`` files carried its own copy
of ``_ssh_agent_has_key()`` / ``_host_reachable()`` (DEVOPS-GAP-010), each with
its own idea of how to find the host, and several with a hardcoded PoC subnet or
a hardcoded ``/tmp/poc-ssh-config``. Changing a guard rule meant editing 26
files, and a machine with a perfectly good ``IdentityFile`` still skipped the
whole suite because no ``ssh-agent`` was running.

The rules now live here:

* the host comes from ``POC_HOST`` and nowhere else -- when it is unset the
  live group skips with a message naming the variable instead of silently
  trying an alias that only exists on one developer's laptop;
* a credential is usable when **either** ``ssh-agent`` holds a key **or**
  ``ssh <alias> true`` succeeds with ``BatchMode=yes`` (which covers
  ``IdentityFile``, ``ProxyJump`` and password-via-askpass hosts);
* reachability is *probed*, never inferred from an IP range;
* capability skips (paramiko, POSIX shell) are markers applied centrally in
  ``pytest_collection_modifyitems`` rather than a guard copy per file;
* the scratch ssh config is *located*, never fabricated -- the two cases that
  read it need stanzas a generator cannot invent, so they skip on the missing
  stanza's name (:func:`require_ssh_config_stanza`) instead of failing on a
  config that only looked plausible;
* ``ssh -G <alias>`` is the authority on what an alias resolves to, so a case
  that must derive a variant config does not hardcode HostName/User again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

#: Environment variable naming the ssh alias the live suite may touch.
POC_HOST_ENV = "POC_HOST"

#: Where the scratch ssh config lives (was a hardcoded ``/tmp/poc-ssh-config``,
#: which is ``C:\tmp\poc-ssh-config`` on Windows). It is read, never written.
POC_CONFIG_NAME = "poc-ssh-config"


def poc_host() -> str | None:
    """Return the configured live host alias, or ``None``.

    No default is committed: ``pai`` only resolved on the machine that wrote
    the suite, and a missing alias used to look like a product failure.
    """
    value = os.environ.get(POC_HOST_ENV, "").strip()
    return value or None


def have_binary(name: str) -> bool:
    return shutil.which(name) is not None


def has_paramiko() -> bool:
    """True when the paramiko SFTP backend can actually be imported.

    Guards the ``sftp_only`` cases. A machine without paramiko is a supported
    configuration (scp/tar/rsync fallbacks), not a broken one.
    """
    try:
        import paramiko  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install counts as "no"
        return False
    return True


def posix_shell_ok() -> bool:
    """True when the suite can rely on POSIX-only facilities here.

    ``setsid``, POSIX permission bits and ``/tmp`` paths are the three things
    that quietly differ on Windows.
    """
    return os.name == "posix"


def resolved_ssh_options(host: str) -> dict[str, str]:
    """Return the options ``ssh -G <host>`` resolved for ``host``.

    The live cases that need a *derived* config (the same destination reached
    through a fresh ``UserKnownHostsFile``) used to hardcode
    ``HostName 192.168.2.115``, ``User root`` and macOS-only directives such as
    ``UseKeychain yes``. That only worked on the machine that wrote it, and on
    Windows the config was rejected outright ("Bad configuration option:
    usekeychain"). Asking the ssh client itself keeps the tests host-agnostic.

    Keys are lower-cased; first occurrence wins (``ssh -G`` repeats
    ``identityfile`` for every identity it would try). An empty dict means
    ``ssh -G`` failed and the caller has no derivation to make.
    """
    if not have_binary("ssh"):
        return {}
    try:
        proc = subprocess.run(
            ["ssh", "-G", host],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    if proc.returncode != 0:
        return {}
    options: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition(" ")
        if not sep:
            continue
        options.setdefault(key.lower(), value.strip())
    return options


def ssh_agent_has_key() -> bool:
    """Return True iff ``ssh-add -l`` reports at least one loaded identity."""
    if not have_binary("ssh-add"):
        return False
    try:
        proc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def host_reachable(host: str, timeout: int = 10) -> bool:
    """Probe ``ssh <host> true`` with BatchMode so it can never prompt."""
    if not have_binary("ssh"):
        return False
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "true"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


def live_skip_reason() -> str | None:
    """Return why the live group cannot run here, or ``None`` when it can.

    The message is the whole point: it names the capability that is missing, so
    a skip never reads like a silent product bug. A usable ``IdentityFile`` in
    ``~/.ssh/config`` is enough -- ``ssh-agent`` is optional.
    """
    host = poc_host()
    if host is None:
        return (
            f"{POC_HOST_ENV} is not set; the live suite needs an ssh alias it is "
            "allowed to touch (e.g. POC_HOST=lab-240 pytest -m live_ssh)"
        )
    if not have_binary("ssh"):
        return "the OpenSSH client ('ssh') is not on PATH"
    if ssh_agent_has_key():
        if not host_reachable(host):
            return f"host '{host}' is not reachable (ssh {host} true failed)"
        return None
    if host_reachable(host):
        return None
    return (
        f"no usable credential for '{host}': ssh-agent holds no identity and "
        "`ssh -o BatchMode=yes <alias> true` failed. Load a key with `ssh-add`, "
        "or configure IdentityFile for the alias in ~/.ssh/config."
    )


def require_live_ssh() -> None:
    """``pytest.skip`` unless the live group can run; import pytest lazily."""
    import pytest

    reason = live_skip_reason()
    if reason is not None:
        pytest.skip(reason)


def poc_ssh_config() -> Path:
    """Return the path of the scratch ssh config the live suite may read.

    It is deliberately **not** generated any more (GAP-002). The two cases that
    need it require stanzas no generator can invent:

    * ``Host poc-via-agent`` -- a host with *no* ``IdentityFile``, so the only
      possible credential is the ssh-agent identity;
    * ``Host poc-noauth`` -- ``IdentityFile /dev/null`` + ``IdentitiesOnly
      yes``, i.e. a guaranteed publickey denial.

    A fabricated ``Host <alias> / HostName <alias>`` block satisfied neither,
    yet made the cases look runnable: they failed with what read like a product
    error instead of skipping for a precisely named missing stanza. Those cases
    now call :func:`require_ssh_config_stanza`; this function only locates the
    file, so ``POC_CONFIG`` stays a plain path.
    """
    return Path(tempfile.gettempdir()) / POC_CONFIG_NAME


def ssh_config_stanzas(path: Path) -> set[str]:
    """Return the ``Host`` patterns declared in an ssh config file.

    Best-effort and dependency-free: ``Host a b`` yields ``a`` and ``b``;
    comments and ``Match`` blocks are ignored. Wildcards are kept verbatim, so
    an exact-match lookup only succeeds when someone really wrote that stanza.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    stanzas: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        if keyword.lower() != "host":
            continue
        stanzas.update(rest.split())
    return stanzas


def require_ssh_config_stanza(stanza: str) -> Path:
    """``pytest.skip`` naming the missing ``Host`` stanza, else return the path.

    The skip message has to say *which stanza and why*, because "config file
    missing" points the reader at the wrong thing: on this machine the file may
    exist and still not define the host the case needs.
    """
    import pytest

    path = poc_ssh_config()
    if stanza in ssh_config_stanzas(path):
        return path
    pytest.skip(
        f"the scratch ssh config {path} has no `Host {stanza}` stanza; this "
        f"case needs a hand-written one (see tests/README.md) because "
        f"'{stanza}' models a credential shape no generated config can express"
    )


def run_envelope(script: Path, *args: str, timeout: int = 60) -> tuple[dict[str, Any], str]:
    """Run a shipped CLI with ``--json`` and return ``(envelope, stderr)``.

    Shared by the integration modules so they all fail the same readable way
    when a script prints prose instead of its envelope.
    """
    import json
    import sys

    import pytest

    argv = [sys.executable, str(script), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False,
        encoding="utf-8", errors="replace",
    )
    try:
        return json.loads(proc.stdout), proc.stderr
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{script.name} produced non-JSON stdout: {exc}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


__all__ = [
    "POC_CONFIG_NAME",
    "POC_HOST_ENV",
    "has_paramiko",
    "have_binary",
    "host_reachable",
    "live_skip_reason",
    "poc_host",
    "poc_ssh_config",
    "posix_shell_ok",
    "require_live_ssh",
    "require_ssh_config_stanza",
    "resolved_ssh_options",
    "run_envelope",
    "ssh_agent_has_key",
    "ssh_config_stanzas",
]
