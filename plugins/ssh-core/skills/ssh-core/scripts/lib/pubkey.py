"""pubkey.py — public-key discovery and ``authorized_keys`` install helpers.

Pure functions only: no network, no subprocess. ``deploy_pubkey.py`` drives
them over SSH; unit tests exercise them directly.

Two things live here:

* **Discovery** — find the caller's default public key, reject a private key
  accidentally passed as ``--key``, and expose the ``type blob`` identity of
  a key (used for dedupe and for the "is it really there?" report).
* **Install script** — build the POSIX shell snippet that appends the key to
  ``$HOME/.ssh/authorized_keys`` idempotently, fixes the two modes sshd
  insists on (0700 / 0600), restores the SELinux label when ``restorecon``
  exists, and reports a machine-readable ``SSHCORE:...`` marker so the
  caller never has to guess what happened from free-form text.

The script deliberately uses only POSIX built-ins plus ``grep`` / ``chmod`` /
``command``: it must run on old enterprise distros (Kylin 3, RHEL 7) and on
minimal containers where ``base64`` or ``python`` are absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Modern-to-legacy order: RSA-only hosts (older distros / appliances) then
# work with no --key at all once an RSA key exists.
DEFAULT_KEY_CANDIDATES: tuple[str, ...] = (
    "~/.ssh/id_ed25519.pub",
    "~/.ssh/id_ecdsa.pub",
    "~/.ssh/id_rsa.pub",
    "~/.ssh/id_dsa.pub",
)
DEFAULT_KEY = DEFAULT_KEY_CANDIDATES[0]

# OpenSSH public-key type prefixes; first whitespace-delimited token of any
# ``.pub`` file. Extending the tuple supports new key types without any other
# code change.
PUBKEY_PREFIXES: tuple[str, ...] = (
    "ssh-rsa",
    "ssh-ed25519",
    "ssh-dss",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "ssh-ed25519-sk",
)

# Markers the install script prints. Kept short and greppable.
MARK_INSTALLED = "SSHCORE:installed"
MARK_ALREADY = "SSHCORE:already_installed"
MARK_HOME = "SSHCORE:home:"
MARK_ERROR_PREFIX = "SSHCORE:error:"

PUBKEY_PEEK_BYTES = 200


class PubkeyError(Exception):
    """A local pubkey problem, carrying the failure class to report."""

    def __init__(self, failure_class: str, message: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.message = message


@dataclass(frozen=True)
class Pubkey:
    """One parsed OpenSSH public key line."""

    path: Path
    line: str
    key_type: str
    blob: str
    comment: str = ""
    source: str = "explicit"

    @property
    def identity(self) -> str:
        """``type blob`` — the part sshd actually matches, comment excluded."""
        return f"{self.key_type} {self.blob}"

    @property
    def private_key_path(self) -> Path | None:
        """The private key that shares this file's basename, if it exists.

        ``--key`` may point at someone else's ``.pub`` with no private half
        on this machine; verification then cannot work, and saying so beats
        reporting a mysterious "Permission denied".
        """
        if self.path.suffix != ".pub":
            return None
        candidate = self.path.with_suffix("")
        return candidate if candidate.is_file() else None

    @property
    def short_name(self) -> str:
        return self.path.name


def resolve_key_path(path: str) -> Path:
    """Expand ``~`` and make absolute. Does NOT check existence."""
    return Path(path).expanduser().resolve()


def pick_default_pubkey() -> tuple[str, str]:
    """Choose the default pubkey: first existing candidate, else the first.

    Returns ``(path, source)`` where source is ``"auto"`` (found on disk) or
    ``"fallback"`` (none existed; we report the modern default so the error
    message stays actionable).
    """
    for candidate in DEFAULT_KEY_CANDIDATES:
        if Path(candidate).expanduser().is_file():
            return candidate, "auto"
    return DEFAULT_KEY, "fallback"


def looks_like_pubkey_text(text: str) -> bool:
    """True iff ``text`` starts with a known OpenSSH public-key type token."""
    head = text.lstrip()
    return any(head.startswith(prefix + " ") for prefix in PUBKEY_PREFIXES)


def _first_key_line(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line
    return ""


def pubkey_from_text(
    text: str, *, path: Path | None = None, source: str = "explicit"
) -> Pubkey:
    """Parse one public-key line (``type base64 [comment]``) into a :class:`Pubkey`.

    Shared by :func:`load_pubkey` (which reads a file first) and by
    ``deploy_pubkey.py`` (which already has the text and needs the same
    quote-safe identity used by the install script). Keeping this in one place
    is what stops the "type blob" derivation from drifting between the two.
    """
    line = _first_key_line(text)
    parts = line.split()
    if len(parts) < 2 or not looks_like_pubkey_text(line):
        raise PubkeyError(
            "key_file_missing",
            "{} doesn't look like an OpenSSH public key (a private key was "
            "passed?). Point --key at the .pub file.".format(path or "<stdin>"),
        )
    return Pubkey(
        path=path or Path("<stdin>"),
        line=line,
        key_type=parts[0],
        blob=parts[1],
        comment=" ".join(parts[2:]),
        source=source,
    )


def load_pubkey(path: str | Path, *, source: str = "explicit") -> Pubkey:
    """Read + validate one ``.pub`` file.

    Raises :class:`PubkeyError` with ``failure_class="key_file_missing"`` when
    the file is absent, unreadable, empty, or does not start with a known key
    type — the last case is the dangerous one: a caller who passes the
    **private** key would otherwise have it appended verbatim to the remote
    ``authorized_keys``.
    """
    resolved = resolve_key_path(str(path))
    if not resolved.exists() or not os.access(resolved, os.R_OK):
        raise PubkeyError(
            "key_file_missing",
            f"public key not found or unreadable: {resolved}. "
            "Generate one with `ssh-keygen -t ed25519` or pass --key <path>.",
        )
    try:
        with resolved.open("rb") as handle:
            head = handle.read(PUBKEY_PEEK_BYTES)
    except OSError as exc:
        raise PubkeyError(
            "key_file_missing", "public key unreadable: {} ({})".format(resolved, exc)
        )
    text = head.decode("utf-8", errors="replace")
    # Judge the first *key* line, not the raw head: a `.pub` file whose first
    # line is a comment (some organisations prepend provenance) is still a valid
    # public key, and refusing it would be a false negative on a real host.
    if not looks_like_pubkey_text(_first_key_line(text) or text):
        raise PubkeyError(
            "key_file_missing",
            "{} doesn't look like an OpenSSH public key (a private key was "
            "passed?). Point --key at the .pub file.".format(resolved),
        )
    line = _first_key_line(resolved.read_text(encoding="utf-8", errors="replace"))
    parts = line.split()
    if len(parts) < 2:
        raise PubkeyError(
            "key_file_missing",
            "{} has an incomplete public key line (expected 'type base64 [comment]')".format(
                resolved
            ),
        )
    return Pubkey(
        path=resolved,
        line=line,
        key_type=parts[0],
        blob=parts[1],
        comment=" ".join(parts[2:]),
        source=source,
    )


# --- install script ---------------------------------------------------------


def sh_quote(value: str) -> str:
    """Quote ``value`` for POSIX ``sh`` using single quotes."""
    return "'" + value.replace("'", "'\\''") + "'"


def build_install_script(key: Pubkey) -> str:
    """Return the POSIX shell snippet that installs ``key`` idempotently.

    The snippet is intentionally dependency-light and exits non-zero with a
    ``SSHCORE:error:<reason>`` marker on the paths that mean "this account
    cannot hold an authorized_keys file" — a permission problem is a
    different remediation from a wrong password.
    """
    ident = sh_quote(key.identity)
    line = sh_quote(key.line)
    return "\n".join(
        [
            "umask 077",
            'if [ -z "${HOME:-}" ]; then HOME=$(cd ~ 2>/dev/null && pwd) || HOME=; fi',
            '[ -n "${HOME:-}" ] || { echo "SSHCORE:error:no-home"; exit 4; }',
            'SSH_DIR="$HOME/.ssh"',
            'AUTH="$SSH_DIR/authorized_keys"',
            'mkdir -p "$SSH_DIR" 2>/dev/null || { echo "SSHCORE:error:mkdir"; exit 3; }',
            '[ -e "$AUTH" ] || : >>"$AUTH" 2>/dev/null '
            '|| { echo "SSHCORE:error:touch"; exit 3; }',
            "[ -w \"$AUTH\" ] || { echo \"SSHCORE:error:not-writable\"; exit 3; }",
            'if grep -qF ' + ident + ' "$AUTH" 2>/dev/null; then',
            '  echo "' + MARK_ALREADY + '"',
            "else",
            "  printf '%s\\n' " + line + " >>\"$AUTH\" 2>/dev/null "
            '|| { echo "SSHCORE:error:append"; exit 3; }',
            '  echo "' + MARK_INSTALLED + '"',
            "fi",
            'chmod 700 "$SSH_DIR" 2>/dev/null',
            'chmod 600 "$AUTH" 2>/dev/null',
            # A mislabeled ~/.ssh makes sshd ignore the whole directory on
            # enforcing RHEL/Kylin boxes; harmless where restorecon is absent.
            'command -v restorecon >/dev/null 2>&1 && '
            'restorecon -R "$SSH_DIR" >/dev/null 2>&1',
            'echo "' + MARK_HOME + '$HOME"',
            "exit 0",
        ]
    )


@dataclass
class InstallOutcome:
    """What the install script reported."""

    outcome: str | None  # "installed" | "already_installed" | None
    error: str | None  # "mkdir" | "touch" | "append" | "no-home" | ...
    home: str | None

    @property
    def ok(self) -> bool:
        return self.outcome is not None and self.error is None


def parse_install_output(stdout: str) -> InstallOutcome:
    """Parse the ``SSHCORE:`` markers out of the remote stdout."""
    outcome: str | None = None
    error: str | None = None
    home: str | None = None
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if line == MARK_ALREADY:
            outcome = "already_installed"
        elif line == MARK_INSTALLED:
            outcome = "installed"
        elif line.startswith(MARK_ERROR_PREFIX):
            error = line[len(MARK_ERROR_PREFIX):].strip() or "unknown"
        elif line.startswith(MARK_HOME):
            home = line[len(MARK_HOME):].strip()
    return InstallOutcome(outcome=outcome, error=error, home=home)


# --- verification -----------------------------------------------------------


VERIFY_COMMAND = "echo SSHCORE_KEY_OK"
VERIFY_MARKER = "SSHCORE_KEY_OK"


def normalize_identity_path(path: Path) -> str:
    """Render an identity path as ``~/.ssh/...`` when it lives under $HOME.

    ``~/.ssh/config`` is portable and hides the username; absolute paths leak
    it into a file the user may share. Falls back to the absolute path.
    """
    try:
        home = Path(os.path.expanduser("~")).resolve()
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return str(path)
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return str(resolved)
    return "~/" + relative.as_posix()


__all__ = [
    "DEFAULT_KEY",
    "DEFAULT_KEY_CANDIDATES",
    "InstallOutcome",
    "MARK_ALREADY",
    "MARK_ERROR_PREFIX",
    "MARK_HOME",
    "MARK_INSTALLED",
    "PUBKEY_PREFIXES",
    "Pubkey",
    "PubkeyError",
    "VERIFY_COMMAND",
    "VERIFY_MARKER",
    "build_install_script",
    "load_pubkey",
    "looks_like_pubkey_text",
    "normalize_identity_path",
    "parse_install_output",
    "pick_default_pubkey",
    "pubkey_from_text",
    "resolve_key_path",
    "sh_quote",
]
