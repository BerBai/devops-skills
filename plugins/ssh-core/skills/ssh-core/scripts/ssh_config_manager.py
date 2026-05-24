#!/usr/bin/env python3
"""ssh_config_manager.py - CRUD on ~/.ssh/config (v1.0).

Public surface::

    ssh_config_manager.py [--config-path PATH] [--json]
        create --alias A --host H --user U
               [--port P] [--key PATH] [--proxy-jump bastion]
               [--environment X] [--tags a,b]
               [--description "..."] [--location L]
        update --alias A [<any of the create fields>]
        delete --alias A
        list   [--environment X] [--tags a,b]
        find   <alias>

Every write produces ``<config>.bak.<unix-ts>`` first (per
``references/safety.md`` soft conventions) and uses atomic ``os.replace``
to swap in the new content. Hosts that declare an ``IdentityFile`` get
``IdentitiesOnly yes`` emitted automatically -- this avoids "Too many
authentication failures" from ssh-agent fanning out every loaded key
(see ``references/troubleshooting.md`` "Too many authentication failures").

The OpenSSH-compatible Host block + comment-line metadata convention::

    # description: prod web on aliyun beijing
    # environment: production
    # tags: web,nginx
    # location: aliyun-beijing
    Host prod-web-01
        HostName 10.0.1.11
        User deploy
        IdentityFile ~/.ssh/id_ed25519
        ProxyJump bastion

Those four metadata keys are the only comment lines we *consume*. Every
other comment is kept verbatim in the Host's preceding context, and
``Match`` blocks plus anything that follows them are preserved unchanged
in the document epilogue (we don't parse Match in v1.0).

Exit codes (beyond the 0/1/2 baseline; see error-handling.md)::

    0    operation succeeded
    1    internal / unexpected exception
    2    argparse failure
    4    alias not found (update/delete/find)
    5    alias already exists (create)
    6    config file does not exist
    7    config file could not be parsed
    8    file permission error (read or write)

``data.failure_class`` mirrors the prd's failure-class table: one of
``not_found`` / ``already_exists`` / ``config_missing`` / ``parse_error``
/ ``permission`` / ``internal``, or ``null`` on success.

See:
    plugins/ssh-core/skills/ssh-core/references/workflows.md      (S 5)
    plugins/ssh-core/skills/ssh-core/references/safety.md
    plugins/ssh-core/skills/ssh-core/references/troubleshooting.md
    .trellis/spec/backend/error-handling.md                       (JSON envelope)
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from lib import emit, json_result, msys_safe_env  # noqa: E402, F401, I001

# --- Constants --------------------------------------------------------------

# Exit codes (per prd failure-class table).
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_ARGS = 2
EXIT_NOT_FOUND = 4
EXIT_ALREADY_EXISTS = 5
EXIT_CONFIG_MISSING = 6
EXIT_PARSE_ERROR = 7
EXIT_PERMISSION = 8

# failure_class strings matching the prd's table.
FAIL_NOT_FOUND = "not_found"
FAIL_ALREADY_EXISTS = "already_exists"
FAIL_CONFIG_MISSING = "config_missing"
FAIL_PARSE_ERROR = "parse_error"
FAIL_PERMISSION = "permission"
FAIL_INTERNAL = "internal"

# Metadata comment line regex. Captures (key, value); only the four
# enumerated keys are consumed -- everything else stays in preceding_comments.
_METADATA_RE = re.compile(
    r"^\s*#\s*(description|environment|tags|location)\s*:\s*(.+?)\s*$",
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

# Metadata emission order (stable, diff-friendly).
_METADATA_ORDER: tuple[str, ...] = ("description", "environment", "tags", "location")

# Indent width used when rendering body directives.
_INDENT = "    "

# Number of (key, value) parts produced by an OpenSSH-style "Key Value"
# split via ``str.split(None, 1)``. Pulled out so PLR2004 doesn't flag
# the comparison as a magic value.
_DIRECTIVE_PARTS = 2


# --- Data model -------------------------------------------------------------


@dataclass
class HostBlock:
    """One ``Host <alias>`` entry parsed from ``~/.ssh/config``.

    Attribute names use snake_case so they line up with the JSON output
    contract (``host``, ``user``, ``port``, ``identity_file``,
    ``proxy_jump``, ``description``, ``environment``, ``tags``,
    ``location``). ``identity_file`` is renamed to ``key`` in the JSON
    output of ``find`` to match the prd.
    """

    alias: str
    # OpenSSH fields (None means "not set; ssh applies its default").
    host: str | None = None
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    proxy_jump: str | None = None
    # ssh-core metadata, parsed from comment lines immediately preceding
    # the ``Host`` directive. tags is a list (split on ',') for ergonomics.
    description: str | None = None
    environment: str | None = None
    tags: list[str] = field(default_factory=list)
    location: str | None = None
    # Directives we don't model explicitly; preserved verbatim (raw key
    # casing kept) so user-authored config survives a render round-trip.
    extra_directives: list[tuple[str, str]] = field(default_factory=list)
    # Non-metadata comment + blank lines that appear between the previous
    # Host body (or start of file) and this Host's directive line. Render
    # emits these verbatim, then the reconstructed metadata comments,
    # then ``Host <alias>``.
    preceding_comments: list[str] = field(default_factory=list)


@dataclass
class ConfigDocument:
    """In-memory AST for a single ``~/.ssh/config`` file."""

    path: Path
    hosts: list[HostBlock] = field(default_factory=list)
    # Lines before the first Host directive (verbatim).
    prologue: list[str] = field(default_factory=list)
    # Lines from the first ``Match`` onward (verbatim; we don't parse
    # Match in v1.0). Trailing content after the last Host with no Match
    # also lands here.
    epilogue: list[str] = field(default_factory=list)

    def find(self, alias: str) -> HostBlock | None:
        for h in self.hosts:
            if h.alias == alias:
                return h
        return None

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

    Line-by-line state machine over::

        PROLOGUE -> COMMENT_BLOCK <-> HOST_BODY -> AFTER_HOSTS

    ``Match`` blocks are intentionally not parsed: the first ``Match``
    line and everything after it goes into ``doc.epilogue`` verbatim, so
    user-defined Match rules round-trip without us understanding them.

    The function is deliberately one long function instead of split per
    state -- the per-line branching is the algorithm, and splitting would
    require either a shared mutable Parser object or threading a 5-tuple
    return value through every helper. ``# noqa`` suppresses Ruff's
    PLR0912 / PLR0915 size warnings for this reason.
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
        # Match both "Host alias" and the malformed bare "Host" (which we
        # surface as ConfigParseError below) so users get a parse_error
        # rather than silent prologue-stuffing on broken config.
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
            # Switch to epilogue. Pending lines (between previous Host
            # body and this Match) belong with the epilogue too.
            _close_current()
            doc.epilogue.extend(pending)
            pending = []
            doc.epilogue.append(raw)
            state = "AFTER_HOSTS"
            continue

        if is_host_dir:
            # Everything after "Host " is the alias verbatim -- we don't
            # split multi-pattern Host directives, so "Host a b" becomes
            # alias="a b" and round-trips byte-equal on render.
            parts = raw.split(None, 1)
            alias_value = parts[1].strip() if len(parts) > 1 else ""
            if not alias_value:
                raise ConfigParseError(
                    f"Host directive without alias: {raw!r}"
                )
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
                # Unknown top-level directive. Flush pending lines and
                # the directive into prologue so we don't drop content.
                doc.prologue.extend(pending)
                pending = []
                doc.prologue.append(raw)
            continue

        # state in {HOST_BODY, COMMENT_BLOCK}
        if is_blank or is_comment:
            pending.append(raw)
            state = "COMMENT_BLOCK"
            continue

        # Directive line inside a Host body (or after a stray blank line
        # in COMMENT_BLOCK -- pending stays attached to the next Host).
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

    # End of file. Flush remaining state.
    _close_current()
    if state == "PROLOGUE":
        doc.prologue.extend(pending)
    else:
        # Trailing comments/blanks after the last Host (or remaining in
        # COMMENT_BLOCK with no following Host) become the epilogue.
        doc.epilogue.extend(pending)

    return doc


def _extract_metadata(
    lines: list[str],
) -> tuple[list[str], dict[str, Any]]:
    """Split a pending-comment buffer into (non-metadata, metadata-dict).

    Non-metadata lines (user-written comments, blank lines) keep their
    relative order. Metadata values are normalized: ``tags`` becomes a
    stripped list-of-strings; the other three keys are kept verbatim.
    """
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
        else:
            meta[key] = value
    return non_meta, meta


def _apply_directive(host: HostBlock, stripped: str) -> None:
    """Parse one OpenSSH directive line and apply it to ``host``.

    Supports both ``Key Value`` and ``Key=Value`` forms (OpenSSH accepts
    both). Unknown directives go into ``extra_directives`` with the
    original key casing preserved so the user's intent is retained on
    re-render.
    """
    # Prefer whitespace split (OpenSSH's canonical form). Fall back to
    # '=' only if the line has no whitespace separator.
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
                raise ValueError(
                    f"Port must be an integer, got {value!r}"
                ) from e
        else:
            setattr(host, attr, value)
    else:
        # Preserve unknown directive verbatim (raw key casing kept).
        host.extra_directives.append((key, value))


# --- Renderer ---------------------------------------------------------------


def render(doc: ConfigDocument) -> str:
    """Render the AST back to a stable ``~/.ssh/config`` text.

    Stability invariants:

    * ``prologue`` and ``epilogue`` emit verbatim (line-for-line).
    * Each Host emits ``preceding_comments`` (user context), then the
      reconstructed metadata comments in ``_METADATA_ORDER``, then the
      ``Host <alias>`` directive, then known body directives in the order
      ``HostName -> User -> Port -> IdentityFile -> ProxyJump``, then the
      automatic ``IdentitiesOnly yes`` (if applicable), then
      ``extra_directives`` in original order.
    * Indent is always 4 spaces (OpenSSH tolerates any indent; pinning
      gives diff-friendly output across writes).
    * The output ends with a single trailing ``\\n`` (POSIX habit) iff
      there's any content.
    """
    out: list[str] = []
    out.extend(doc.prologue)
    for h in doc.hosts:
        out.extend(h.preceding_comments)
        out.extend(_emit_metadata(h))
        out.append(f"Host {h.alias}")
        if h.host is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['host']} {h.host}")
        if h.user is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['user']} {h.user}")
        if h.port is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['port']} {h.port}")
        if h.identity_file is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['identity_file']} {h.identity_file}")
        if h.proxy_jump is not None:
            out.append(f"{_INDENT}{_RENDER_KEYS['proxy_jump']} {h.proxy_jump}")
        # IdentitiesOnly auto-set rule: emit only when (a) IdentityFile is
        # set AND (b) the user has not written `IdentitiesOnly ...`
        # themselves. See design.md S IdentitiesOnly + troubleshooting.md
        # "Too many authentication failures".
        if h.identity_file is not None and not _has_identities_only(h):
            out.append(f"{_INDENT}IdentitiesOnly yes")
        for k, v in h.extra_directives:
            out.append(f"{_INDENT}{k} {v}")
    out.extend(doc.epilogue)
    return "\n".join(out) + ("\n" if out else "")


def _emit_metadata(host: HostBlock) -> list[str]:
    """Reconstruct the metadata comment block for one Host."""
    out: list[str] = []
    if host.description:
        out.append(f"# description: {host.description}")
    if host.environment:
        out.append(f"# environment: {host.environment}")
    if host.tags:
        out.append(f"# tags: {','.join(host.tags)}")
    if host.location:
        out.append(f"# location: {host.location}")
    return out


def _has_identities_only(host: HostBlock) -> bool:
    """Did the user already write an IdentitiesOnly directive themselves?"""
    return any(k.lower() == "identitiesonly" for k, _ in host.extra_directives)


# --- Atomic write -----------------------------------------------------------


def atomic_write_with_backup(path: Path, content: str) -> Path:
    """Write ``content`` to ``path`` atomically, returning the backup path.

    Sequence:

    1. ``cp path -> path.bak.<unix-ts>`` via ``shutil.copy2`` (preserves
       the original's permissions and timestamps).
    2. Write ``content`` to ``path.tmp``.
    3. ``tmp.chmod(path.st_mode)`` so the new file inherits the
       original file's mode (typically 0o600 for ``~/.ssh/config``).
    4. ``os.replace(tmp, path)`` -- POSIX-atomic, overwrites on Windows.

    On any failure mid-flight we attempt to clean up the partial
    ``.tmp`` so the working copy at ``path`` is never half-written.
    """
    if not path.exists():
        raise FileNotFoundError(f"config file does not exist: {path}")

    ts = int(time.time())
    backup = Path(f"{path}.bak.{ts}")
    shutil.copy2(path, backup)

    tmp = Path(f"{path}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        with contextlib.suppress(OSError):
            # Best-effort chmod -- permission failures bubble up via the
            # final os.replace anyway, so swallowing here is safe.
            tmp.chmod(path.stat().st_mode)
        os.replace(tmp, path)
    except Exception:
        # Clean up the .tmp so we don't leave a half-written file behind.
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
        raise

    return backup


# --- Doc loading helpers ----------------------------------------------------


def _load_doc(config_path: Path) -> ConfigDocument:
    """Read + parse the config file.

    Raises ``FileNotFoundError`` / ``PermissionError`` / ``ConfigParseError``;
    callers map those to exit codes via :func:`_map_load_error`.
    """
    text = config_path.read_text(encoding="utf-8")
    return parse_config(text, config_path)


def _failure_envelope(
    exit_code: int,
    failure_class: str,
    message: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standardized failure envelope for cmd_* handlers."""
    data: dict[str, Any] = {"failure_class": failure_class}
    if extra:
        data.update(extra)
    return json_result(
        success=False,
        exit_code=exit_code,
        stderr=message if message.endswith("\n") else message + "\n",
        data=data,
    )


def _map_load_error(exc: Exception, config_path: Path) -> dict[str, Any]:
    """Map a load-time exception to the right failure envelope."""
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


# --- Subcommand handlers (read path) ----------------------------------------


def _host_to_list_dict(h: HostBlock) -> dict[str, Any]:
    """The 8-field per-host record used in ``cmd_list`` JSON output."""
    return {
        "alias": h.alias,
        "host": h.host,
        "user": h.user,
        "port": h.port,
        "environment": h.environment,
        "tags": list(h.tags),
        "location": h.location,
        "description": h.description,
    }


def _host_to_find_dict(h: HostBlock) -> dict[str, Any]:
    """The 11-field per-host record used in ``cmd_find`` JSON output."""
    return {
        "alias": h.alias,
        "host": h.host,
        "user": h.user,
        "port": h.port,
        "key": h.identity_file,
        "proxy_jump": h.proxy_jump,
        "environment": h.environment,
        "tags": list(h.tags),
        "location": h.location,
        "description": h.description,
        "raw_lines": _host_raw_lines(h),
    }


def _host_raw_lines(h: HostBlock) -> list[str]:
    """Reconstruct the lines for one Host as ``render`` would emit them."""
    out: list[str] = []
    out.extend(h.preceding_comments)
    out.extend(_emit_metadata(h))
    out.append(f"Host {h.alias}")
    if h.host is not None:
        out.append(f"{_INDENT}{_RENDER_KEYS['host']} {h.host}")
    if h.user is not None:
        out.append(f"{_INDENT}{_RENDER_KEYS['user']} {h.user}")
    if h.port is not None:
        out.append(f"{_INDENT}{_RENDER_KEYS['port']} {h.port}")
    if h.identity_file is not None:
        out.append(f"{_INDENT}{_RENDER_KEYS['identity_file']} {h.identity_file}")
    if h.proxy_jump is not None:
        out.append(f"{_INDENT}{_RENDER_KEYS['proxy_jump']} {h.proxy_jump}")
    if h.identity_file is not None and not _has_identities_only(h):
        out.append(f"{_INDENT}IdentitiesOnly yes")
    for k, v in h.extra_directives:
        out.append(f"{_INDENT}{k} {v}")
    return out


def _parse_tags_filter(raw: str | None) -> list[str] | None:
    """Turn a ``--tags a,b,c`` string into a list (or None if absent)."""
    if not raw:
        return None
    parts = [t.strip() for t in raw.split(",") if t.strip()]
    return parts or None


def _format_list_human(hosts: list[dict[str, Any]]) -> str:
    """Render the list output as a small fixed-column table."""
    if not hosts:
        return "(no hosts match)\n"
    cols = ("alias", "environment", "tags", "user", "host")
    rows: list[list[str]] = [list(cols)]
    for h in hosts:
        rows.append([
            h["alias"] or "",
            h["environment"] or "",
            ",".join(h["tags"]) if h["tags"] else "",
            h["user"] or "",
            h["host"] or "",
        ])
    widths = [max(len(r[i]) for r in rows) for i in range(len(cols))]
    lines: list[str] = []
    for r in rows:
        lines.append(
            "  ".join(r[i].ljust(widths[i]) for i in range(len(cols))).rstrip()
        )
    return "\n".join(lines) + "\n"


def _format_find_human(payload: dict[str, Any]) -> str:
    """Render the find output as key: value lines + the raw block tail."""
    keys = (
        "alias", "host", "user", "port", "key", "proxy_jump",
        "environment", "tags", "location", "description",
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
    hosts_payload = [_host_to_list_dict(h) for h in filtered]
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=_format_list_human(hosts_payload),
        data={"hosts": hosts_payload, "failure_class": None},
    )


def cmd_find(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    try:
        doc = _load_doc(config_path)
    except (FileNotFoundError, PermissionError, ConfigParseError) as e:
        return _map_load_error(e, config_path)

    host = doc.find(args.alias)
    if host is None:
        return _failure_envelope(
            EXIT_NOT_FOUND,
            FAIL_NOT_FOUND,
            f"ssh_config_manager: alias not found: {args.alias}",
            extra={"alias": args.alias},
        )
    payload = _host_to_find_dict(host)
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=_format_find_human(payload),
        data={**payload, "failure_class": None},
    )


# --- Subcommand handlers (write path) ---------------------------------------


def _build_host_from_args(
    args: argparse.Namespace,
    *,
    existing: HostBlock | None = None,
) -> tuple[HostBlock, list[str]]:
    """Return (new_or_updated_host, changed_field_names).

    For ``create`` (existing is None), the required args (host, user) plus
    any non-None optional args land on a fresh HostBlock.

    For ``update``, only fields the user actually passed on the CLI are
    mutated; the rest of ``existing`` survives untouched. The change
    detector compares post-mutation values against the snapshot taken
    before mutation -- this is what ``data.changed_fields`` reports.
    """
    host = HostBlock(alias=args.alias) if existing is None else existing

    before = (
        host.host, host.user, host.port, host.identity_file, host.proxy_jump,
        host.description, host.environment, tuple(host.tags), host.location,
    )

    if existing is None:
        # create: required args are guaranteed by argparse.
        host.host = args.host
        host.user = args.user
    else:
        if args.host is not None:
            host.host = args.host
        if args.user is not None:
            host.user = args.user

    if args.port is not None:
        host.port = args.port
    if args.key is not None:
        host.identity_file = args.key
    if args.proxy_jump is not None:
        host.proxy_jump = args.proxy_jump
    if args.environment is not None:
        host.environment = args.environment
    if args.tags is not None:
        host.tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.description is not None:
        host.description = args.description
    if args.location is not None:
        host.location = args.location

    after = (
        host.host, host.user, host.port, host.identity_file, host.proxy_jump,
        host.description, host.environment, tuple(host.tags), host.location,
    )

    field_names = (
        "host", "user", "port", "identity_file", "proxy_jump",
        "description", "environment", "tags", "location",
    )
    changed = [
        name for name, b, a in zip(field_names, before, after, strict=True)
        if b != a
    ]
    return host, changed


def cmd_create(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    try:
        doc = _load_doc(config_path)
    except (FileNotFoundError, PermissionError, ConfigParseError) as e:
        return _map_load_error(e, config_path)

    if doc.find(args.alias) is not None:
        return _failure_envelope(
            EXIT_ALREADY_EXISTS,
            FAIL_ALREADY_EXISTS,
            f"ssh_config_manager: alias already exists: {args.alias}",
            extra={"alias": args.alias},
        )

    new_host, _changed = _build_host_from_args(args, existing=None)
    if doc.hosts:
        # Visually separate from the previous Host body with a blank
        # line so the render is human-friendly.
        new_host.preceding_comments = [""]
    doc.hosts.append(new_host)

    try:
        backup = atomic_write_with_backup(config_path, render(doc))
    except PermissionError as e:
        return _failure_envelope(
            EXIT_PERMISSION,
            FAIL_PERMISSION,
            f"ssh_config_manager: permission denied writing {config_path}: {e}",
        )
    except Exception as e:  # noqa: BLE001  defensive: never raise to user
        return _failure_envelope(
            EXIT_INTERNAL,
            FAIL_INTERNAL,
            f"ssh_config_manager: write failed: {e}",
        )

    msg = f"created host '{args.alias}' (backup: {backup.name})\n"
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=msg,
        data={
            "alias": args.alias,
            "action": "created",
            "backup_path": str(backup),
            "failure_class": None,
        },
    )


def cmd_update(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    try:
        doc = _load_doc(config_path)
    except (FileNotFoundError, PermissionError, ConfigParseError) as e:
        return _map_load_error(e, config_path)

    host = doc.find(args.alias)
    if host is None:
        return _failure_envelope(
            EXIT_NOT_FOUND,
            FAIL_NOT_FOUND,
            f"ssh_config_manager: alias not found: {args.alias}",
            extra={"alias": args.alias},
        )

    _, changed = _build_host_from_args(args, existing=host)

    try:
        backup = atomic_write_with_backup(config_path, render(doc))
    except PermissionError as e:
        return _failure_envelope(
            EXIT_PERMISSION,
            FAIL_PERMISSION,
            f"ssh_config_manager: permission denied writing {config_path}: {e}",
        )
    except Exception as e:  # noqa: BLE001  defensive: never raise to user
        return _failure_envelope(
            EXIT_INTERNAL,
            FAIL_INTERNAL,
            f"ssh_config_manager: write failed: {e}",
        )

    if changed:
        msg = (
            f"updated host '{args.alias}' "
            f"(fields: {','.join(changed)}; backup: {backup.name})\n"
        )
    else:
        msg = (
            f"updated host '{args.alias}' "
            f"(no fields changed; backup: {backup.name})\n"
        )
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=msg,
        data={
            "alias": args.alias,
            "action": "updated",
            "changed_fields": changed,
            "backup_path": str(backup),
            "failure_class": None,
        },
    )


def cmd_delete(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    try:
        doc = _load_doc(config_path)
    except (FileNotFoundError, PermissionError, ConfigParseError) as e:
        return _map_load_error(e, config_path)

    host = doc.find(args.alias)
    if host is None:
        return _failure_envelope(
            EXIT_NOT_FOUND,
            FAIL_NOT_FOUND,
            f"ssh_config_manager: alias not found: {args.alias}",
            extra={"alias": args.alias},
        )

    doc.hosts.remove(host)

    try:
        backup = atomic_write_with_backup(config_path, render(doc))
    except PermissionError as e:
        return _failure_envelope(
            EXIT_PERMISSION,
            FAIL_PERMISSION,
            f"ssh_config_manager: permission denied writing {config_path}: {e}",
        )
    except Exception as e:  # noqa: BLE001  defensive: never raise to user
        return _failure_envelope(
            EXIT_INTERNAL,
            FAIL_INTERNAL,
            f"ssh_config_manager: write failed: {e}",
        )

    msg = f"deleted host '{args.alias}' (backup: {backup.name})\n"
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout=msg,
        data={
            "alias": args.alias,
            "action": "deleted",
            "backup_path": str(backup),
            "failure_class": None,
        },
    )


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree with shared --json / --config-path flags
    available both before and after the subcommand.

    ``default=argparse.SUPPRESS`` is essential here: when a subparser is
    declared with ``parents=[common]`` it re-binds the same arguments,
    and the subparser's default would otherwise clobber the value the
    top-level parser already wrote into the namespace -- so a CLI like
    ``ssh_config_manager.py --config-path X create ...`` would silently
    fall back to ``~/.ssh/config`` and mutate the real config. SUPPRESS
    means "if the user didn't pass this flag, don't touch the namespace
    attribute", which preserves the top-level value.
    """
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

    p = argparse.ArgumentParser(
        prog="ssh_config_manager.py",
        parents=[common],
        description=(
            "CRUD over ~/.ssh/config: create / update / delete / list / find. "
            "Preserves OpenSSH-native compatibility while attaching ssh-core "
            "metadata (description / environment / tags / location) as "
            "comment lines above each Host block."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_create_or_update(
        sp: argparse.ArgumentParser, *, require_host_user: bool
    ) -> None:
        sp.add_argument("--alias", required=True)
        sp.add_argument("--host", required=require_host_user)
        sp.add_argument("--user", required=require_host_user)
        sp.add_argument("--port", type=int)
        sp.add_argument("--key", help="Path to IdentityFile")
        sp.add_argument("--proxy-jump", dest="proxy_jump")
        sp.add_argument("--environment")
        sp.add_argument("--tags", help="Comma-separated tag list")
        sp.add_argument("--description")
        sp.add_argument("--location")

    create = sub.add_parser(
        "create", parents=[common], help="Create a new Host entry"
    )
    _add_create_or_update(create, require_host_user=True)

    update = sub.add_parser(
        "update", parents=[common], help="Update an existing Host entry"
    )
    _add_create_or_update(update, require_host_user=False)

    delete = sub.add_parser(
        "delete", parents=[common], help="Delete a Host entry"
    )
    delete.add_argument("--alias", required=True)

    ls = sub.add_parser(
        "list", parents=[common], help="List Host entries (with optional filters)"
    )
    ls.add_argument("--environment")
    ls.add_argument(
        "--tags", help="Comma-separated tag list; matched with AND semantics"
    )

    find = sub.add_parser(
        "find", parents=[common], help="Show one Host's full record"
    )
    find.add_argument("alias")

    return p


_HANDLERS = {
    "create": cmd_create,
    "update": cmd_update,
    "delete": cmd_delete,
    "list":   cmd_list,
    "find":   cmd_find,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # SUPPRESS in build_parser() means these attrs may be absent if the
    # user didn't pass the flag at all (vs falsy if they passed the flag
    # with an empty value).
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
    sys.exit(main())
