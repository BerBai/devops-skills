#!/usr/bin/env python3
"""doctor.py - prove what this machine can actually DO, not just what it has.

``selfcheck.py`` answers "which tools are installed". That was not enough: the
review that produced this script (A3) got

    paramiko: missing / degraded
    scp: ok
    ...

and still had to discover, one failed call at a time, that ``--resume``,
``--bwlimit``, ``--delete`` and ``--preserve`` were unavailable, that password
auth needed ``SSH_CORE_ASKPASS_DIR``, and that the picky host rejected the
key it had. Every line was true and none of it answered the question.

``doctor.py`` runs the capability instead of looking for the binary:

* **local** probes: the interpreter, ``ssh``, ``scp``, ``sftp``, ``rsync``,
  ``tar``, paramiko, a *staged* askpass helper (create + read back + delete),
  the audit directory, the config file, ControlMaster support, and whether
  every script answers ``--version``;
* with ``--host`` (or ``--hosts a,b``): one real **connect** per routing mode
  (key, password), one real **upload/download round trip** with a sha256 check
  through the transport that would actually be used, an optional **sudo**
  probe, and a TLS-free **port probe** that does not need ssh at all.

A second profile covers the rest of the marketplace (OPT-001). ssh-core is one
of eight plugins here; the other seven need backends that ssh-core never
touches (``docker``/``podman``, ``kubectl``/``helm``, ``terraform``/``tofu``,
``gh``/``glab``, the ``yaml``/``hcl2`` Python modules, and
``journalctl``/``tail``/``nc`` on the log plugins). ``--profile full`` probes
those too and prints one readiness line per plugin:

    remote-debug   OK        journalctl 239 (lab-240), tail (lab-240), nc optional
    k8s-debug      DEGRADED  kubectl MISSING, helm MISSING
    docker-quick   DEGRADED  docker|podman MISSING

With ``--host`` the remote side is probed in **one** ssh round trip (a single
batched ``command -v`` + ``--version`` loop), never one ssh per binary. The
machine-readable answer is ``data.capabilities`` (one entry per plugin
requirement, with ``present`` and a cheap ``version``) plus
``data.plugin_readiness``; both only appear under ``full``, so the default
``transport`` profile stays byte-compatible with 1.5.

Each probe returns ``{name, ok, status, detail, failure_class, elapsed_ms}``,
so the output is both human-readable and machine-checkable::

    python scripts/doctor.py                       # local only
    python scripts/doctor.py lab-240 --json        # + remote probes
    python scripts/doctor.py --hosts a,b --no-transfer   # connect only
    python scripts/doctor.py --require password    # exit 9 unless it works
    python scripts/doctor.py --profile full --host lab-240   # all 8 plugins

Exit codes: 0 when every ``--require`` capability passed (and nothing required
failed), 9 (verify failed) when a required capability does not work, 2 on argv
errors.

Nothing here writes to ``~/.ssh`` and nothing prints a password: the round trip
uses a random 1-4 KiB blob and deletes both copies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Keep the tree free of __pycache__/*.pyc (see the other scripts).
sys.dont_write_bytecode = True
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib import CHILD_TEXT_KWARGS, msys_safe_env  # noqa: E402
from lib.askpass import (  # noqa: E402
    ASKPASS_DIR_ENV,
    ASKPASS_HOME_ENV,
    AskpassUnavailable,
    askpass_env,
)
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_INTERNAL,
    EXIT_INVALID_ARGUMENTS,
    EXIT_OK,
    EXIT_VERIFY_FAILED,
)
from lib.transfer import paramiko_available  # noqa: E402
from lib.version import VERSION_LINE, __version__  # noqa: E402

#: Scripts that must answer ``--version``. Kept in step with selfcheck.py.
SCRIPT_FILES: tuple[str, ...] = (
    "ssh_execute.py",
    "ssh_session.py",
    "ssh_cluster.py",
    "ssh_daemon.py",
    "ssh_tunnel.py",
    "ssh_upload.py",
    "ssh_download.py",
    "ssh_server_transfer.py",
    "deploy_pubkey.py",
    "ssh_config_manager.py",
    "audit.py",
    "selfcheck.py",
    "doctor.py",
    "ssh_slice.py",
)

#: Probe name → the word a caller passes to ``--require``.
REQUIREMENTS: dict[str, str] = {
    "exec_key": "run a command with key auth",
    "exec_password": "run a command with password auth",
    "transfer": "move a file in both directions",
    "sftp": "SFTP backend (resume/bwlimit/preserve/delete)",
    "sudo": "escalate with `sudo -n`",
    "askpass": "stage the SSH_ASKPASS helper",
    "ssh": "the OpenSSH client",
    "paramiko": "the paramiko module",
    "rsync": "rsync",
    "tar": "tar",
    "scp": "scp",
    "config": "a readable ~/.ssh/config",
}

#: ``--profile`` values. ``transport`` is ssh-core's own surface (1.5 output,
#: unchanged); ``full`` adds the other seven plugins' backends (OPT-001).
PROFILES: tuple[str, ...] = ("transport", "full")


@dataclass(frozen=True)
class PluginBackend:
    """What one plugin in this marketplace needs beyond ssh-core itself.

    ``needs`` is an **any-of** group: docker-quick auto-detects
    ``docker`` then ``podman``, iac-state resolves ``tofu`` then ``terraform``,
    and cicd-debug speaks either provider, so one present member is enough.
    ``also`` is a list that must be fully present; ``modules`` are Python
    imports resolved on *this* interpreter (they cannot be satisfied by the
    remote host, because the parsing happens locally). ``optional`` never
    changes readiness: it is reported so "works, but this path is unavailable"
    is visible instead of being discovered at the first call.
    """

    plugin: str
    needs: tuple[str, ...] = ()
    also: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    note: str = ""


#: Every plugin shipped in this repository, and the backends it needs.
#:
#: ssh-core is the transport every other plugin delegates to; ssh-guarded adds
#: nothing but the ssh-core scripts themselves. The six diagnostic plugins are
#: the reason this table exists: each one used to require a manual ``command
#: -v`` on every target before a report could say "environment not available"
#: rather than "verified" (OPT-001).
PLUGIN_BACKENDS: tuple[PluginBackend, ...] = (
    PluginBackend(
        plugin="ssh-core",
        also=("ssh",),
        optional=("scp", "sftp", "ssh-keygen", "rsync", "tar"),
        note="transport every other plugin delegates to",
    ),
    PluginBackend(
        plugin="ssh-guarded",
        also=("ssh",),
        note="approval/audit layer over ssh-core; no extra backend",
    ),
    PluginBackend(
        plugin="remote-debug",
        also=("ssh",),
        optional=("journalctl", "tail", "nc"),
        note="tail/journalctl on the target; nc only for port_check",
    ),
    PluginBackend(
        plugin="k8s-debug",
        also=("ssh", "kubectl", "helm"),
        note="kubectl for pods/cluster, helm for releases",
    ),
    PluginBackend(
        plugin="docker-quick",
        needs=("docker", "podman"),
        also=("ssh",),
        note="one container runtime, local or on the target",
    ),
    PluginBackend(
        plugin="iac-state",
        needs=("terraform", "tofu"),
        also=("ssh",),
        modules=("hcl2",),
        note="terraform/tofu for state+plan, python-hcl2 for module_validate",
    ),
    PluginBackend(
        plugin="cicd-debug",
        needs=("gh", "glab"),
        also=("ssh",),
        modules=("yaml",),
        note="gh or glab for the provider API, PyYAML for workflow files",
    ),
    PluginBackend(
        plugin="log-aggregator",
        also=("ssh",),
        optional=("journalctl", "tail", "docker", "kubectl"),
        note="journal:/tail:/docker:/kube: sources; file:// works locally",
    ),
)

#: Python module → distribution name (for the version and the install hint).
PYTHON_MODULE_DIST: dict[str, str] = {
    "yaml": "PyYAML",
    "hcl2": "python-hcl2",
}

#: Every backend binary any plugin may need, probed on the target in one batch.
REMOTE_BACKENDS: tuple[str, ...] = (
    "docker",
    "podman",
    "kubectl",
    "helm",
    "terraform",
    "tofu",
    "gh",
    "glab",
    "journalctl",
    "tail",
    "nc",
    "python3",
    "rsync",
)

#: Marker the remote batch prints so a banner or a stray login message cannot
#: be mistaken for a probe row.
_REMOTE_MARKER = "__SSHCORE_BACKEND__"


@dataclass
class BackendStatus:
    """One probed backend binary or Python module."""

    name: str
    kind: str                     # "binary" | "python"
    scope: str = "local"          # "local" | "remote"
    present: bool = False
    version: str | None = None
    path: str | None = None
    detail: str = ""
    #: Set when the *probe itself* failed (the target is unreachable), which is
    #: different from "the target answered and the binary is not installed":
    #: the first cannot be a green light, the second can be a legitimate gap.
    probe_error: str | None = None

    @property
    def label(self) -> str:
        """``docker 24.0.7`` / ``docker (lab-240)`` / ``docker``."""
        if self.present and self.version:
            return f"{self.name} {self.version}"
        if self.present and self.scope == "remote":
            return f"{self.name} (remote)"
        return self.name

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "scope": self.scope,
            "present": self.present,
            "version": self.version,
            "path": self.path,
            "detail": self.detail,
        }
        if self.probe_error:
            payload["probe_error"] = self.probe_error
        return payload


@dataclass
class Probe:
    """One capability check and its outcome."""

    name: str
    ok: bool
    status: str = "ok"          # ok | missing | failed | skipped | degraded
    detail: str = ""
    failure_class: str | None = None
    elapsed_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "failure_class": self.failure_class,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.extra:
            payload["extra"] = self.extra
        return payload


def _timed(fn, *args, **kwargs) -> tuple[Any, int]:
    started = time.monotonic()
    value = fn(*args, **kwargs)
    return value, int((time.monotonic() - started) * 1000)


# --- local probes ------------------------------------------------------------


def probe_tool(name: str) -> Probe:
    path = shutil.which(name)
    if path:
        return Probe(name, True, detail=path)
    return Probe(
        name, False, status="missing",
        detail=f"{name} is not on PATH",
        failure_class="dependency_missing",
    )


def probe_ssh_version() -> Probe:
    try:
        proc = subprocess.run(
            ["ssh", "-V"], capture_output=True, text=True, timeout=15,
            check=False, env=msys_safe_env(), **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Probe("ssh", False, status="missing", detail=str(exc),
                     failure_class="dependency_missing")
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    return Probe("ssh", True, detail=text[0] if text else "ssh present")


def probe_paramiko() -> Probe:
    if not paramiko_available():
        return Probe(
            "paramiko", False, status="degraded",
            detail=(
                "not installed: --resume, --bwlimit, --chmod, --preserve and "
                "--delete fall back to scp (refused with exit 166); install "
                "with `pip install 'paramiko>=3.0,<4'`"
            ),
            failure_class="dependency_missing",
        )
    try:
        import paramiko  # noqa: F401

        version = getattr(paramiko, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        version = "unknown"
    return Probe("paramiko", True, detail=f"paramiko {version}")


def probe_askpass() -> Probe:
    """Stage a real helper and read the secret back (review A2).

    Presence of the module proves nothing; the failure the review hit was a
    *writable directory*, so this writes to one and cleans up. It never uses a
    real password — the value is random and thrown away.
    """
    secret = hashlib.sha256(os.urandom(16)).hexdigest()[:24]
    try:
        with askpass_env(secret) as env:
            helper = Path(env["SSH_ASKPASS"])
            if not helper.is_file():
                return Probe("askpass", False, status="failed",
                             detail="helper was not created",
                             failure_class="askpass_unavailable")
            work = helper.parent
            secret_file = work / "secret"
            if secret_file.read_text(encoding="utf-8") != secret:
                return Probe("askpass", False, status="failed",
                             detail="the staged secret did not round-trip",
                             failure_class="askpass_unavailable")
            return Probe(
                "askpass", True,
                detail=f"staged at {work} (SSH_ASKPASS={env['SSH_ASKPASS']})",
            )
    except AskpassUnavailable as exc:
        return Probe(
            "askpass", False, status="failed",
            detail=(
                f"{exc}\n  Every candidate was denied. Set {ASKPASS_DIR_ENV} "
                f"or {ASKPASS_HOME_ENV} to a private writable directory."
            ),
            failure_class="askpass_unavailable",
            extra={"tried": exc.tried},
        )
    except OSError as exc:
        return Probe("askpass", False, status="failed", detail=str(exc),
                     failure_class="askpass_unavailable")


def probe_ssh_config() -> Probe:
    path = Path("~/.ssh/config").expanduser()
    if not path.exists():
        return Probe(
            "config", True, status="skipped",
            detail=f"{path} does not exist (ad-hoc user@host still works)",
        )
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return Probe("config", False, status="failed", detail=str(exc),
                     failure_class="permission")
    hosts = 0
    try:
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if line.strip().lower().startswith("host "):
                hosts += 1
    except OSError:
        pass
    return Probe("config", True, detail=f"{path} ({hosts} Host blocks)")


def probe_control_master() -> Probe:
    """POSIX only: ControlPath needs a unix socket (documented, not a bug)."""
    from lib import is_windows

    if is_windows():
        return Probe(
            "control_master", False, status="skipped",
            detail="not supported by OpenSSH for Windows; use --commands-file",
        )
    return Probe("control_master", True, detail="available (POSIX sockets)")


def probe_scripts() -> Probe:
    bad: list[str] = []
    for name in SCRIPT_FILES:
        path = SCRIPTS_DIR / name
        if not path.is_file():
            bad.append(f"{name}: missing")
            continue
        try:
            proc = subprocess.run(
                [sys.executable, str(path), "--version"],
                capture_output=True, text=True, timeout=60, check=False,
                env=msys_safe_env(), **CHILD_TEXT_KWARGS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            bad.append(f"{name}: {exc}")
            continue
        if proc.returncode != 0 or __version__ not in (proc.stdout or ""):
            bad.append(f"{name}: rc={proc.returncode}")
    total = len(SCRIPT_FILES)
    if bad:
        return Probe("scripts", False, status="failed",
                     detail=f"{total - len(bad)}/{total} answered: " + "; ".join(bad),
                     failure_class="verify_failed")
    return Probe("scripts", True, detail=f"{total}/{total} scripts answer --version")


def local_probes() -> list[Probe]:
    probes: list[Probe] = [
        Probe("python", True, detail=f"{sys.version.split()[0]} at {sys.executable}"),
    ]
    value, elapsed = _timed(probe_ssh_version)
    value.elapsed_ms = elapsed
    probes.append(value)
    for tool in ("scp", "sftp", "ssh-keygen", "rsync", "tar"):
        value, elapsed = _timed(probe_tool, tool)
        value.elapsed_ms = elapsed
        probes.append(value)
    for probe_fn in (probe_paramiko, probe_askpass, probe_ssh_config,
                     probe_control_master, probe_scripts):
        value, elapsed = _timed(probe_fn)
        value.elapsed_ms = elapsed
        probes.append(value)
    return probes


# --- capability probes (--profile full, OPT-001) ------------------------------


def _run_version(argv: list[str], timeout: int = 10) -> str:
    """First line of ``<binary> <version flags>``, or "" when it cannot run.

    Only ever used for the *cheap* half of a capability row: a version string
    is nice to print, but ``present`` is decided by the binary being on PATH
    (or by ``command -v`` on the remote side), never by this succeeding.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL, env=msys_safe_env(), **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0].strip() if text else ""


#: A version token: ``24.0.7``, ``v1.31.0``, ``243``. Build metadata
#: (``+gd3f6a8b``, ``-rc1``) is stripped before matching, so Helm's
#: ``v3.14.0+gd3f6a8b`` reports ``3.14.0`` rather than ``None``.
_VERSION_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _short_version(text: str) -> str | None:
    """Pick the version token out of a CLI banner.

    Position, not the program name, is the reliable signal: ``kubectl`` prints
    ``Client Version: v1.31.0`` while ``docker`` prints
    ``Docker version 24.0.7, build ...``, and a prefix filter keyed on the
    binary name silently lost the first. The first token that *looks* like a
    version after the leading word wins, which is also what ``helm`` /
    ``terraform`` / ``tail (GNU coreutils) 8.32`` need.
    """
    for line in text.splitlines():
        tokens = line.replace(",", " ").replace(":", " ").split()
        for index, token in enumerate(tokens):
            cleaned = token.strip("()[]")
            # Skip the bare leading word: it is the program's own name, which
            # can look numeric-ish (`nc`). Two exceptions keep a version-only
            # banner readable: a ``v`` prefix (``helm version --short`` ->
            # ``v3.14.0+gd3f6a8b``) and a line that is just the number.
            if index == 0 and len(tokens) > 1 and not re.match(r"^[vV]\d", cleaned):
                continue
            cleaned = cleaned.lstrip("vV").split("+", 1)[0]
            if _VERSION_TOKEN_RE.match(cleaned):
                return cleaned
    return None


#: ``binary`` → the argv that prints its version. Only the cheap flags: the
#: presence answer never depends on this succeeding, and a tool that hangs or
#: exits non-zero on ``--version`` must not turn into a missing backend.
VERSION_COMMANDS: dict[str, tuple[str, ...]] = {
    "docker": ("--version",),
    "podman": ("--version",),
    "kubectl": ("version", "--client"),
    "helm": ("version", "--short"),
    "terraform": ("version",),
    "tofu": ("version",),
    "gh": ("--version",),
    "glab": ("--version",),
    "journalctl": ("--version",),
    "tail": ("--version",),
    "nc": ("-h",),
    "python3": ("--version",),
    "rsync": ("--version",),
}


def _probe_binary_local(name: str) -> BackendStatus:
    path = shutil.which(name)
    if not path:
        return BackendStatus(
            name=name, kind="binary", present=False,
            detail=f"{name} is not on PATH",
        )
    flags = VERSION_COMMANDS.get(name, ("--version",))
    version = _short_version(_run_version([name, *flags]))
    return BackendStatus(
        name=name, kind="binary", present=True, version=version, path=path,
        detail=path,
    )


def _probe_module_local(name: str) -> BackendStatus:
    """Import a module in a *child* interpreter.

    Deliberately not ``importlib.util.find_spec`` in-process: doctor must
    report what a plugin script will see when it runs as ``python script.py``,
    and several of these plugins are loaded with their own ``sys.path``
    manipulation. A child is also immune to a half-imported module in this
    process.
    """
    script = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.find_spec({name!r})\n"
        "version = None\n"
        "if spec is not None:\n"
        "    try:\n"
        f"        from importlib.metadata import version as _v\n"
        f"        version = _v({PYTHON_MODULE_DIST.get(name, name)!r})\n"
        "    except Exception:\n"
        "        version = None\n"
        "print(json.dumps({'found': spec is not None, 'version': version,\n"
        "                  'origin': getattr(spec, 'origin', None)}))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=30, check=False, stdin=subprocess.DEVNULL,
            env=msys_safe_env(), **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return BackendStatus(
            name=name, kind="python", present=False, detail=str(exc),
        )
    try:
        payload = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError, TypeError):
        return BackendStatus(
            name=name, kind="python", present=False,
            detail=f"probe failed (rc={proc.returncode})",
        )
    if not payload.get("found"):
        dist = PYTHON_MODULE_DIST.get(name, name)
        return BackendStatus(
            name=name, kind="python", present=False,
            detail=f"module {name} is not importable (pip install '{dist}')",
        )
    return BackendStatus(
        name=name, kind="python", present=True,
        version=payload.get("version"), path=payload.get("origin"),
        detail=payload.get("origin") or "importable",
    )


def _probe_binaries_local(names: tuple[str, ...]) -> dict[str, BackendStatus]:
    return {name: _probe_binary_local(name) for name in names}


def _probe_modules_local(names: tuple[str, ...]) -> dict[str, BackendStatus]:
    return {name: _probe_module_local(name) for name in names}


#: Shell snippet that probes every backend in ONE round trip.
#:
#: Each row is ``<marker> <name>|<path>|<version line>`` written with ``echo``
#: and a literal marker, so no printf format and no quoting of a tool's own
#: banner can break the batch (a broken format string is exactly how the first
#: cut reported "syntax error: unexpected end of file" for every binary). A
#: missing binary emits ``<marker> <name>|`` — an unambiguously empty path.
def _remote_backend_command(names: tuple[str, ...]) -> str:
    statements = []
    for name in names:
        version_argv = VERSION_COMMANDS.get(name, ("--version",))
        version_cmd = " ".join([name, *version_argv])
        statements.append(
            f"if command -v {name} >/dev/null 2>&1; then "
            f'echo "{_REMOTE_MARKER} {name}|$(command -v {name})|'
            f'$({version_cmd} 2>/dev/null | head -n 1)"; '
            f'else echo "{_REMOTE_MARKER} {name}|"; fi'
        )
    return "; ".join(statements)


def _parse_remote_backends(
    stdout: str, names: tuple[str, ...]
) -> dict[str, BackendStatus]:
    """Turn the batch output into one status per requested backend.

    Returns one entry per requested name even when the answer is empty, so a
    caller never has to distinguish "absent from the dict" from "not present
    on the target".
    """
    found: dict[str, BackendStatus] = {
        name: BackendStatus(
            name=name, kind="binary", scope="remote", present=False,
            detail="not installed on the target",
        )
        for name in names
    }
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith(_REMOTE_MARKER):
            continue
        rest = line[len(_REMOTE_MARKER):].strip()
        name, sep, tail = rest.partition("|")
        name = name.strip()
        if not sep or name not in found:
            continue
        path, _sep, version_text = tail.partition("|")
        path = path.strip()
        if not path:
            continue
        version = (
            _short_version(version_text.strip()) if version_text.strip() else None
        )
        found[name] = BackendStatus(
            name=name, kind="binary", scope="remote", present=True,
            version=version, path=path, detail=path,
        )
    return found


def probe_remote_backends(
    alias: str, args: argparse.Namespace, names: tuple[str, ...] = REMOTE_BACKENDS,
) -> dict[str, BackendStatus]:
    """Probe every remote backend in **one** ssh round trip (OPT-001).

    One ``ssh_execute`` call runs the whole batched ``command -v`` loop: N
    binaries cost one handshake, not N. The route is the same one every other
    probe uses (``--auth`` / ``--ssh-config`` / ``--user`` / key or password),
    so a failure here is already classified.
    """
    if not names:
        return {}
    argv = [
        str(_ssh_execute()), alias, _remote_backend_command(names),
        "--auth", args.modes[0], "--timeout", str(max(args.timeout, 15)),
    ]
    if args.ssh_config:
        argv += ["--ssh-config", args.ssh_config]
    if args.user:
        argv += ["--user", args.user]
    if args.accept_new_hostkey:
        argv.append("--accept-new-hostkey")
    payload, _elapsed = _run_worker(argv, max(args.timeout, 15) + 30)
    if not payload.get("success"):
        failure = (payload.get("data") or {}).get("failure_class") or "remote_error"
        stderr = (payload.get("stderr") or "").strip().splitlines()
        detail = stderr[-1] if stderr else failure
        # A failed probe is NOT "the backend is absent": the target never
        # answered. Recording it lets readiness say "unknown" instead of a
        # green row that hides an unreachable host.
        return {
            name: BackendStatus(
                name=name, kind="binary", scope="remote", present=False,
                detail=f"remote probe failed ({failure}): {detail}",
                probe_error=failure,
            )
            for name in names
        }
    return _parse_remote_backends(payload.get("stdout") or "", names)


def remote_probe_failure(
    statuses: dict[str, BackendStatus]
) -> str | None:
    """The transport failure that stopped the remote batch, if there was one."""
    for status in statuses.values():
        if status.probe_error:
            return status.probe_error
    return None


#: Backends a plugin's remote route invokes on the target. Probed and reported
#: for information only: the plugin's readiness is decided by ``needs`` /
#: ``also`` / ``modules``, not by these, because every one of them has a local
#: route or an alternative that makes the plugin usable without it.
_REMOTE_RELEVANT: dict[str, tuple[str, ...]] = {
    "remote-debug": ("journalctl", "tail", "nc"),
    "k8s-debug": ("kubectl", "helm"),
    "docker-quick": ("docker", "podman"),
    "iac-state": ("terraform", "tofu"),
    "cicd-debug": ("gh", "glab"),
    "log-aggregator": ("journalctl", "tail", "docker", "kubectl"),
}


#: Backends that are only ever resolved on *this* machine. ``ssh`` is the
#: obvious one: it is the client doctor uses to reach the target, so the
#: target's own ``ssh`` binary (if any) says nothing about readiness. Python
#: modules are local for the same reason — the parsing happens here.
_LOCAL_ALWAYS: frozenset[str] = frozenset({"ssh"})


@dataclass
class PluginReadiness:
    """One plugin's row: ready, degraded, unknown, and what exactly is missing."""

    plugin: str
    status: str                     # "ok" | "degraded" | "unknown"
    required: list[str]
    missing: list[str] = field(default_factory=list)
    optional_missing: list[str] = field(default_factory=list)
    optional_present: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    note: str = ""
    probe_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plugin": self.plugin,
            "status": self.status,
            "ready": self.status == "ok",
            "required": list(self.required),
            "missing": list(self.missing),
            "optional_missing": list(self.optional_missing),
            "optional_present": list(self.optional_present),
            "present": list(self.present),
            "note": self.note,
        }
        if self.probe_error:
            payload["probe_error"] = self.probe_error
        return payload


def assess_plugins(
    local: dict[str, BackendStatus],
    remote: dict[str, BackendStatus] | None = None,
) -> list[PluginReadiness]:
    """Turn backend statuses into per-plugin readiness rows.

    ``needs`` is any-of, ``also`` and ``modules`` are all-of. Python modules
    are always resolved locally (the parsing happens here); binaries prefer the
    *remote* answer when a host was probed and fall back to the local one, so a
    plugin whose route runs on this machine still reports correctly.

    ``optional`` never affects ``status``: it is collected separately as
    ``optional_missing`` so "works, but ``port_check`` has no ``nc``" is
    visible without turning a usable plugin red.
    """
    remote = remote or {}
    rows: list[PluginReadiness] = []
    for spec in PLUGIN_BACKENDS:
        relevant = _REMOTE_RELEVANT.get(spec.plugin, ())
        status_of: dict[str, BackendStatus] = {}
        for name in (*spec.needs, *spec.also, *spec.optional, *relevant):
            if name in remote and name not in _LOCAL_ALWAYS:
                status_of[name] = remote[name]
            elif name in local:
                status_of[name] = local[name]
        missing: list[str] = []
        present_labels: list[str] = []

        for name in spec.also:
            status = status_of.get(name)
            if status is not None and status.present:
                present_labels.append(status.label)
            else:
                missing.append(name)
        if spec.needs:
            hits = [
                status_of[name] for name in spec.needs
                if name in status_of and status_of[name].present
            ]
            if hits:
                present_labels.extend(status.label for status in hits)
            else:
                missing.append(" or ".join(spec.needs))
        for module in spec.modules:
            status = local.get(module)
            if status is not None and status.present:
                present_labels.append(
                    f"{module} {status.version}" if status.version
                    else f"{module} (local)"
                )
            else:
                missing.append(f"python:{module}")
        optional_missing = [
            name for name in spec.optional
            if not (status_of.get(name) is not None and status_of[name].present)
        ]
        optional_present = [
            status_of[name].label for name in spec.optional
            if status_of.get(name) is not None and status_of[name].present
        ]
        # "We asked the target and it said no" is degraded. "We could not ask
        # the target" is unknown: a green row there would be a false green, and
        # a red one would blame the plugin for a network outage. Only backends
        # that are actually resolved remotely can make a row unknown — `ssh`
        # itself is always local (it is the client doctor connects with).
        relevant_remote = {
            name: remote[name]
            for name in (*spec.needs, *spec.also, *relevant)
            if name in remote and name not in _LOCAL_ALWAYS
        }
        probe_error = remote_probe_failure(relevant_remote) if remote else None
        if probe_error and not missing:
            status = "unknown"
        else:
            status = "ok" if not missing else "degraded"
        rows.append(PluginReadiness(
            plugin=spec.plugin,
            status=status,
            required=list(spec.needs) + list(spec.also)
            + [f"python:{m}" for m in spec.modules],
            missing=missing,
            optional_missing=sorted(set(optional_missing)),
            optional_present=sorted(set(optional_present)),
            present=sorted(set(present_labels)),
            note=spec.note,
            probe_error=probe_error,
        ))
    return rows


def capability_entries(
    rows: list[PluginReadiness],
    local: dict[str, BackendStatus],
    remote: dict[str, BackendStatus] | None = None,
) -> list[dict[str, Any]]:
    """``data.capabilities``: one machine-readable entry per requirement.

    CI gates on these (``jq -e '.data.capabilities[] | select(.plugin=="k8s-debug") | .ready'``),
    so each row names the plugin, the requirement as written in the docs, the
    scope it was probed in, and whether it is present. ``version`` is only
    filled in where it was cheap to obtain; it is never a gate.
    """
    remote = remote or {}
    entries: list[dict[str, Any]] = []
    for spec in PLUGIN_BACKENDS:
        relevant = _REMOTE_RELEVANT.get(spec.plugin, ())
        groups: list[tuple[str, tuple[str, ...], bool]] = []
        if spec.needs:
            groups.append(("binary_any", spec.needs, True))
        for name in spec.also:
            groups.append(("binary", (name,), True))
        for module in spec.modules:
            groups.append(("python", (module,), True))
        # Probed because the plugin uses it on a target, but never a gate:
        # log-aggregator still works from `file://`, remote-debug still works
        # without `nc` (port_check falls back to /dev/tcp).
        for name in relevant:
            if name in spec.needs or name in spec.also:
                continue
            groups.append(("binary", (name,), False))
        for name in spec.optional:
            if name in spec.needs or name in spec.also or name in relevant:
                continue
            groups.append(("binary", (name,), False))
        seen: set[str] = set()
        for kind, names, required in groups:
            for name in names:
                if name in seen:
                    continue
                seen.add(name)
                status = (
                    local.get(name) if kind == "python" or name in _LOCAL_ALWAYS
                    else remote.get(name) or local.get(name)
                )
                entries.append({
                    "plugin": spec.plugin,
                    "requirement": name,
                    "kind": kind,
                    "scope": status.scope if status is not None else "local",
                    "required": required,
                    "present": bool(status is not None and status.present),
                    "version": status.version if status is not None else None,
                })
    return entries


def render_capabilities(rows: list[PluginReadiness], host: str | None) -> str:
    """The grouped, human half of ``--profile full`` (issue example layout)."""
    width = max(len(row.plugin) for row in rows) + 2 if rows else 16
    lines: list[str] = []
    scope = f"target {host}" if host else "local probes only"
    lines.append(f"  plugin readiness  ({scope})")
    lines.append("")
    for row in rows:
        marker = {
            "ok": "OK", "degraded": "DEGRADED", "unknown": "UNKNOWN",
        }.get(row.status, row.status.upper())
        if row.status == "ok":
            detail = ", ".join(row.present + row.optional_present) or row.note
        elif row.status == "unknown":
            detail = (
                f"remote probe failed ({row.probe_error}); local findings: "
                + (", ".join(row.missing) or "none")
            )
        else:
            detail = ", ".join(f"{item} MISSING" for item in row.missing)
        if row.optional_missing:
            detail += " (optional missing: " + ", ".join(row.optional_missing) + ")"
        lines.append(f"  {row.plugin:<{width}} {marker:<8} {detail}".rstrip())
    ready = [row.plugin for row in rows if row.status == "ok"]
    degraded = [row.plugin for row in rows if row.status == "degraded"]
    unknown = [row.plugin for row in rows if row.status == "unknown"]
    lines.append("")
    lines.append("  ready:    " + (", ".join(ready) or "(none)"))
    if degraded:
        lines.append("  degraded: " + ", ".join(degraded))
    if unknown:
        lines.append("  unknown:  " + ", ".join(unknown))
    return "\n".join(lines).rstrip() + "\n"


def render_full_report(
    probes: list[Probe],
    targets: list[str],
    readiness: dict[str | None, list[PluginReadiness]],
) -> str:
    """``render_human`` plus one readiness block per probed target.

    The transport table is emitted first and byte-identically, so a caller who
    diffs ``--profile transport`` output against ``--profile full`` still sees
    the same section before the new one.
    """
    text = render_human(probes, targets)
    blocks: list[str] = [text]
    if not readiness:
        return text
    for host in ([None] if None in readiness else []) + targets:
        rows = readiness.get(host)
        if not rows:
            continue
        blocks.append(render_capabilities(rows, host))
    return "\n".join(blocks)



# --- remote probes -----------------------------------------------------------


def _ssh_execute() -> Path:
    return SCRIPTS_DIR / "ssh_execute.py"


def _run_worker(argv: list[str], timeout: int) -> tuple[dict[str, Any], int]:
    """Run one of our own scripts and parse its envelope."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, *argv, "--json"],
            capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL, env=msys_safe_env(), **CHILD_TEXT_KWARGS,
        )
    except subprocess.TimeoutExpired:
        return (
            {
                "success": False, "exit_code": 124, "stdout": "", "stderr": "timeout",
                "data": {"failure_class": "timeout"},
            },
            int((time.monotonic() - started) * 1000),
        )
    except (OSError, ValueError) as exc:
        return (
            {
                "success": False, "exit_code": EXIT_INTERNAL, "stdout": "",
                "stderr": str(exc), "data": {"failure_class": "internal"},
            },
            int((time.monotonic() - started) * 1000),
        )
    try:
        payload = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, TypeError):
        payload = {
            "success": False, "exit_code": proc.returncode,
            "stdout": proc.stdout, "stderr": proc.stderr,
            "data": {"failure_class": "ssh_execute_broken"},
        }
    return payload, int((time.monotonic() - started) * 1000)


def probe_exec(
    alias: str,
    *,
    auth: str,
    ssh_config: str | None,
    user: str | None,
    timeout: int,
    accept_new_hostkey: bool,
    label: str,
) -> Probe:
    argv = [
        str(_ssh_execute()), alias, "id -un",
        "--auth", auth, "--timeout", str(timeout),
    ]
    if ssh_config:
        argv += ["--ssh-config", ssh_config]
    if user:
        argv += ["--user", user]
    if accept_new_hostkey:
        argv.append("--accept-new-hostkey")
    payload, elapsed = _run_worker(argv, timeout + 30)
    data = payload.get("data") or {}
    fclass = data.get("failure_class")
    ok = bool(payload.get("success"))
    detail = (
        f"{payload.get('stdout', '').strip() or '(no output)'}"
        if ok
        else (payload.get("stderr") or "").strip().splitlines()[-1]
        if (payload.get("stderr") or "").strip()
        else (fclass or "failed")
    )
    return Probe(
        label, ok,
        status="ok" if ok else ("skipped" if fclass == "user_required" else "failed"),
        detail=detail,
        failure_class=fclass,
        elapsed_ms=elapsed,
        extra={"route": data.get("route"), "transport": data.get("transport")},
    )


def _remote_dir(alias: str, auth: str, ssh_config: str | None,
                user: str | None) -> str:
    return f"/tmp/ssh-core-doctor-{os.getpid()}"


def probe_transfer(
    alias: str,
    *,
    auth: str,
    ssh_config: str | None,
    user: str | None,
    timeout: int,
    accept_new_hostkey: bool,
) -> Probe:
    """Upload a random blob, download it back, compare sha256, clean up.

    This is the probe the review asked for: it exercises the transport that
    would *actually* be used (scp here, paramiko elsewhere), so a green line
    means "files move on this machine", not "scp is installed".
    """
    work = Path(tempfile.mkdtemp(prefix="ssh-core-doctor-"))
    local_src = work / "probe.bin"
    local_back = work / "probe.back"
    blob = os.urandom(4096)
    local_src.write_bytes(blob)
    remote_dir = _remote_dir(alias, auth, ssh_config, user)
    remote_file = f"{remote_dir}/probe.bin"
    common = ["--auth", auth, "--timeout", str(timeout)]
    if ssh_config:
        common += ["--ssh-config", ssh_config]
    if accept_new_hostkey:
        common.append("--accept-new-hostkey")
    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    steps: list[str] = []
    try:
        mk, _ = _run_worker(
            [str(_ssh_execute()), alias, f"mkdir -p {remote_dir}",
             *(["--user", user] if user else []), *common],
            timeout + 30,
        )
        if not mk.get("success"):
            fclass = (mk.get("data") or {}).get("failure_class")
            if fclass == "user_required" or mk.get("exit_code") == 115:
                return Probe(
                    "transfer", False, status="skipped",
                    detail="no login user; pass --user or use user@host",
                    failure_class="user_required",
                    elapsed_ms=_elapsed(),
                )
            return Probe(
                "transfer", False, status="failed",
                detail=(mk.get("stderr") or "").strip()
                or "could not create the probe directory",
                failure_class=fclass,
                elapsed_ms=_elapsed(),
            )
        steps.append("mkdir")
        up, _ = _run_worker(
            [str(SCRIPTS_DIR / "ssh_upload.py"), alias, str(local_src), remote_file,
             *common],
            max(timeout, 60) + 30,
        )
        if not up.get("success"):
            return Probe(
                "transfer", False, status="failed",
                detail=(up.get("stderr") or "").strip().splitlines()[-1],
                failure_class=(up.get("data") or {}).get("failure_class"),
                elapsed_ms=_elapsed(),
                extra={"transport": (up.get("data") or {}).get("transport")},
            )
        steps.append("upload")
        down, _ = _run_worker(
            [str(SCRIPTS_DIR / "ssh_download.py"), alias, remote_file, str(local_back),
             *common],
            max(timeout, 60) + 30,
        )
        if not down.get("success"):
            return Probe(
                "transfer", False, status="failed",
                detail=(down.get("stderr") or "").strip().splitlines()[-1],
                failure_class=(down.get("data") or {}).get("failure_class"),
                elapsed_ms=_elapsed(),
                extra={"transport": (down.get("data") or {}).get("transport")},
            )
        steps.append("download")
        if not local_back.is_file():
            return Probe(
                "transfer", False, status="failed",
                detail="download reported success but the file is missing",
                failure_class="verify_failed",
                elapsed_ms=_elapsed(),
            )
        if local_back.read_bytes() != blob:
            return Probe(
                "transfer", False, status="failed",
                detail="round-trip sha256 differs: the transport corrupted the file",
                failure_class="transfer_verify_failed",
                elapsed_ms=_elapsed(),
            )
        steps.append("sha256")
        transport = (up.get("data") or {}).get("transport")
        return Probe(
            "transfer", True,
            detail=f"round trip ok ({len(blob)} bytes, transport={transport})",
            elapsed_ms=_elapsed(),
            extra={"transport": transport, "steps": steps},
        )
    finally:
        _run_worker(
            [str(_ssh_execute()), alias, f"rm -rf {remote_dir}",
             *(["--user", user] if user else []), *common],
            timeout + 30,
        )
        shutil.rmtree(work, ignore_errors=True)


def probe_sudo(
    alias: str,
    *,
    auth: str,
    ssh_config: str | None,
    user: str | None,
    timeout: int,
) -> Probe:
    argv = [
        str(_ssh_execute()), alias, "id -u", "--sudo", "--auth", auth,
        "--timeout", str(timeout),
    ]
    if ssh_config:
        argv += ["--ssh-config", ssh_config]
    if user:
        argv += ["--user", user]
    payload, elapsed = _run_worker(argv, timeout + 30)
    data = payload.get("data") or {}
    ok = bool(payload.get("success")) and (payload.get("stdout") or "").strip() == "0"
    return Probe(
        "sudo", ok,
        status="ok" if ok else "failed",
        detail=(
            "sudo -n works (uid 0)"
            if ok
            else (payload.get("stderr") or "").strip().splitlines()[-1]
            if (payload.get("stderr") or "").strip()
            else data.get("failure_class", "failed")
        ),
        failure_class=data.get("failure_class"),
        elapsed_ms=elapsed,
    )


def remote_probes(alias: str, args: argparse.Namespace) -> list[Probe]:
    probes: list[Probe] = []
    modes = args.modes
    for mode in modes:
        label = f"exec_{mode}"
        if mode == "password" and not os.environ.get(args.password_env):
            probes.append(Probe(
                label, False, status="skipped",
                detail=f"${args.password_env} is not set; password auth untested",
                failure_class="password_required",
            ))
            continue
        value, elapsed = _timed(
            probe_exec, alias,
            auth=mode, ssh_config=args.ssh_config, user=args.user,
            timeout=args.timeout, accept_new_hostkey=args.accept_new_hostkey,
            label=label,
        )
        value.elapsed_ms = elapsed
        probes.append(value)
    if args.transfer:
        if "password" in modes and not os.environ.get(args.password_env):
            auth = "key"
        elif modes == ["password"]:
            auth = "password"
        else:
            auth = modes[0]
        value, elapsed = _timed(
            probe_transfer, alias,
            auth=auth, ssh_config=args.ssh_config, user=args.user,
            timeout=args.timeout, accept_new_hostkey=args.accept_new_hostkey,
        )
        value.elapsed_ms = elapsed
        probes.append(value)
    if args.sudo:
        value, elapsed = _timed(
            probe_sudo, alias,
            auth=modes[0], ssh_config=args.ssh_config, user=args.user,
            timeout=args.timeout,
        )
        value.elapsed_ms = elapsed
        probes.append(value)
    return probes


# --- report ------------------------------------------------------------------


def render_human(probes: list[Probe], hosts: list[str]) -> str:
    width = max(len(p.name) for p in probes) + 2 if probes else 10
    lines = [f"ssh-core doctor  ({VERSION_LINE})", ""]
    if hosts:
        lines.append(f"  targets: {', '.join(hosts)}")
    else:
        lines.append("  targets: (local only; pass --host ALIAS for live probes)")
    lines.append("")
    for probe in probes:
        marker = {"ok": "OK", "missing": "MISSING", "failed": "FAILED",
                  "skipped": "skipped", "degraded": "DEGRADED"}.get(
                      probe.status, probe.status.upper())
        lines.append(f"  {probe.name:<{width}} {marker:<8} {probe.detail}".rstrip())
    lines.append("")
    usable = [p.name for p in probes if p.ok]
    broken = [p.name for p in probes if not p.ok]
    lines.append("  works:    " + (", ".join(usable) or "(nothing)"))
    if broken:
        lines.append("  does not: " + ", ".join(broken))
    return "\n".join(lines) + "\n"


def _requirements_failed(
    probes: list[Probe],
    required: list[str],
    plugin_rows: list[PluginReadiness] | None = None,
) -> list[str]:
    by_name = {p.name: p for p in probes}
    missing: list[str] = []
    for want in required:
        if want in by_name:
            if not by_name[want].ok:
                missing.append(want)
            continue
        # A requirement may be satisfied by any number of probes (e.g. `exec`
        # matches exec_key / exec_password).
        family = [p for name, p in by_name.items() if name.startswith(want)]
        if family:
            if not any(p.ok for p in family):
                missing.append(want)
            continue
        # OPT-001: a plugin name gates on that plugin's readiness
        # (`--require k8s` / `--require k8s-debug`), which is the whole point
        # of data.capabilities existing.
        rows = [
            row for row in (plugin_rows or [])
            if row.plugin == want or row.plugin.startswith(want)
        ]
        if not rows or not all(row.status == "ok" for row in rows):
            missing.append(want)
    return missing


def _plugin_requirement_names() -> list[str]:
    """Plugin ids a caller may pass to ``--require`` (``--profile full``)."""
    return [spec.plugin for spec in PLUGIN_BACKENDS]


def rows_for_gating(
    readiness: dict[str | None, list[PluginReadiness]], targets: list[str]
) -> list[PluginReadiness]:
    """The readiness rows a ``--require <plugin>`` gate must satisfy.

    With several targets a plugin must be ready on **every** one of them: a
    gate that passes because one host has ``kubectl`` would be a false green,
    which is exactly what this feature exists to prevent.
    """
    if not readiness:
        return []
    if not targets:
        return list(readiness.get(None, []))
    merged: dict[str, PluginReadiness] = {}
    for alias in targets:
        for row in readiness.get(alias, []):
            seen = merged.get(row.plugin)
            if seen is None or row.status != "ok":
                merged[row.plugin] = row
    return list(merged.values())


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="doctor.py",
        description=(
            "Prove which ssh-core capabilities actually work here: local tools, "
            "a staged askpass helper, and (with --host) real connect / transfer "
            "/ sudo probes. --profile full additionally probes the backends of "
            "the other seven plugins in this marketplace. Use --require to gate "
            "a deployment."
        ),
    )
    p.add_argument(
        "--profile", choices=PROFILES, default="transport",
        help=(
            "transport (default): ssh-core's own capabilities, unchanged. "
            "full: also probe docker/podman, kubectl/helm, terraform/tofu, "
            "gh/glab, python yaml/hcl2 and journalctl/tail/nc, then print "
            "readiness per plugin and fill data.capabilities"
        ),
    )
    p.add_argument(
        "--host", default=None,
        help="Alias, user@host or IP to probe live (implies --modes auto)",
    )
    p.add_argument(
        "--hosts", default=None,
        help="Comma-separated targets; every probe runs against each of them",
    )
    p.add_argument(
        "--modes", default="auto",
        help=(
            "Comma-separated auth routes to probe: auto, key, password "
            "(default: key,password — auto is expanded so a host that only "
            "accepts one of them still reports the other as broken)"
        ),
    )
    p.add_argument(
        "--user", "-l", default=None,
        help="Login user for the live probes (forwarded as --user)",
    )
    p.add_argument(
        "--ssh-config", default=None, help="Pass -F <path> to every probe"
    )
    p.add_argument(
        "--password-env", default="SSH_PASSWORD",
        help="Environment variable holding the password (default: SSH_PASSWORD)",
    )
    p.add_argument(
        "--accept-new-hostkey", action="store_true",
        help="Let the probes trust an unknown host key (persisted in known_hosts)",
    )
    p.add_argument(
        "--timeout", type=int, default=30,
        help="Per-probe timeout in seconds (default: 30)",
    )
    p.add_argument(
        "--no-transfer", dest="transfer", action="store_false",
        help="Skip the upload/download round trip",
    )
    p.add_argument(
        "--sudo", action="store_true",
        help="Also prove `sudo -n` works on the target",
    )
    p.add_argument(
        "--require", action="append", default=[], metavar="CAPABILITY",
        help=(
            "Exit 9 unless this capability works; repeatable. Known names: "
            + ", ".join(sorted(REQUIREMENTS))
            + ". With --profile full a plugin id also works (e.g. "
            "--require k8s-debug, --require docker), gating on that plugin's "
            "readiness row."
        ),
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--version", "-V", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("doctor.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

    # Plugin ids are only gated under `--profile full`, because that is the
    # only profile that probes them. Under `transport` an unknown name stays an
    # argv error, exactly as it was before OPT-001.
    plugin_names = (
        _plugin_requirement_names() if args.profile == "full" else []
    )
    unknown = [
        name for name in args.require
        if name not in REQUIREMENTS
        and not any(plugin.startswith(name) for plugin in plugin_names)
    ]
    if unknown:
        hint = (
            f" or a plugin id ({', '.join(plugin_names)})"
            if plugin_names
            else " (plugin ids need --profile full)"
        )
        payload = {
            "success": False, "exit_code": EXIT_INVALID_ARGUMENTS,
            "stdout": "",
            "stderr": (
                f"doctor: unknown --require target(s): {', '.join(unknown)}; "
                f"known: {', '.join(sorted(REQUIREMENTS))}{hint}\n"
            ),
            "data": {"failure_class": "invalid_arguments", "unknown": unknown},
        }
        _emit(payload, args.json)
        return EXIT_INVALID_ARGUMENTS

    targets: list[str] = []
    if args.host:
        targets.append(args.host)
    if args.hosts:
        targets.extend(h.strip() for h in args.hosts.split(",") if h.strip())

    modes = [m.strip() for m in str(args.modes).split(",") if m.strip()]
    if not modes or "auto" in modes:
        # `auto` cannot be probed directly: it *resolves* to key or password
        # from the metadata, and doctor exists to show both answers.
        modes = ["key", "password"]
    args.modes = modes

    probes = local_probes()
    per_host: dict[str, list[Probe]] = {}
    for alias in targets:
        per_host[alias] = remote_probes(alias, args)
        probes.extend(per_host[alias])

    # --profile full (OPT-001): the other seven plugins' backends, plus the
    # remote side in one batched round trip per target. `transport` adds
    # nothing to `data`, so its JSON stays byte-compatible with 1.5.
    readiness: dict[str | None, list[PluginReadiness]] = {}
    local_backends: dict[str, BackendStatus] = {}
    remote_backends: dict[str, dict[str, BackendStatus]] = {}
    if args.profile == "full":
        local_backends = _probe_local_backends()
        for alias in targets:
            remote_backends[alias] = probe_remote_backends(alias, args)
        if targets:
            for alias in targets:
                readiness[alias] = assess_plugins(
                    local_backends, remote_backends.get(alias)
                )
        else:
            readiness[None] = assess_plugins(local_backends, None)

    missing = _requirements_failed(
        probes, args.require, rows_for_gating(readiness, targets)
    )
    code = EXIT_VERIFY_FAILED if missing else EXIT_OK
    data: dict[str, Any] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "targets": targets,
        "modes": modes,
        "probes": [p.as_dict() for p in probes],
        "required": list(args.require),
        "missing_requirements": missing,
        "works": [p.name for p in probes if p.ok],
        "does_not_work": [p.name for p in probes if not p.ok],
        "failure_class": None if code == EXIT_OK else "verify_failed",
    }
    if per_host:
        data["by_host"] = {
            alias: [p.as_dict() for p in host_probes]
            for alias, host_probes in per_host.items()
        }
    if args.profile == "full":
        data["profile"] = "full"
        data["backends"] = {
            "local": [s.as_dict() for s in local_backends.values()],
            "remote": {
                alias: [s.as_dict() for s in statuses.values()]
                for alias, statuses in remote_backends.items()
            },
        }
        data["plugin_readiness"] = {
            (alias or "local"): [row.as_dict() for row in rows]
            for alias, rows in readiness.items()
        }
        # One flat, CI-consumable array. `host` is null for a purely local
        # run, so a gate reads the same field whatever the target set is.
        entries: list[dict[str, Any]] = []
        for alias in (targets or [None]):
            rows = readiness.get(alias, [])
            statuses = remote_backends.get(alias) if alias else None
            for entry in capability_entries(rows, local_backends, statuses):
                entry["host"] = alias
                entries.append(entry)
        data["capabilities"] = entries
    payload = {
        "success": code == EXIT_OK,
        "exit_code": code,
        "stdout": (
            "" if args.json
            else render_full_report(probes, targets, readiness)
            if args.profile == "full"
            else render_human(probes, targets)
        ),
        "stderr": (
            f"doctor: not working: {', '.join(missing)}\n" if missing else ""
        ),
        "data": data,
    }
    _emit(payload, args.json)
    return code


def _probe_local_backends() -> dict[str, BackendStatus]:
    """Every binary/module any plugin needs, probed once on this machine."""
    binaries: list[str] = []
    for spec in PLUGIN_BACKENDS:
        for name in (*spec.needs, *spec.also, *spec.optional):
            if name not in binaries:
                binaries.append(name)
    modules = tuple(PYTHON_MODULE_DIST)
    statuses = _probe_binaries_local(tuple(binaries))
    statuses.update(_probe_modules_local(modules))
    return statuses


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(payload.get("stdout") or "")
        if payload.get("stderr"):
            sys.stderr.write(payload["stderr"])


if __name__ == "__main__":
    sys.exit(guarded_main("doctor.py", main))
