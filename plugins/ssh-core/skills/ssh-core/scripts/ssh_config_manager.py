#!/usr/bin/env python3
"""ssh_config_manager.py - CRUD + restore on ~/.ssh/config (v1.4.0).

Public surface::

    ssh_config_manager.py [--config-path PATH] [--json]
        create --alias A --host H --user U
               [--port P] [--key PATH] [--proxy-jump bastion]
               [--environment X] [--tags a,b] [--description "..."]
               [--location L] [--auth key|password|interactive]
               [--device-type server|network]
        update --alias A [<any of the create fields>] [--dry-run] [--diff]
        delete --alias A [--dry-run] [--allow-wildcard]
        list   [--environment X] [--tags a,b] [--include-wildcards]
               [--format table|json|ansible|yaml|ssh-config]
        find   <alias> [--show-key]
        restore --alias A [--from <backup>] [--list-backups] [--dry-run]
        validate

Inventory export (DV-16)
------------------------
``list --format`` turns the config into something another tool can consume
without post-processing:

``table``
    the historical aligned human table.
``json``
    the records, pretty-printed (``data.hosts`` carries the same list).
``ansible``
    ``{"all": {"hosts": {...}}, "_meta": {"hostvars": {}}}`` — valid input for
    ``ansible-inventory --list -i``. Metadata Ansible has no key for is
    namespaced as ``ssh_core_*`` so it survives the round trip.
``yaml``
    the same inventory as YAML, rendered without a PyYAML dependency.
``ssh-config``
    clean ``Host`` blocks only (no metadata comments, no ``Match``/``Include``)
    for piping into another ``ssh_config`` consumer.

Behaviour worth knowing (all of it used to be a bug):

* **Empty string deletes a field.** ``update --user ''`` removes the
  ``User`` line instead of rendering ``User `` — which OpenSSH rejects with
  ``no argument after keyword "user"`` and which used to break *every* host
  in the file, not just the edited one (SSH-02).
* **Every write is locked, atomic and validated.** A cross-process lock
  (:class:`lib.configfile.ConfigLock`) covers the whole read-modify-write
  cycle, the scratch file is per-process, and the rendered text is checked
  with ``ssh -F <tmp> -G <alias>`` before it replaces the real file. A
  failure restores the previous content (SSH-03).
* **Backups are unique and never deleted.** ``<cfg>.bak.<ms>-<pid>-<n>``
  (SSH-04), restorable with ``restore`` (SSH-05).
* **Wildcards are visible but not editable.** ``list`` marks ``Host *`` /
  ``Host web-*`` rows with ``wildcard: true``; ``create`` / ``update`` /
  ``delete`` refuse them unless ``--allow-wildcard`` is passed, so a typo
  cannot delete the global defaults (SSH-20).
* **Writes are exact-match only (FA-01).** ``create`` / ``update`` /
  ``delete`` / ``find`` look up an alias with ``find_exact``. Before that they
  used OpenSSH glob matching, so a ``Host *`` block made ``delete no-such-host``
  delete the global defaults, ``update no-such-host`` rewrite them and
  ``create anything`` fail with ``already_exists``.
* **Alias matching is case-insensitive**, like OpenSSH itself (SSH-21), and
  ``create`` creates a missing config file when its parent directory exists
  (SSH-22).

Exit codes come from :mod:`lib.exit_codes` (SSH-19); ``--help`` documents the
per-command set.

See:
    references/safety.md       — backup + never-delete conventions
    references/workflows.md    — §5 config management
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    CHILD_TEXT_KWARGS,
    append_audit,
    audit_enabled,
    emit,
    is_wildcard_alias,
    json_result,
    msys_safe_env,
)
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.configfile import (  # noqa: E402
    ConfigLock,
    ConfigLockTimeout,
    atomic_write_with_backup,
    find_backup,
    latest_backup,
    list_backups,
    restore_backup,
)
from lib.exit_codes import (  # noqa: E402
    EXIT_ALREADY_EXISTS,
    EXIT_AMBIGUOUS_ALIAS,
    EXIT_CONFIG_MISSING,
    EXIT_INTERNAL,
    EXIT_INVALID_ARGUMENTS,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PARSE_ERROR,
    EXIT_PERMISSION,
    EXIT_WILDCARD_REFUSED,
)
from lib.redact import redact_identity_path  # noqa: E402
from lib.version import __version__  # noqa: E402, F401  (re-exported for --version)

# --- Constants --------------------------------------------------------------

# failure_class strings matching the shared table.
FAIL_NOT_FOUND = "not_found"
FAIL_ALREADY_EXISTS = "already_exists"
FAIL_CONFIG_MISSING = "config_missing"
FAIL_PARSE_ERROR = "parse_error"
FAIL_PERMISSION = "permission"
FAIL_INTERNAL = "internal"
FAIL_WILDCARD_REFUSED = "wildcard_refused"
FAIL_AMBIGUOUS_ALIAS = "ambiguous_alias"
FAIL_INVALID_ARGUMENTS = "invalid_arguments"

# Metadata comment line regex. Captures (key, value); only the enumerated
# keys are consumed -- everything else stays in preceding_comments.
_METADATA_RE = re.compile(
    r"^\s*#\s*(description|environment|tags|location|auth|device-type)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)

# Recognized OpenSSH directives (case-insensitive on the wire, mapped to
# our canonical lowercase HostBlock attribute names).
_OPENSSH_KEYS: dict[str, str] = {
    "hostname": "host",
    "user": "user",
    "port": "port",
    "identityfile": "identity_file",
    "proxyjump": "proxy_jump",
}

# Stable capitalization used when we render those five back out.
_RENDER_KEYS: dict[str, str] = {
    "host": "HostName",
    "user": "User",
    "port": "Port",
    "identity_file": "IdentityFile",
    "proxy_jump": "ProxyJump",
}

# Indent width used when rendering body directives.
_INDENT = "    "

# Number of (key, value) parts produced by an OpenSSH-style "Key Value"
# split via ``str.split(None, 1)``.
_DIRECTIVE_PARTS = 2

# A directive line that would make OpenSSH abort the *whole* file.
_VALUELESS_DIRECTIVE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*$")


# --- Data model -------------------------------------------------------------


@dataclass
class HostBlock:
    """One ``Host <alias>`` entry parsed from ``~/.ssh/config``."""

    alias: str
    host: str | None = None
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    proxy_jump: str | None = None
    description: str | None = None
    environment: str | None = None
    tags: list[str] = field(default_factory=list)
    location: str | None = None
    auth: str | None = None
    device_type: str | None = None
    extra_directives: list[tuple[str, str]] = field(default_factory=list)
    preceding_comments: list[str] = field(default_factory=list)

    @property
    def patterns(self) -> list[str]:
        """The ``Host`` patterns, split on whitespace (``Host a b`` → 2)."""
        return self.alias.split()

    @property
    def is_wildcard(self) -> bool:
        return any(is_wildcard_alias(p) for p in self.patterns)


@dataclass
class ConfigDocument:
    """In-memory AST for a single ``~/.ssh/config`` file."""

    path: Path
    hosts: list[HostBlock] = field(default_factory=list)
    prologue: list[str] = field(default_factory=list)
    epilogue: list[str] = field(default_factory=list)

    def find(self, alias: str) -> HostBlock | None:
        """First block whose pattern matches ``alias`` (OpenSSH semantics).

        **Read-only use only.** This is "what would ssh actually apply to this
        name", which is glob-aware on purpose: ``Host *`` matches everything.
        Every *write* path (create / update / delete) must use
        :meth:`find_exact`, and ``find`` must not be used to decide whether an
        alias exists — a ``Host *`` block made ``create`` report
        ``already_exists`` for every name, and made ``delete no-such-host``
        delete the global defaults (FA-01).
        """
        from lib import alias_matches

        for h in self.hosts:
            if any(alias_matches(p, alias) for p in h.patterns):
                return h
        return None

    def find_exact(self, alias: str) -> HostBlock | None:
        """Block whose alias matches exactly, ignoring case (SSH-21)."""
        for h in self.hosts:
            if h.alias.casefold() == alias.casefold():
                return h
        return None

    def wildcard_match(self, alias: str) -> HostBlock | None:
        """First *wildcard* block that would apply to ``alias`` (FA-01).

        Used only to explain a miss: ``find no-such-host`` should say "no exact
        entry; ``Host *`` would apply", not pretend an entry exists.
        """
        from lib import alias_matches, is_wildcard_alias

        for h in self.hosts:
            if not h.is_wildcard:
                continue
            if any(
                is_wildcard_alias(p) and alias_matches(p, alias) for p in h.patterns
            ):
                return h
        return None

    def canonical_alias(self, alias: str) -> str | None:
        """Return the on-disk spelling of ``alias`` (or None if absent)."""
        block = self.find_exact(alias)
        return block.alias if block is not None else None

    def list_filtered(
        self, env: str | None = None, tags: list[str] | None = None
    ) -> list[HostBlock]:
        out: list[HostBlock] = []
        for h in self.hosts:
            if env is not None and h.environment != env:
                continue
            if tags and not all(t in h.tags for t in tags):
                continue
            out.append(h)
        return out


# --- Parser -----------------------------------------------------------------


class ConfigParseError(Exception):
    """Raised when the on-disk config can't be parsed safely."""


def parse_config(text: str, path: Path) -> ConfigDocument:  # noqa: PLR0912, PLR0915
    """Parse OpenSSH config text into a :class:`ConfigDocument`.

    ``Match`` blocks are intentionally not parsed: the first ``Match`` line and
    everything after it goes into ``doc.epilogue`` verbatim, so user-defined
    Match rules round-trip without us understanding them.
    """
    doc = ConfigDocument(path=path)
    lines = text.splitlines()  # drops the final '\n' if present
    pending: list[str] = []
    current: HostBlock | None = None
    state = "PROLOGUE"

    def _close_current() -> None:
        nonlocal current
        if current is not None:
            doc.hosts.append(current)
            current = None

    for raw in lines:
        stripped = raw.strip()
        is_blank = not stripped
        is_comment = stripped.startswith("#")
        lower = stripped.lower()
        is_host_dir = (
            lower == "host"
            or lower.startswith("host ")
            or lower.startswith("host\t")
        )
        is_match_dir = (
            lower == "match"
            or lower.startswith("match ")
            or lower.startswith("match\t")
        )

        if state == "AFTER_HOSTS":
            doc.epilogue.append(raw)
            continue

        if is_match_dir:
            _close_current()
            doc.epilogue.extend(pending)
            pending = []
            doc.epilogue.append(raw)
            state = "AFTER_HOSTS"
            continue

        if is_host_dir:
            parts = raw.split(None, 1)
            alias_value = parts[1].strip() if len(parts) > 1 else ""
            if not alias_value:
                raise ConfigParseError(f"Host directive without alias: {raw!r}")
            _close_current()
            preceding, metadata = _extract_metadata(pending)
            pending = []
            current = HostBlock(alias=alias_value, preceding_comments=preceding)
            for k, v in metadata.items():
                setattr(current, k, v)
            state = "HOST_BODY"
            continue

        if state == "PROLOGUE":
            if is_blank or is_comment:
                pending.append(raw)
            else:
                doc.prologue.extend(pending)
                pending = []
                doc.prologue.append(raw)
            continue

        if is_blank or is_comment:
            pending.append(raw)
            state = "COMMENT_BLOCK"
            continue

        if current is None:
            raise ConfigParseError(
                f"directive line with no enclosing Host: {stripped!r}"
            )
        try:
            _apply_directive(current, stripped)
        except ValueError as e:
            raise ConfigParseError(
                f"could not parse directive {stripped!r}: {e}"
            ) from e
        state = "HOST_BODY"

    _close_current()
    if state == "PROLOGUE":
        doc.prologue.extend(pending)
    else:
        doc.epilogue.extend(pending)

    return doc


def _extract_metadata(lines: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Split a pending-comment buffer into (non-metadata, metadata-dict)."""
    non_meta: list[str] = []
    meta: dict[str, Any] = {}
    for line in lines:
        m = _METADATA_RE.match(line)
        if m is None:
            non_meta.append(line)
            continue
        key = m.group(1).lower()
        value = m.group(2).strip()
        if key == "tags":
            meta["tags"] = [t.strip() for t in value.split(",") if t.strip()]
        elif key == "device-type":
            meta["device_type"] = value.lower()
        else:
            meta[key] = value.lower() if key == "auth" else value
    return non_meta, meta


def _apply_directive(host: HostBlock, stripped: str) -> None:
    """Parse one OpenSSH directive line and apply it to ``host``."""
    parts = stripped.split(None, 1)
    if len(parts) == _DIRECTIVE_PARTS:
        key, value = parts[0], parts[1].strip()
    elif "=" in stripped:
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
    else:
        raise ValueError(f"directive without value: {stripped!r}")

    if not key or not value:
        raise ValueError(f"empty key or value in {stripped!r}")

    klower = key.lower()
    if klower in _OPENSSH_KEYS:
        attr = _OPENSSH_KEYS[klower]
        if attr == "port":
            try:
                host.port = int(value)
            except ValueError as e:
                raise ValueError(f"Port must be an integer, got {value!r}") from e
        else:
            setattr(host, attr, value)
    else:
        host.extra_directives.append((key, value))


# --- Renderer ---------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Normalise an optional field: blank → None, collapse inner whitespace.

    ``None`` is the only thing the renderer treats as "omit this directive".
    Before 1.1 the check was ``is not None``, so ``--user ''`` happily wrote
    ``User `` and OpenSSH refused to parse the entire file (SSH-02).
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def render(doc: ConfigDocument) -> str:
    """Render the AST back to a stable, OpenSSH-parseable ``~/.ssh/config``."""
    out: list[str] = []
    out.extend(doc.prologue)
    for h in doc.hosts:
        out.extend(h.preceding_comments)
        out.extend(_emit_metadata(h))
        out.append(f"Host {h.alias}")
        host = _clean(h.host)
        user = _clean(h.user)
        identity = _clean(h.identity_file)
        proxy = _clean(h.proxy_jump)
        if host is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['host']} {host}")
        if user is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['user']} {user}")
        if h.port is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['port']} {h.port}")
        if identity is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['identity_file']} {identity}")
        if proxy is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['proxy_jump']} {proxy}")
        if identity is not None and not _has_identities_only(h):
            out.append(f"{_INDENT}IdentitiesOnly yes")
        for k, v in h.extra_directives:
            cleaned = _clean(v)
            if cleaned is None:
                continue
            out.append(f"{_INDENT}{k} {cleaned}")
    out.extend(doc.epilogue)
    return "\n".join(out) + ("\n" if out else "")


def _emit_metadata(host: HostBlock) -> list[str]:
    """Reconstruct the metadata comment block for one Host."""
    out: list[str] = []
    if _clean(host.description):
        out.append(f"# description: {_clean(host.description)}")
    if _clean(host.environment):
        out.append(f"# environment: {_clean(host.environment)}")
    if host.tags:
        out.append(f"# tags: {','.join(host.tags)}")
    if _clean(host.location):
        out.append(f"# location: {_clean(host.location)}")
    if _clean(host.auth):
        out.append(f"# auth: {_clean(host.auth)}")
    if _clean(host.device_type):
        out.append(f"# device-type: {_clean(host.device_type)}")
    return out


def _has_identities_only(host: HostBlock) -> bool:
    """Did the user already write an IdentitiesOnly directive themselves?"""
    return any(k.lower() == "identitiesonly" for k, _ in host.extra_directives)


# --- Validation -------------------------------------------------------------


def valueless_directive(text: str) -> tuple[int, str] | None:
    """Return ``(line_number, keyword)`` for the first valueless directive.

    OpenSSH treats ``User `` (keyword with no argument) as a fatal parse error
    for the *entire* file, so this is a cheap, network-free guard that runs
    before every write (SSH-02).
    """
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _VALUELESS_DIRECTIVE_RE.match(line)
        if m:
            return index, m.group(1)
    return None


def ssh_syntax_check(text: str, alias: str, *, timeout: int = 13) -> tuple[bool, str]:
    """Run ``ssh -F <temp> -G <alias>`` to prove OpenSSH can parse ``text``.

    No network, no auth: ``-G`` only prints the effective configuration. A
    failure means the rendered file would break connections, so the caller
    must not install it.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="ssh-core-validate-"))
    tmp = tmp_dir / "config"
    try:
        tmp.write_text(text, encoding="utf-8")
        proc = subprocess.run(
            ["ssh", "-F", str(tmp), "-G", alias],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=msys_safe_env(),
            **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # No ssh binary (rare, e.g. a slim container): don't block the write.
        return True, f"skipped (ssh unavailable: {e})"
    finally:
        with contextlib.suppress(OSError):
            for child in tmp_dir.iterdir():
                child.unlink()
            tmp_dir.rmdir()
    if proc.returncode == 0:
        return True, "ok"
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or f"ssh -G exited {proc.returncode}"


# --- Doc loading helpers ----------------------------------------------------


def _load_doc(config_path: Path) -> ConfigDocument:
    """Read + parse the config file (``utf-8-sig`` tolerates a Notepad BOM)."""
    text = config_path.read_text(encoding="utf-8-sig")
    return parse_config(text, config_path)


def _failure_envelope(
    exit_code: int,
    failure_class: str,
    message: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"failure_class": failure_class, "exit_code": exit_code}
    if extra:
        data.update(extra)
    return json_result(
        success=False,
        exit_code=exit_code,
        stderr=message if message.endswith("\n") else message + "\n",
        data=data,
    )


def _map_load_error(exc: Exception, config_path: Path) -> dict[str, Any]:
    if isinstance(exc, FileNotFoundError):
        return _failure_envelope(
            EXIT_CONFIG_MISSING,
            FAIL_CONFIG_MISSING,
            f"ssh_config_manager: config file does not exist: {config_path}",
        )
    if isinstance(exc, PermissionError):
        return _failure_envelope(
            EXIT_PERMISSION,
            FAIL_PERMISSION,
            f"ssh_config_manager: permission denied reading {config_path}: {exc}",
        )
    if isinstance(exc, ConfigParseError):
        return _failure_envelope(
            EXIT_PARSE_ERROR,
            FAIL_PARSE_ERROR,
            f"ssh_config_manager: could not parse {config_path}: {exc}",
        )
    return _failure_envelope(
        EXIT_INTERNAL,
        FAIL_INTERNAL,
        f"ssh_config_manager: unexpected error: {exc}",
    )


def _map_write_error(exc: Exception, config_path: Path) -> dict[str, Any]:
    if isinstance(exc, PermissionError):
        return _failure_envelope(
            EXIT_PERMISSION,
            FAIL_PERMISSION,
            f"ssh_config_manager: permission denied writing {config_path}: {exc}",
        )
    if isinstance(exc, ConfigLockTimeout):
        return _failure_envelope(
            EXIT_PERMISSION,
            FAIL_PERMISSION,
            f"ssh_config_manager: {exc}",
        )
    return _failure_envelope(
        EXIT_INTERNAL,
        FAIL_INTERNAL,
        f"ssh_config_manager: write failed: {exc}",
    )


# --- Subcommand handlers (read path) ----------------------------------------


def _host_to_list_dict(h: HostBlock) -> dict[str, Any]:
    return {
        "alias": h.alias,
        "host": h.host or h.alias,
        # SSHCORE-027: `host` falls back to the alias so consumers always have
        # a connectable name, but that makes "Host foo" and
        # "Host foo / HostName foo" indistinguishable. `hostname_explicit`
        # restores the distinction without changing the meaning of `host`.
        "hostname_explicit": bool(h.host),
        "host_source": "hostname" if h.host else "alias",
        "user": h.user,
        "port": h.port,
        "environment": h.environment,
        "tags": list(h.tags),
        "location": h.location,
        "description": h.description,
        "auth": h.auth,
        "device_type": h.device_type,
        "wildcard": h.is_wildcard,
    }


def _host_to_find_dict(h: HostBlock) -> dict[str, Any]:
    return {
        "alias": h.alias,
        "host": h.host or h.alias,
        "hostname_explicit": bool(h.host),
        "host_source": "hostname" if h.host else "alias",
        "user": h.user,
        "port": h.port,
        "key": h.identity_file,
        "proxy_jump": h.proxy_jump,
        "environment": h.environment,
        "tags": list(h.tags),
        "location": h.location,
        "description": h.description,
        "auth": h.auth,
        "device_type": h.device_type,
        "wildcard": h.is_wildcard,
        "raw_lines": _host_raw_lines(h),
    }


def _host_raw_lines(h: HostBlock) -> list[str]:
    tmp_doc = ConfigDocument(path=Path("."), hosts=[h])
    return render(tmp_doc).splitlines()


def _parse_tags_filter(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    return parts or None


def _format_list_human(hosts: list[dict[str, Any]]) -> str:
    if not hosts:
        return "(no hosts match)\n"
    # `host` is `HostName` when one was written and the alias otherwise, which
    # made the two columns look like a duplicate for every plain
    # "Host 10.0.0.5" entry (SSHCORE-027). The column now shows "-" when no
    # HostName exists, and the JSON keeps `hostname_explicit` for scripts.
    cols = ("alias", "environment", "tags", "auth", "device", "user", "host")
    rows: list[list[str]] = [list(cols)]
    for h in hosts:
        alias = h["alias"] + ("  [wildcard]" if h.get("wildcard") else "")
        explicit = h.get("hostname_explicit")
        if explicit is None:
            explicit = bool(h.get("host")) and h.get("host") != h.get("alias")
        rows.append([
            alias,
            h["environment"] or "",
            ",".join(h["tags"]) if h["tags"] else "",
            h.get("auth") or "",
            h.get("device_type") or "",
            h["user"] or "",
            (h["host"] or "") if explicit else "-",
        ])
    widths = [max(len(r[i]) for r in rows) for i in range(len(cols))]
    lines: list[str] = []
    for r in rows:
        lines.append(
            "  ".join(r[i].ljust(widths[i]) for i in range(len(cols))).rstrip()
        )
    return "\n".join(lines) + "\n"


# --- Inventory export (DV-16) ------------------------------------------------

#: ``list --format`` values. ``table`` is the historical human table; the rest
#: exist so the inventory can be fed to another tool without post-processing.
LIST_FORMATS: tuple[str, ...] = ("table", "json", "ansible", "yaml", "ssh-config")


def _ansible_inventory(hosts: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an Ansible ``--list`` style inventory from the host records.

    Shape (``ansible-inventory --list -i`` compatible)::

        {"all": {"hosts": {"web-01": {"ansible_host": "10.0.1.11",
                                       "ansible_user": "deploy",
                                       "ansible_port": 2222,
                                       "ansible_ssh_private_key_file": "...",
                                       "ssh_core_environment": "production",
                                       "ssh_core_tags": ["web"]}}},
         "_meta": {"hostvars": {}}}

    Ansible keys are only emitted when the value exists, so a minimal config
    produces a minimal host entry. Metadata that Ansible has no native key for
    is namespaced under ``ssh_core_*`` instead of being dropped — an inventory
    export that loses the tags cannot drive a tag-based playbook.
    """
    hosts_out: dict[str, Any] = {}
    for h in hosts:
        entry: dict[str, Any] = {}
        if h.get("host"):
            entry["ansible_host"] = h["host"]
        if h.get("user"):
            entry["ansible_user"] = h["user"]
        if h.get("port"):
            entry["ansible_port"] = h["port"]
        if h.get("key"):
            entry["ansible_ssh_private_key_file"] = h["key"]
        if h.get("environment"):
            entry["ssh_core_environment"] = h["environment"]
        if h.get("tags"):
            entry["ssh_core_tags"] = list(h["tags"])
        if h.get("description"):
            entry["ssh_core_description"] = h["description"]
        if h.get("location"):
            entry["ssh_core_location"] = h["location"]
        if h.get("auth"):
            entry["ssh_core_auth"] = h["auth"]
        if h.get("device_type"):
            entry["ssh_core_device_type"] = h["device_type"]
        hosts_out[h["alias"]] = entry
    return {"all": {"hosts": hosts_out}, "_meta": {"hostvars": {}}}


def _yaml_scalar(value: Any) -> str:
    """Render one YAML scalar without pulling in PyYAML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or text != text.strip() or any(
        ch in text for ch in ":#{}[],&*?|>!%@`\"'"
    ):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _yaml_inventory(hosts: list[dict[str, Any]]) -> str:
    """A dependency-free YAML rendering of the same inventory as Ansible's."""
    inventory = _ansible_inventory(hosts)["all"]["hosts"]
    lines = ["# Generated by ssh_config_manager.py list --format yaml", "all:", "  hosts:"]
    if not inventory:
        return "\n".join(lines[:2]) + "\n  hosts: {}\n"
    for alias, entry in inventory.items():
        lines.append(f"    {_yaml_scalar(alias)}:")
        if not entry:
            lines[-1] += " {}"
            continue
        for key, value in entry.items():
            if isinstance(value, list):
                rendered = "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
            else:
                rendered = _yaml_scalar(value)
            lines.append(f"      {key}: {rendered}")
    return "\n".join(lines) + "\n"


def _first_match_hosts(doc: "ConfigDocument") -> list[dict[str, Any]]:
    """The ``ssh -G``-per-alias view: first matching block wins per alias.

    Distinct from ``list``'s raw-blocks view because OpenSSH resolves an alias
    to exactly one effective set of values, and an export meant to drive
    another tool must agree with what ``ssh <alias>`` would actually do.
    """
    seen: dict[str, dict[str, Any]] = {}
    for block in doc.hosts:
        if block.is_wildcard:
            continue
        for alias in block.patterns:
            if alias in seen:
                continue
            # `{**a, "k": v}` rather than `a | {"k": v}`: the dict merge
            # operator is 3.9+, and requires-python is >=3.8 (CI-05). This is
            # the only runtime construct in the tree that would fail there, and
            # tools/check_python_floor.py now blocks a regression.
            seen[alias] = {**_host_to_find_dict(block), "alias": alias}
    return [seen[alias] for alias in sorted(seen)]


def _ssh_config_export(doc: "ConfigDocument", hosts: list[dict[str, Any]]) -> str:
    """A clean ``ssh_config`` rendering: Host blocks only, no metadata comments.

    Hosts are re-emitted from their effective values, so the result is a valid
    config a *different* tool can consume; the ssh-core metadata comments and
    unrelated prologue (Match blocks, Include lines) are dropped on purpose.
    """
    wanted = {h["alias"] for h in hosts}
    out: list[str] = [
        "# Generated by ssh_config_manager.py list --format ssh-config",
        "# Contains only Host blocks and their directives: no metadata comments.",
    ]
    for block in doc.hosts:
        if block.is_wildcard:
            continue
        overlap = [p for p in block.patterns if p in wanted]
        if not overlap:
            continue
        out.append(f"Host {' '.join(overlap)}")
        for key in ("host", "user"):
            value = _clean(getattr(block, key))
            if value is not None:
                out.append(f"{_INDENT}{_RENDER_KEYS[key]} {value}")
        if block.port is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['port']} {block.port}")
        identity = _clean(block.identity_file)
        if identity is not None:
            out.append(f"{_INDENT}IdentityFile {identity}")
        proxy = _clean(block.proxy_jump)
        if proxy is not None:
            out.append(f"{_INDENT}ProxyJump {proxy}")
        for key, value in block.extra_directives:
            cleaned = _clean(value)
            if cleaned is not None:
                out.append(f"{_INDENT}{key} {cleaned}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _render_list_output(
    fmt: str,
    doc: "ConfigDocument",
    hosts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    """Render ``list`` in the requested format; returns (stdout, extra_data).

    ``json`` returns ``None`` for the extra block because the envelope's own
    ``data.hosts`` already *is* the JSON form.
    """
    if fmt == "table":
        return _format_list_human(hosts), None
    if fmt == "ansible":
        inventory = _ansible_inventory(hosts)
        return (
            json.dumps(inventory, indent=2, ensure_ascii=False) + "\n",
            {"format": "ansible", "inventory": inventory},
        )
    if fmt == "yaml":
        text = _yaml_inventory(hosts)
        return text, {"format": "yaml"}
    if fmt == "ssh-config":
        exported = _ssh_config_export(doc, _first_match_hosts(doc))
        return exported, {"format": "ssh-config"}
    # json: the envelope carries the records; the human stdout is the same
    # data pretty-printed so a non---json caller still sees something useful.
    return (
        json.dumps(hosts, indent=2, ensure_ascii=False) + "\n",
        {"format": "json"},
    )


def _format_find_human(payload: dict[str, Any]) -> str:
    keys = (
        "alias", "host", "user", "port", "key", "proxy_jump",
        "environment", "tags", "location", "description",
        "auth", "device_type",
    )
    lines: list[str] = []
    for k in keys:
        v = payload.get(k)
        if isinstance(v, list):
            v = ",".join(v) if v else ""
        lines.append(f"{k}: {'' if v is None else v}")
    raw = payload.get("raw_lines") or []
    if raw:
        lines.append("")
        lines.append("raw:")
        lines.extend(f"  {ln}" for ln in raw)
    return "\n".join(lines) + "\n"


def cmd_list(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    try:
        doc = _load_doc(config_path)
    except (FileNotFoundError, PermissionError, ConfigParseError) as e:
        return _map_load_error(e, config_path)

    tags = _parse_tags_filter(args.tags)
    filtered = doc.list_filtered(env=args.environment, tags=tags)
    include_wildcards = bool(getattr(args, "include_wildcards", False))
    if not include_wildcards:
        filtered = [h for h in filtered if not h.is_wildcard]
    hosts_payload = [_host_to_list_dict(h) for h in filtered]
    fmt = getattr(args, "format", "table") or "table"
    stdout, extra = _render_list_output(fmt, doc, hosts_payload)
    data: dict[str, Any] = {
        "hosts": hosts_payload,
        "wildcards_hidden": sum(
            1 for h in doc.hosts if h.is_wildcard
        ) if not include_wildcards else 0,
        "failure_class": None,
    }
    if extra:
        data.update(extra)
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=stdout,
        data=data,
    )


def cmd_find(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    try:
        doc = _load_doc(config_path)
    except (FileNotFoundError, PermissionError, ConfigParseError) as e:
        return _map_load_error(e, config_path)

    # FA-01: an *exact* entry is the answer to "does this alias exist". Glob
    # matching stays available as an explanation, never as a hit: a bare
    # `Host *` used to make every name look present, so `find no-such-host`
    # returned `Host *` with success=true.
    host = doc.find_exact(args.alias)
    if host is None:
        pattern_block = doc.wildcard_match(args.alias)
        extra: dict[str, Any] = {
            "alias": args.alias,
            "matched_by": "pattern" if pattern_block is not None else None,
            "wildcard_matched": pattern_block is not None,
        }
        if pattern_block is not None:
            extra["wildcard_alias"] = pattern_block.alias
        return _failure_envelope(
            EXIT_NOT_FOUND,
            FAIL_NOT_FOUND,
            (
                f"ssh_config_manager: alias not found: {args.alias}"
                + (
                    f" (only matched by the wildcard block 'Host "
                    f"{pattern_block.alias}', which is not an exact entry)"
                    if pattern_block is not None
                    else ""
                )
            ),
            extra=extra,
        )
    payload = _host_to_find_dict(host)
    # SSH-07: never print an absolute/homedir IdentityFile path unless the
    # caller explicitly asks for it (SKILL.md: "Never show ... IdentityFile
    # paths"). The key *name* stays visible so the record is still useful, and
    # ``raw_lines`` must be scrubbed too — it is a verbatim copy of the block,
    # so leaving it alone would leak the very path we just hid.
    show_key = bool(getattr(args, "show_key", False))
    if not show_key and payload.get("key"):
        full = payload["key"]
        redacted = redact_identity_path(full)
        payload["key"] = redacted
        payload["key_redacted"] = True
        payload["raw_lines"] = [
            line.replace(full, redacted) for line in payload.get("raw_lines", [])
        ]
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=_format_find_human(payload),
        data={**payload, "matched_by": "exact", "wildcard_matched": False,
              "failure_class": None},
    )


# --- Subcommand handlers (write path) ---------------------------------------


def _empty_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _build_host_from_args(
    args: argparse.Namespace,
    *,
    existing: HostBlock | None = None,
) -> tuple[HostBlock, list[str]]:
    """Return (new_or_updated_host, changed_field_names).

    For ``update``, an explicit empty string means *delete this field*
    (SSH-02). For ``create`` it is refused earlier by :func:`_validate_create`.
    """
    host = HostBlock(alias=getattr(args, "alias", "")) if existing is None else existing

    before = (
        host.host, host.user, host.port, host.identity_file, host.proxy_jump,
        host.description, host.environment, tuple(host.tags), host.location,
        host.auth, host.device_type,
    )

    if existing is None:
        host.host = _empty_to_none(args.host)
        host.user = _empty_to_none(args.user)
    else:
        if args.host is not None:
            host.host = _empty_to_none(args.host)
        if args.user is not None:
            host.user = _empty_to_none(args.user)

    if args.port is not None:
        host.port = args.port
    if getattr(args, "device_type", None) is not None:
        host.device_type = _empty_to_none(args.device_type)
    if args.key is not None:
        host.identity_file = _empty_to_none(args.key)
    if getattr(args, "auth", None) is not None:
        host.auth = _empty_to_none(args.auth)
    if host.auth in ("password", "interactive"):
        host.identity_file = None
    if args.proxy_jump is not None:
        host.proxy_jump = _empty_to_none(args.proxy_jump)
    if args.environment is not None:
        host.environment = _empty_to_none(args.environment)
    if args.tags is not None:
        host.tags = [
            t.strip() for t in args.tags.split(",") if t.strip()
        ] if args.tags.strip() else []
    if args.description is not None:
        host.description = _empty_to_none(args.description)
    if args.location is not None:
        host.location = _empty_to_none(args.location)

    after = (
        host.host, host.user, host.port, host.identity_file, host.proxy_jump,
        host.description, host.environment, tuple(host.tags), host.location,
        host.auth, host.device_type,
    )

    field_names = (
        "host", "user", "port", "identity_file", "proxy_jump",
        "description", "environment", "tags", "location", "auth",
        "device_type",
    )
    changed = [name for name, b, a in zip(field_names, before, after) if b != a]
    return host, changed


def _refuse_wildcard(
    alias: str, args: argparse.Namespace, action: str
) -> dict[str, Any] | None:
    """Reject edits of ``Host *`` / ``Host web-*`` unless explicitly allowed."""
    if not is_wildcard_alias(alias) or getattr(args, "allow_wildcard", False):
        return None
    return _failure_envelope(
        EXIT_WILDCARD_REFUSED,
        FAIL_WILDCARD_REFUSED,
        (
            f"ssh_config_manager: {action} refused: {alias!r} is a wildcard "
            "Host pattern. Editing it would change every host it matches "
            "(a global 'Host *' block carries ServerAliveInterval and "
            "similar defaults). Pass --allow-wildcard if that is really what "
            "you want."
        ),
        extra={"alias": alias, "wildcard": True},
    )


def _validate_create(args: argparse.Namespace) -> dict[str, Any] | None:
    """create requires a non-empty host and user (SSH-02 follow-up)."""
    for field_name in ("host", "user"):
        value = getattr(args, field_name, None)
        if value is None or not str(value).strip():
            return _failure_envelope(
                EXIT_INVALID_ARGUMENTS,
                FAIL_INVALID_ARGUMENTS,
                f"ssh_config_manager: --{field_name} must not be empty on create",
            )
    return None


def _dry_run_payload(
    *,
    action: str,
    alias: str,
    config_path: Path,
    old_text: str,
    new_text: str,
    backup: Path | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Build the envelope for ``--dry-run`` (never touches the file)."""
    data: dict[str, Any] = {
        "alias": alias,
        "action": action,
        "dry_run": True,
        "would_write": new_text,
        "config_path": str(config_path),
        "failure_class": None,
    }
    if backup is not None:
        data["would_backup_to"] = str(backup)
    if getattr(args, "diff", False):
        data["diff"] = _unified_diff(old_text, new_text, config_path, alias)
    human = (
        f"[dry-run] would {action} host '{alias}' in {config_path}\n"
        f"[dry-run] would back up to {backup.name if backup else '(none)'}\n"
    )
    if getattr(args, "diff", False):
        human += data["diff"]
    else:
        human += new_text
    return json_result(
        success=True, exit_code=EXIT_OK, stdout=human, data=data
    )


def _unified_diff(old: str, new: str, path: Path, alias: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (proposed: {alias})",
    )
    text = "".join(diff)
    return text if text else "(no textual difference)\n"


def _write_with_guards(
    *,
    args: argparse.Namespace,
    config_path: Path,
    action: str,
    mutate: "Callable[[ConfigDocument], dict[str, Any] | None]",
) -> dict[str, Any]:
    """Lock → re-read → mutate → render → validate → atomically replace.

    Everything happens *inside* :class:`ConfigLock`, and the document is
    re-read from disk after the lock is taken. That is what makes concurrent
    writers safe (SSH-03): without the re-read, two processes that both parsed
    the old file would each write their own view and silently drop the other's
    host.

    ``mutate`` receives the fresh document, mutates it in place, and may
    return a failure envelope to abort without touching the file.
    """
    try:
        with ConfigLock(config_path):
            try:
                old_text = config_path.read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                old_text = ""
            doc = _load_doc(config_path)
            abort = mutate(doc)
            if abort is not None:
                return abort

            new_text = render(doc)
            problem = valueless_directive(new_text)
            if problem is not None:
                line_no, keyword = problem
                return _failure_envelope(
                    EXIT_PARSE_ERROR,
                    FAIL_PARSE_ERROR,
                    (
                        "ssh_config_manager: refusing to write: rendered line "
                        f"{line_no} is '{keyword}' with no value, which makes "
                        "OpenSSH reject the whole config file"
                    ),
                    extra={"line": line_no, "keyword": keyword},
                )

            alias = getattr(args, "_alias", "")

            if getattr(args, "dry_run", False):
                return _dry_run_payload(
                    action=action,
                    alias=alias,
                    config_path=config_path,
                    old_text=old_text,
                    new_text=new_text,
                    backup=None,
                    args=args,
                )

            if not getattr(args, "no_validate", False):
                ok, detail = ssh_syntax_check(new_text, alias)
                if not ok:
                    return _failure_envelope(
                        EXIT_PARSE_ERROR,
                        FAIL_PARSE_ERROR,
                        (
                            "ssh_config_manager: refusing to write: OpenSSH "
                            f"cannot parse the rendered config ({detail}). The "
                            "file on disk is unchanged."
                        ),
                        extra={"ssh_validation": detail},
                    )

            backup = atomic_write_with_backup(config_path, new_text)
    except (PermissionError, ConfigLockTimeout) as e:
        return _map_write_error(e, config_path)
    except (FileNotFoundError, ConfigParseError) as e:
        return _map_load_error(e, config_path)
    except Exception as e:  # noqa: BLE001  defensive: never raise to user
        return _map_write_error(e, config_path)

    _audit(args, action, alias, config_path, backup, new_text)
    return _success_write_envelope(
        action=action,
        alias=alias,
        config_path=config_path,
        backup=backup,
        old_text=old_text,
        new_text=new_text,
        args=args,
    )


def _success_write_envelope(
    *,
    action: str,
    alias: str,
    config_path: Path,
    backup: Path,
    old_text: str,
    new_text: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    changed = getattr(args, "_changed_fields", [])
    if action == "restored":
        human = f"restored {config_path} from backup (safety copy: {backup.name})\n"
    elif changed:
        human = (
            f"{action} host '{alias}' "
            f"(fields: {','.join(changed)}; backup: {backup.name})\n"
        )
    else:
        human = f"{action} host '{alias}' (backup: {backup.name})\n"
    data: dict[str, Any] = {
        "alias": alias,
        "action": action,
        "backup_path": str(backup),
        "config_path": str(config_path),
        "failure_class": None,
    }
    if changed:
        data["changed_fields"] = changed
    if getattr(args, "diff", False):
        data["diff"] = _unified_diff(old_text, new_text, config_path, alias)
    return json_result(
        success=True, exit_code=EXIT_OK, stdout=human, data=data
    )


def _audit(
    args: argparse.Namespace,
    action: str,
    alias: str,
    config_path: Path,
    backup: Path | None,
    new_text: str,
) -> None:
    """Record a config mutation (SSH-25)."""
    if not audit_enabled(getattr(args, "audit", None), default=False):
        return
    append_audit(
        alias,
        {
            "host": alias,
            "route": "config",
            "action": action,
            "config_path": str(config_path),
            "backup": backup.name if backup else None,
            "bytes": len(new_text.encode("utf-8")),
            "success": True,
            "failure_class": None,
        },
    )


def _ensure_config(path: Path, *, create: bool) -> None:
    """Create an empty config file for ``create`` when its parent exists."""
    if path.exists() or not create:
        return
    if not path.parent.is_dir():
        raise FileNotFoundError(
            f"parent directory does not exist: {path.parent}"
        )
    path.write_text("", encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def cmd_create(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    invalid = _validate_create(args)
    if invalid is not None:
        return invalid
    refusal = _refuse_wildcard(args.alias, args, "create")
    if refusal is not None:
        return refusal
    try:
        _ensure_config(config_path, create=True)  # SSH-22
    except OSError as e:
        return _map_load_error(e, config_path)
    if not config_path.is_file():
        return _map_load_error(FileNotFoundError(str(config_path)), config_path)

    args._alias = args.alias
    args._changed_fields = []

    def mutate(doc: ConfigDocument) -> dict[str, Any] | None:
        # FA-01: only an *exact* entry can collide. Using the glob-aware
        # `find()` here meant a `Host web-*` block rejected `web-01`, and a
        # `Host *` block rejected every name in the file.
        existing = doc.find_exact(args.alias)
        if existing is not None:
            # SSH-21: report the on-disk spelling so WIN-PROD-01 vs
            # win-prod-01 is obvious instead of looking like a phantom.
            return _failure_envelope(
                EXIT_ALREADY_EXISTS,
                FAIL_ALREADY_EXISTS,
                f"ssh_config_manager: alias already exists: {existing.alias}"
                + (
                    f" (case-insensitive match for {args.alias})"
                    if existing.alias != args.alias
                    else ""
                ),
                extra={"alias": existing.alias, "requested_alias": args.alias},
            )
        new_host, changed = _build_host_from_args(args, existing=None)
        if doc.hosts:
            new_host.preceding_comments = [""]
        doc.hosts.append(new_host)
        args._changed_fields = changed
        return None

    return _write_with_guards(
        args=args, config_path=config_path, action="created", mutate=mutate
    )


def cmd_update(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    refusal = _refuse_wildcard(args.alias, args, "update")
    if refusal is not None:
        return refusal
    args._alias = args.alias
    args._changed_fields = []

    def mutate(doc: ConfigDocument) -> dict[str, Any] | None:
        host = doc.find_exact(args.alias)
        if host is None:
            return _failure_envelope(
                EXIT_NOT_FOUND,
                FAIL_NOT_FOUND,
                f"ssh_config_manager: alias not found: {args.alias}",
                extra={"alias": args.alias},
            )
        _, changed = _build_host_from_args(args, existing=host)
        args._changed_fields = changed
        args._alias = host.alias
        return None

    return _write_with_guards(
        args=args, config_path=config_path, action="updated", mutate=mutate
    )


def cmd_delete(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    refusal = _refuse_wildcard(args.alias, args, "delete")
    if refusal is not None:
        return refusal
    args._alias = args.alias
    args._changed_fields = []

    def mutate(doc: ConfigDocument) -> dict[str, Any] | None:
        host = doc.find_exact(args.alias)
        if host is None:
            return _failure_envelope(
                EXIT_NOT_FOUND,
                FAIL_NOT_FOUND,
                f"ssh_config_manager: alias not found: {args.alias}",
                extra={"alias": args.alias},
            )
        args._alias = host.alias
        doc.hosts.remove(host)
        return None

    return _write_with_guards(
        args=args, config_path=config_path, action="deleted", mutate=mutate
    )


def cmd_restore(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:  # noqa: PLR0911
    """Restore ``config_path`` from a backup (SSH-05)."""
    if getattr(args, "list_backups", False):
        backups = list_backups(config_path)
        payload = [
            {
                "path": str(p),
                "name": p.name,
                "size": p.stat().st_size if p.exists() else 0,
            }
            for p in backups
        ]
        human = (
            "\n".join(f"{b['name']}  ({b['size']} bytes)" for b in payload)
            + ("\n" if payload else "(no backups)\n")
        )
        return json_result(
            success=True,
            exit_code=EXIT_OK,
            stdout=human,
            data={"backups": payload, "failure_class": None},
        )

    backup = find_backup(config_path, getattr(args, "from_backup", None))
    if backup is None:
        return _failure_envelope(
            EXIT_NOT_FOUND,
            FAIL_NOT_FOUND,
            (
                f"ssh_config_manager: no backup found for {config_path}"
                + (f" matching {args.from_backup!r}" if args.from_backup else "")
            ),
            extra={"config_path": str(config_path)},
        )

    restored_text = backup.read_text(encoding="utf-8-sig")
    problem = valueless_directive(restored_text)
    if problem is not None:
        line_no, keyword = problem
        return _failure_envelope(
            EXIT_PARSE_ERROR,
            FAIL_PARSE_ERROR,
            (
                f"ssh_config_manager: refusing to restore {backup.name}: line "
                f"{line_no} is '{keyword}' with no value"
            ),
            extra={"line": line_no, "keyword": keyword, "backup": str(backup)},
        )

    alias = getattr(args, "alias", None)
    if alias:
        try:
            doc = parse_config(restored_text, config_path)
        except ConfigParseError as e:
            return _map_load_error(e, config_path)
        block = doc.find_exact(alias)
        if block is None:
            return _failure_envelope(
                EXIT_NOT_FOUND,
                FAIL_NOT_FOUND,
                f"ssh_config_manager: alias {alias!r} is not in {backup.name}",
                extra={"alias": alias, "backup": str(backup)},
            )

    if getattr(args, "dry_run", False):
        old_text = (
            config_path.read_text(encoding="utf-8-sig")
            if config_path.exists()
            else ""
        )
        return _dry_run_payload(
            action="restore",
            alias=alias or "*",
            config_path=config_path,
            old_text=old_text,
            new_text=restored_text,
            backup=backup,
            args=args,
        )

    if not getattr(args, "force", False):
        ok, detail = ssh_syntax_check(restored_text, alias or "*")
        if not ok:
            return _failure_envelope(
                EXIT_PARSE_ERROR,
                FAIL_PARSE_ERROR,
                (
                    "ssh_config_manager: refusing to restore: OpenSSH cannot "
                    f"parse {backup.name} ({detail})"
                ),
                extra={"backup": str(backup), "ssh_validation": detail},
            )

    try:
        with ConfigLock(config_path):
            safety = restore_backup(config_path, backup)
    except (PermissionError, ConfigLockTimeout) as e:
        return _map_write_error(e, config_path)
    except OSError as e:
        return _map_write_error(e, config_path)

    _audit(args, "restored", alias or "*", config_path, safety, restored_text)
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=(
            f"restored {config_path} from {backup.name} "
            f"(previous state saved as {Path(safety).name})\n"
        ),
        data={
            "alias": alias,
            "action": "restored",
            "from_backup": str(backup),
            "safety_backup": str(safety),
            "config_path": str(config_path),
            "failure_class": None,
        },
    )


def cmd_validate(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    """Report whether the on-disk config is parseable by OpenSSH."""
    try:
        text = config_path.read_text(encoding="utf-8-sig")
        doc = parse_config(text, config_path)
    except (FileNotFoundError, PermissionError, ConfigParseError) as e:
        return _map_load_error(e, config_path)

    problem = valueless_directive(text)
    first_alias = doc.hosts[0].patterns[0] if doc.hosts and doc.hosts[0].patterns else "*"
    ok, detail = ssh_syntax_check(text, first_alias)
    issues: list[str] = []
    if problem is not None:
        issues.append(f"line {problem[0]}: '{problem[1]}' has no value")
    if not ok:
        issues.append(f"ssh -G: {detail}")
    success = not issues
    return json_result(
        success=success,
        exit_code=EXIT_OK if success else EXIT_PARSE_ERROR,
        stdout=(
            f"{config_path}: ok ({len(doc.hosts)} host block(s))\n"
            if success
            else f"{config_path}: {len(issues)} problem(s)\n"
            + "\n".join(f"  - {i}" for i in issues)
            + "\n"
        ),
        data={
            "config_path": str(config_path),
            "hosts": len(doc.hosts),
            "issues": issues,
            "ssh_validation": detail,
            "failure_class": None if success else FAIL_PARSE_ERROR,
        },
    )


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree with shared flags before *and* after the verb."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit the shared JSON output contract on stdout",
    )
    common.add_argument(
        "--config-path",
        default=argparse.SUPPRESS,
        help="Override default ~/.ssh/config (mainly for tests)",
    )
    common.add_argument(
        "--audit-log",
        dest="audit",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Append a JSONL audit record under $TMPDIR/ssh-core/",
    )

    p = SSHArgumentParser(
        prog="ssh_config_manager.py",
        parents=[common],
        description=(
            "CRUD + restore over ~/.ssh/config: create / update / delete / "
            "list / find / restore / validate. Writes are locked, atomic, "
            "backed up and validated with `ssh -G` before they land."
        ),
    )
    p.add_argument(
        "--version", "-V", action="store_true", help="Print the ssh-core version"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _shared_write_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--dry-run", action="store_true",
            help="Print the would-be file (and --diff) without writing",
        )
        sp.add_argument(
            "--diff", action="store_true",
            help="Include a unified diff of the change",
        )
        sp.add_argument(
            "--no-validate", action="store_true",
            help="Skip the `ssh -G` syntax check before writing",
        )
        sp.add_argument(
            "--allow-wildcard", action="store_true",
            help="Permit create/update/delete on Host patterns containing * ? !",
        )
        sp.add_argument(
            "--force", action="store_true",
            help="Skip the safety confirmation (currently: pre-write validation)",
        )

    def _add_create_or_update(
        sp: argparse.ArgumentParser,
        *,
        require_host_user: bool,
        nullable: bool = False,
    ) -> None:
        # On update every value flag is ``nargs="?"`` with ``const=""`` so the
        # documented "empty value deletes the field" form works both as
        # ``--user ''`` and as a bare ``--user`` (SSH-02). ``create`` keeps
        # hard-required values.
        extra = {"nargs": "?"} if nullable else {}
        const = {"const": ""} if nullable else {}

        def add(name: str, **kwargs: Any) -> None:
            sp.add_argument(name, **{**extra, **const, **kwargs})

        sp.add_argument("--alias", required=True)
        add("--host", required=require_host_user)
        add("--user", required=require_host_user)
        sp.add_argument("--port", type=int)
        add("--key", help="Path to IdentityFile (empty deletes)")
        add("--proxy-jump", dest="proxy_jump")
        add("--environment")
        add("--tags", help="Comma-separated tag list")
        add("--description")
        add("--location")
        add(
            "--auth", choices=("key", "password", "interactive"),
            help=(
                "Auth method metadata (# auth:); password/interactive "
                "suppress IdentityFile so the prompt can appear"
            ),
        )
        add(
            "--device-type", choices=("server", "network"),
            help="Device kind metadata (# device-type:)",
        )
        _shared_write_flags(sp)

    create = sub.add_parser(
        "create", parents=[common], help="Create a new Host entry"
    )
    _add_create_or_update(create, require_host_user=True)

    update = sub.add_parser(
        "update", parents=[common],
        help=(
            "Update an existing Host entry; an empty value (--user '' or a bare "
            "--user) deletes that field"
        ),
    )
    _add_create_or_update(update, require_host_user=False, nullable=True)

    delete = sub.add_parser(
        "delete", parents=[common], help="Delete a Host entry"
    )
    delete.add_argument("--alias", required=True)
    _shared_write_flags(delete)

    ls = sub.add_parser(
        "list", parents=[common], help="List Host entries (with optional filters)"
    )
    ls.add_argument("--environment")
    ls.add_argument(
        "--tags", help="Comma-separated tag list; matched with AND semantics"
    )
    ls.add_argument(
        "--include-wildcards", action="store_true",
        help="Also show Host * / Host web-* blocks (marked wildcard=true)",
    )
    ls.add_argument(
        "--format", choices=LIST_FORMATS, default="table",
        help=(
            "Output format: table (default), json, ansible (inventory for "
            "`ansible-inventory -i`), yaml, or ssh-config (clean Host blocks "
            "for another tool) (DV-16)"
        ),
    )

    find = sub.add_parser(
        "find", parents=[common], help="Show one Host's full record"
    )
    find.add_argument("alias")
    find.add_argument(
        "--show-key", action="store_true",
        help="Print the IdentityFile path (hidden by default, see safety rule 3)",
    )

    restore = sub.add_parser(
        "restore", parents=[common],
        help="Roll the config back to a backup (latest by default)",
    )
    restore.add_argument("--alias", default=None)
    restore.add_argument(
        "--from", dest="from_backup", default=None,
        help=(
            "Backup path, unique suffix fragment, 'latest' (default) or "
            "'earliest'. Restores are idempotent: the safety copy a restore "
            "writes is not a --from candidate"
        ),
    )
    restore.add_argument(
        "--list-backups", action="store_true", help="List available backups and exit"
    )
    _shared_write_flags(restore)

    sub.add_parser(
        "validate", parents=[common],
        help="Check that OpenSSH can parse the config (ssh -G)",
    )

    return p


_HANDLERS = {
    "create": cmd_create,
    "update": cmd_update,
    "delete": cmd_delete,
    "list": cmd_list,
    "find": cmd_find,
    "restore": cmd_restore,
    "validate": cmd_validate,
}


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_config_manager.py", argv)
    if early is not None:
        return early

    args = build_parser().parse_args(argv)
    raw_config_path = getattr(args, "config_path", None) or "~/.ssh/config"
    as_json = getattr(args, "json", False)
    config_path = Path(raw_config_path).expanduser()

    handler = _HANDLERS[args.cmd]
    try:
        result = handler(args, config_path)
    except Exception as e:  # noqa: BLE001  defensive: never raise to user
        result = _failure_envelope(
            EXIT_INTERNAL,
            FAIL_INTERNAL,
            f"ssh_config_manager: unexpected error: {e}",
        )

    emit(result, as_json=as_json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(guarded_main("ssh_config_manager.py", main))
