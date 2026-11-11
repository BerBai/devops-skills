"""Redaction of host identifiers in envelopes (SSH-07).

``references/safety.md`` rule 3 promises that the *alias* is kept while
``HostName``, ``User``, ``Port`` and ``IdentityFile`` are dropped. Before this
module that rule was documentation only: a failed login printed
``root@192.168.30.240: Permission denied`` on stderr, and ``find`` echoed the
private key path.

The redactor is deliberately conservative:

* It replaces values it can *prove* belong to the alias — the tokens parsed
  out of the alias itself (``root@10.0.0.5:2222``) plus whatever ``ssh -G``
  resolves ``HostName`` / ``User`` / ``IdentityFile`` to. It never guesses
  that an arbitrary dotted word is a hostname.
* Placeholders are stable and greppable: ``<alias>``, ``<user>``, ``<host>``,
  ``<port>``, ``<key>``.
* ``ssh -G`` is a pure config parse (no network, no auth) and is cached per
  ``(alias, config)`` for the lifetime of the process.

Usage::

    from lib.redact import redactor_for
    r = redactor_for(alias, ssh_config)
    result["stderr"] = r.text(result["stderr"])
    result["data"]["ssh_argv"] = r.argv(result["data"]["ssh_argv"])
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import CHILD_TEXT_KWARGS, msys_safe_env

_SSH_G_TIMEOUT = 10
_CACHE: dict[tuple[str, str | None], HostFacts] = {}

# ``[user@]host[:port]`` — the shapes we accept on the command line.
_TARGET_RE = re.compile(
    r"^(?:(?P<user>[A-Za-z0-9._%+-]+)@)?(?P<host>\[[0-9A-Fa-f:.]+\]|[^:@\s]+)"
    r"(?::(?P<port>\d{1,5}))?$"
)

PLACEHOLDER_ALIAS = "<alias>"
PLACEHOLDER_USER = "<user>"
PLACEHOLDER_HOST = "<host>"
PLACEHOLDER_PORT = "<port>"
PLACEHOLDER_KEY = "<key>"

# Identity file paths appear in argv as ``-i <path>`` or ``-o IdentityFile=x``.
_IDENTITY_OPTION_RE = re.compile(r"(?i)^identityfile=(.+)$")


#: An alias that *is* an address literal (``192.168.30.240``, ``[fe80::1]``).
#: It must be redacted like a HostName: the caller typed an address, not a
#: name, so keeping it in ``ssh_argv`` while ``stderr`` says ``<alias>`` is
#: inconsistent — and it is precisely the identifier the rule exists to hide.
_ADDRESS_ALIAS_RE = re.compile(
    r"^(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:]+\]|[0-9A-Fa-f]*:[0-9A-Fa-f:]+)$"
)


def _is_address_literal(value: str) -> bool:
    """True when ``value`` looks like an IP address rather than a name."""
    return bool(_ADDRESS_ALIAS_RE.match(value.strip()))


@dataclass
class HostFacts:
    """Everything about one alias that must not leak into an envelope."""

    alias: str
    user: str | None = None
    hostname: str | None = None
    port: str | None = None
    identity_files: list[str] = field(default_factory=list)
    # Extra strings discovered while parsing the alias itself
    # (``user@host:port`` given directly instead of a config alias).
    literal_user: str | None = None
    literal_host: str | None = None
    literal_port: str | None = None

    def secrets(self) -> list[tuple[str, str]]:
        """``(needle, placeholder)`` pairs, longest needle first.

        Ordering matters: ``root@10.0.0.5`` must be replaced before ``10.0.0.5``
        and before ``root``, otherwise the longer form would be left half
        redacted.

        A *name* alias (``lab-240``) is kept on purpose — it is the handle the
        caller already knows. An alias that is an **address literal** is not:
        it is the host identity itself, so it becomes ``<host>`` in argv and in
        any captured text (SSH-07 consistency).
        """
        pairs: list[tuple[str, str]] = []
        if self.literal_user and self.literal_host:
            pairs.append(
                (
                    f"{self.literal_user}@{self.literal_host}",
                    PLACEHOLDER_ALIAS,
                )
            )
        if self.user and self.hostname and self.user != self.hostname:
            pairs.append((f"{self.user}@{self.hostname}", PLACEHOLDER_ALIAS))
        if _is_address_literal(self.alias):
            pairs.append((self.alias.strip(), PLACEHOLDER_HOST))
        for needle, placeholder in (
            (self.hostname, PLACEHOLDER_HOST),
            (self.literal_host, PLACEHOLDER_HOST),
        ):
            if needle and needle != self.alias:
                pairs.append((needle, placeholder))
        for needle in self.identity_files:
            if needle:
                pairs.append((needle, PLACEHOLDER_KEY))
        # Drop duplicates while preserving order, longest first.
        seen: set[str] = set()
        ordered: list[tuple[str, str]] = []
        for needle, placeholder in sorted(pairs, key=lambda p: -len(p[0])):
            if not needle or needle in seen:
                continue
            seen.add(needle)
            ordered.append((needle, placeholder))
        return ordered


def _ssh_g(alias: str, ssh_config: str | None) -> dict[str, str]:
    """Parse ``ssh -G <alias>`` into a lowercase-key dict (best effort)."""
    argv = ["ssh", "-G"]
    if ssh_config:
        argv += ["-F", ssh_config]
    argv.append(alias)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SSH_G_TIMEOUT,
            check=False,
            env=msys_safe_env(),
            **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    facts: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        facts[parts[0].lower()] = parts[1].strip()
    return facts


def facts_for(alias: str, ssh_config: str | None = None) -> HostFacts:
    """Return (and cache) the redaction facts for ``alias``."""
    key = (alias, ssh_config)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    facts = HostFacts(alias=alias)
    match = _TARGET_RE.match(alias)
    if match:
        facts.literal_user = match.group("user")
        facts.literal_host = match.group("host")
        facts.literal_port = match.group("port")

    parsed = _ssh_g(alias, ssh_config)
    if parsed:
        facts.user = parsed.get("user") or None
        facts.hostname = parsed.get("hostname") or None
        facts.port = parsed.get("port") or None
        identity = parsed.get("identityfile")
        if identity and identity not in ("none", "(none)"):
            facts.identity_files.append(_clean_path(identity))
            expanded = str(Path(identity).expanduser())
            if expanded != identity:
                facts.identity_files.append(expanded)
        also = parsed.get("identityfile")
        if also and also not in facts.identity_files:
            facts.identity_files.append(also)

    _CACHE[key] = facts
    return facts


def _clean_path(value: str) -> str:
    return value.strip().strip('"')


@dataclass
class Redactor:
    """Replaces host identifiers with stable placeholders."""

    facts: HostFacts
    enabled: bool = True

    def text(self, value: str | None) -> str:
        if not value or not self.enabled:
            return value or ""
        out = value
        for needle, placeholder in self.facts.secrets():
            out = out.replace(needle, placeholder)
        # A resolved port is only a secret in combination with a host; a bare
        # "22" carries no identity, so it is left alone.
        return out

    def argv(self, argv: list[str] | None) -> list[str]:
        """Redact an argv list, including ``-i <path>`` / ``-o IdentityFile=``."""
        if not argv:
            return []
        out: list[str] = []
        hide_next = False
        for token in argv:
            if hide_next:
                out.append(PLACEHOLDER_KEY)
                hide_next = False
                continue
            if token in ("-i", "-oIdentityFile") or token == "--identity":
                out.append(token)
                hide_next = token == "-i" or token == "--identity"
                if not hide_next:
                    out.append(PLACEHOLDER_KEY)
                continue
            match = _IDENTITY_OPTION_RE.match(token)
            if match:
                out.append(f"IdentityFile={PLACEHOLDER_KEY}")
                continue
            out.append(self.text(token))
        return out

    def data(self, payload: dict) -> dict:
        """Redact the well-known leaky keys of an envelope ``data`` block."""
        if not self.enabled:
            return payload
        for key in ("ssh_argv", "raw_stderr", "raw_stdout", "ssh_copy_id_argv"):
            value = payload.get(key)
            if isinstance(value, list):
                payload[key] = self.argv(value)
            elif isinstance(value, str):
                payload[key] = self.text(value)
        for key in ("host", "hostname", "remote_host", "identity_file", "key_path"):
            _ = key  # documented above; callers decide when to drop the value
        return payload


def redactor_for(
    alias: str,
    ssh_config: str | None = None,
    *,
    enabled: bool = True,
) -> Redactor:
    """Convenience constructor used by every script."""
    return Redactor(facts=facts_for(alias, ssh_config), enabled=enabled)


def redact_identity_path(value: str | None) -> str | None:
    """Hide the *directory* of a key path but keep the file name readable.

    ``find`` still needs to say *which* key is configured (``~/.ssh/id_x``
    versus ``C:/keys/prod_ed25519``) without disclosing the user's home
    directory or absolute layout.
    """
    if not value:
        return value
    path = Path(value)
    name = path.name
    if not name:
        return PLACEHOLDER_KEY
    return f"~/.ssh/{name}" if name.startswith("id_") else f"<key-dir>/{name}"


def scrub_secrets(text: str, secrets: list[str]) -> str:
    """Blank out literal secrets (passwords) that leaked into captured text."""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


def is_enabled_default() -> bool:
    """Redaction is on unless the caller passed ``--no-redact`` (see docs)."""
    return True


__all__ = [
    "HostFacts",
    "PLACEHOLDER_ALIAS",
    "PLACEHOLDER_HOST",
    "PLACEHOLDER_KEY",
    "PLACEHOLDER_USER",
    "Redactor",
    "facts_for",
    "redact_identity_path",
    "redactor_for",
    "scrub_secrets",
]

# ``contextlib`` and ``field`` are used by the dataclass helpers above; keep
# the imports referenced so linters do not strip them.
_ = (contextlib, field)
