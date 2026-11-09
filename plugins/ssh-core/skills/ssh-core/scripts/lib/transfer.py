"""Transport selection + scp/rsync fallbacks for the file-transfer scripts.

Before 1.1 ``ssh_upload`` / ``ssh_download`` / ``ssh_server_transfer`` could
only talk SFTP through paramiko. On a host without paramiko (no network, no
wheel, a slim container, this Windows box) all three answered
``dependency_missing`` and the skill had **zero** file-transfer capability,
even though ``scp`` ships with every OpenSSH client (SSH-11).

``--transport auto`` now means:

======================  ==============================================
paramiko importable     SFTP (resume, sha256 verify, bwlimit, progress)
paramiko missing        ``scp -O/-r`` (-o BatchMode=yes) + local sha256
``ssh_server_transfer`` ``ssh -A src 'tar -C … -cf -' | ssh dst 'tar -xf -'``
                        (direct), or ``rsync -e ssh`` with ``--use-rsync``
======================  ==============================================

Every helper returns the same :class:`TransferOutcome` so the three scripts
can keep one envelope shape regardless of transport. The outcome carries both
``bytes_transferred`` and ``files_transferred``; a backend that cannot measure
them must say so, never return a placeholder (BUG-007).

Counting is deliberately centralised:

* :class:`TarEntryCounter` reads the tar headers of a stream that is already
  being relayed, so the server-to-server direct mode and the no-paramiko
  recursive download both report the file payload they actually moved;
* :func:`pump_stream` is the only relay loop, so ``bytes`` cannot drift from
  ``bytes written``;
* ``rsync --stats`` is parsed by :func:`parse_rsync_stats` for ``--use-rsync``.

:func:`probe_remote_path` answers "what is at that destination?" with one ssh
round-trip; ``ssh_upload`` uses it to refuse an unrequested overwrite
(GAP-003) without paying a per-file probe.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import stat as _stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from . import CHILD_TEXT_KWARGS, merged_ssh_config, msys_safe_env
from .askpass import askpass_env
from .auth import auth_options


def have(program: str) -> bool:
    """True when ``program`` is executable somewhere on PATH."""
    return shutil.which(program) is not None


def paramiko_available() -> bool:
    """True when the SFTP backend can be used."""
    try:
        import paramiko  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure means "not usable"
        return False
    return True


@dataclass
class TransferOutcome:
    """Transport-independent result of one upload/download/copy."""

    success: bool
    transport: str
    bytes_transferred: int = 0
    elapsed_s: float = 0.0
    sha256_verified: bool = False
    failure_class: str | None = None
    stderr: str = ""
    argv: list[str] = field(default_factory=list)
    #: How many regular files the transport actually moved (BUG-007). It lives
    #: here, not in the caller, so every backend has to answer it: the direct
    #: tar stream used to report a hardcoded ``1`` for a whole tree.
    files_transferred: int = 1
    detail: str = ""
    #: ``None`` when `--preserve` was not requested, ``True`` when the transport
    #: honoured it (FA-05: scp now passes `-p` instead of pretending).
    preserved: bool | None = None

    @property
    def files(self) -> int:
        """Deprecated alias for :attr:`files_transferred` (kept for callers)."""
        return self.files_transferred

    def as_data(self) -> dict:
        return {
            "transport": self.transport,
            "bytes_transferred": self.bytes_transferred,
            "elapsed_s": round(self.elapsed_s, 3),
            "sha256_verified": self.sha256_verified,
            "failure_class": self.failure_class,
            "files_transferred": self.files_transferred,
        }


# --- sha256 helpers ---------------------------------------------------------


def sha256_file(path: Path, chunk: int = 1 << 20) -> str | None:
    """Local sha256 of ``path`` (None when unreadable)."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(chunk)
                if not block:
                    break
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def remote_sha256(
    alias: str,
    remote_path: str,
    *,
    timeout: int = 120,
    password: str | None = None,
    ssh_config: str | None = None,
    accept_new_hostkey: bool = False,
) -> str | None:
    """sha256 of a remote file, read over a plain ssh exec channel.

    ``password`` (SSHCORE-026 follow-up) makes the verification step possible on
    a password-only host: the channel used to be hardcoded ``BatchMode=yes``, so
    a transfer that succeeded over askpass still reported
    ``sha256_verified: false`` because the hash could not be read at all.
    """
    argv = ["ssh", *auth_options(bool(password)), "-o", "ConnectTimeout=8"]
    if accept_new_hostkey:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    elif password:
        argv += ["-o", "StrictHostKeyChecking=yes"]
    config_path = merged_ssh_config(ssh_config)
    if config_path:
        argv += ["-F", config_path]
    argv += [
        "--", alias,
        f"sha256sum -- {_quote(remote_path)} 2>/dev/null || "
        f"shasum -a 256 -- {_quote(remote_path)}",
    ]
    env = msys_safe_env()
    ctx = None
    if password:
        ctx = askpass_env(password)
        env = ctx.__enter__()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            env=env, **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split()[0]


def _quote(value: str) -> str:
    """Single-quote a path for the remote POSIX shell."""
    return "'" + value.replace("'", "'\\''") + "'"


# --- remote destination probe (GAP-003) -------------------------------------
#
# `ssh_upload` used to overwrite whatever was already at the remote path because
# `scp` does: two uploads to `/tmp/release.tgz` both returned `success: true`
# and the first payload was gone. Refusing by default needs exactly one question
# answered before the copy starts: "is something already there?".
#
# It is **one** extra ssh round-trip per upload, never one per file:
#   * a single file has one destination, so one probe covers it;
#   * `--recursive` uploads *into* a directory (contents land under it), so an
#     existing directory target is normal and nothing is probed. Clobbering an
#     individual file inside the tree is what `--delete`/mirror is for.
#
# The answer is deliberately not a bare bool. `absent` means "the shell said
# there is nothing there"; `dir`/`file`/`symlink` say what is there; `unknown`
# means "this transport cannot tell" (ssh missing, timeout, a shell without
# `test`, a garbled answer), and the caller must **fail closed** rather than
# assume absence — an unanswerable probe is exactly the situation in which a
# default-clobber destroys data.

REMOTE_PATH_DIR = "dir"
REMOTE_PATH_FILE = "file"
REMOTE_PATH_LINK = "symlink"
REMOTE_PATH_ABSENT = "absent"
REMOTE_PATH_UNKNOWN = "unknown"

#: Every kind except these two means "something is already at that path".
_ABSENT_KINDS = frozenset({REMOTE_PATH_ABSENT, REMOTE_PATH_UNKNOWN})

#: Sentinels echoed by the probe; anything else on stdout means "no answer".
_PROBE_TOKENS = {
    "SSHCORE:dir": REMOTE_PATH_DIR,
    "SSHCORE:file": REMOTE_PATH_FILE,
    "SSHCORE:link": REMOTE_PATH_LINK,
    "SSHCORE:absent": REMOTE_PATH_ABSENT,
}


def probe_remote_path(
    alias: str,
    remote_path: str,
    *,
    timeout: int = 30,
    password: str | None = None,
    ssh_config: str | None = None,
    accept_new_hostkey: bool = False,
    port: int | None = None,
    identity: str | None = None,
) -> str:
    """Ask the remote side what is at ``remote_path``. One ssh round-trip.

    Returns one of the ``REMOTE_PATH_*`` kinds. Never raises: a transport
    problem is ``unknown``, which callers treat as "refuse to overwrite".

    ``test -d/-e/-h`` are POSIX and work on busybox/dash as well as bash. The
    symlink case matters twice: a dangling symlink still counts as "something is
    there" (scp would write through it), and a link to a directory downloads
    like the directory it points at.
    """
    return probe_remote_path_detail(
        alias,
        remote_path,
        timeout=timeout,
        password=password,
        ssh_config=ssh_config,
        accept_new_hostkey=accept_new_hostkey,
        port=port,
        identity=identity,
    )[0]


def probe_remote_path_detail(
    alias: str,
    remote_path: str,
    *,
    timeout: int = 30,
    password: str | None = None,
    ssh_config: str | None = None,
    accept_new_hostkey: bool = False,
    port: int | None = None,
    identity: str | None = None,
) -> tuple[str, int, str]:
    """``(kind, ssh_returncode, ssh_stderr)`` for one remote existence probe.

    The return code and stderr are handed back so a caller can tell "the path is
    free" (``kind == absent``, rc 0) from "ssh itself failed" (rc 255): the
    upload guard must report ``network``/``auth`` when the host is unreachable
    instead of blaming the destination.
    """
    if not have("ssh"):
        return REMOTE_PATH_UNKNOWN, 127, "ssh is not on PATH"
    quoted = _quote(remote_path)
    remote_cmd = (
        f"if [ -d {quoted} ]; then echo SSHCORE:dir; "
        f"elif [ -e {quoted} ]; then echo SSHCORE:file; "
        f"elif [ -h {quoted} ]; then echo SSHCORE:link; "
        f"else echo SSHCORE:absent; fi"
    )
    argv = ["ssh", *auth_options(bool(password)), "-o", "ConnectTimeout=8"]
    if accept_new_hostkey:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    elif password:
        argv += ["-o", "StrictHostKeyChecking=yes"]
    if ssh_config:
        argv += ["-F", ssh_config]
    if port is not None:
        argv += ["-p", str(port)]
    if identity:
        argv += ["-i", str(Path(identity).expanduser())]
    argv += ["--", alias, remote_cmd]
    env = msys_safe_env()
    ctx = None
    if password:
        ctx = askpass_env(password)
        env = ctx.__enter__()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            env=env, **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return REMOTE_PATH_UNKNOWN, 255, f"remote probe failed: {exc}"
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
    if proc.returncode != 0:
        return REMOTE_PATH_UNKNOWN, proc.returncode, (proc.stderr or "").strip()
    # A login banner can land on stdout on some appliances, so match a whole
    # line rather than requiring the entire stream to be the sentinel.
    for line in (proc.stdout or "").splitlines():
        kind = _PROBE_TOKENS.get(line.strip())
        if kind is not None:
            return kind, 0, ""
    return REMOTE_PATH_UNKNOWN, 0, "remote probe returned no recognizable answer"


def remote_path_kind(
    alias: str,
    remote_path: str,
    *,
    session: Any = None,
    **kwargs: Any,
) -> str:
    """``probe_remote_path``, using an open SFTP session when there is one."""
    if session is not None:
        try:
            attr = session.stat(remote_path)
        except Exception:  # noqa: BLE001 — a failed stat is "cannot tell"
            return REMOTE_PATH_UNKNOWN
        if attr is None:
            return REMOTE_PATH_ABSENT
        mode = int(getattr(attr, "st_mode", 0) or 0)
        if _stat.S_ISDIR(mode):
            return REMOTE_PATH_DIR
        if _stat.S_ISLNK(mode):
            return REMOTE_PATH_LINK
        return REMOTE_PATH_FILE
    return probe_remote_path(alias, remote_path, **kwargs)


#: ``program`` → does its ``scp`` understand ``-O``? Cached per process.
_SCP_LEGACY_FLAG: dict[str, bool] = {}


def scp_supports_legacy_flag(program: str = "scp") -> bool:
    """True when this ``scp`` understands ``-O`` (OpenSSH 9.0 and newer).

    ``-O`` forces the legacy SCP protocol. It was **introduced in 9.0**, in the
    same release that made SFTP the default, so it is meaningless (and unknown)
    on everything older — Kylin 3 / RHEL 7 / RHEL 8 ship 8.x. Those clients
    already speak the legacy protocol, so omitting the flag is exactly right.

    Measured on 192.168.30.231 (OpenSSH 8.2p1) immediately after installing the
    skill there: `scp -O …` died with ``unknown option -- O``, so **every**
    upload/download through the paramiko-free backend failed on that host while
    the same code worked on Windows (9.5p1). The probe is one cheap spawn,
    cached for the life of the process.
    """
    cached = _SCP_LEGACY_FLAG.get(program)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [program, "-O"], capture_output=True, text=True, timeout=10,
            check=False, env=msys_safe_env(), **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        _SCP_LEGACY_FLAG[program] = False
        return False
    blob = f"{proc.stdout or ''}{proc.stderr or ''}".lower()
    supported = not ("unknown option" in blob or "illegal option" in blob
                     or "invalid option" in blob)
    _SCP_LEGACY_FLAG[program] = supported
    return supported


# --- scp backend ------------------------------------------------------------


def scp_argv(
    source: str,
    destination: str,
    *,
    recursive: bool = False,
    port: int | None = None,
    identity: str | None = None,
    ssh_config: str | None = None,
    ssh_options: list[str] | None = None,
    accept_new_hostkey: bool = False,
    preserve: bool = False,
    password_auth: bool = False,
) -> list[str]:
    """Build a modern ``scp`` argv (-O for the pre-SFTP protocol).

    ``accept_new_hostkey`` is threaded through here (FA-09): the flag used to be
    accepted by the CLI and silently dropped on this route, so a first-contact
    deploy over scp still failed with ``host_key``.

    ``preserve`` adds ``-p`` (FA-05): ``--preserve`` used to be a no-op that
    still reported ``success: true`` whenever paramiko was absent. ``scp -p``
    keeps mtime and the permission bits, which is exactly the documented
    ``cp -p`` semantics.

    ``password_auth`` swaps ``BatchMode=yes`` for the explicit password methods
    (SSHCORE-026 follow-up). The secret itself never appears here — the caller
    spawns with ``SSH_ASKPASS`` — but scp has to be *told* not to require a
    publickey, or it aborts before the helper is ever consulted.

    ``-O`` is added only when this ``scp`` knows it (:func:`scp_supports_legacy_flag`):
    OpenSSH < 9.0 rejects it outright, and those clients already use the legacy
    protocol, so the flag buys nothing there.
    """
    argv = ["scp"]
    if scp_supports_legacy_flag():
        argv.append("-O")
    argv += [*auth_options(password_auth), "-o", "ConnectTimeout=8"]
    if accept_new_hostkey:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    elif password_auth:
        argv += ["-o", "StrictHostKeyChecking=yes"]
    if recursive:
        argv.append("-r")
    if preserve:
        argv.append("-p")
    if port is not None:
        argv += ["-P", str(port)]
    if identity:
        argv += ["-i", str(Path(identity).expanduser())]
    if ssh_config:
        argv += ["-F", ssh_config]
    for option in ssh_options or []:
        argv += ["-o", option]
    argv += ["--", source, destination]
    return argv


# --- tar stream accounting (BUG-006 / BUG-007) ------------------------------


class TarEntryCounter:
    """Count the regular files — and their payload bytes — inside a tar stream.

    ``tar -tv`` on the producer side would cost a *second* full read of the
    tree (and, server-to-server, a second ssh round-trip), so the archive is
    counted where it already is: in the bytes this process relays. That is both
    cheaper and exact for the entries that actually moved, which is what the
    envelope claims.

    ``bytes`` counts **file payload**, not archive bytes: a 6-byte file occupies
    1536 bytes of tar (a header, a padded block and a share of the
    end-of-archive blocks), while every other transport in this skill reports
    the content size. ``stream`` mode counts the chunks it writes into each
    destination file, so ``--mode direct`` has to answer the same number or two
    modes disagree for identical trees.

    Parsing is incremental and tolerant: a 512-byte header is buffered, its
    typeflag says whether the entry is a regular file, and the stream is then
    skipped to the next header. PAX/GNU long-name extensions are skipped by
    size, so names are never decoded. Anything unexpected (truncated header,
    corrupt size field) stops the accounting instead of raising — the transfer
    itself is still valid, and a wrong number is worse than a missing one.
    """

    BLOCK = 512

    def __init__(self) -> None:
        self.files = 0
        self.bytes = 0
        self._header = b""
        self._skip = 0
        self._stopped = False

    @property
    def stopped(self) -> bool:
        """True when the stream was not a parseable tar (counts are a lower bound)."""
        return self._stopped

    def feed(self, block: bytes) -> None:
        if self._stopped or not block:
            return
        try:
            offset = 0
            while offset < len(block):
                if self._skip:
                    step = min(self._skip, len(block) - offset)
                    self._skip -= step
                    offset += step
                    continue
                room = self.BLOCK - len(self._header)
                piece = block[offset:offset + room]
                self._header += piece
                offset += len(piece)
                if len(self._header) < self.BLOCK:
                    continue
                header, self._header = self._header, b""
                self._consume_header(header)
        except Exception:  # noqa: BLE001 — accounting must never break a copy
            self._stopped = True

    def _consume_header(self, header: bytes) -> None:
        if header == b"\0" * self.BLOCK:
            # End-of-archive marker; keep reading harmlessly (padding follows).
            return
        size_field = header[124:136].split(b"\0", 1)[0].strip() or b"0"
        try:
            size = int(size_field, 8)
        except ValueError:
            self._stopped = True
            return
        if size < 0:
            self._stopped = True
            return
        if header[156:157] in (b"0", b"\0", b"7"):
            self.files += 1
            self.bytes += size
        if size:
            # Tar pads every entry to a 512-byte boundary.
            self._skip = size + (-size % self.BLOCK)


class StreamTimeout(RuntimeError):
    """A relayed stream ran past its deadline."""


def pump_stream(
    source: IO[bytes] | None,
    sink: IO[bytes] | None,
    *,
    counter: TarEntryCounter | None = None,
    bwlimit_kbs: int | None = None,
    started: float | None = None,
    timeout: float | None = None,
) -> int:
    """Relay ``source`` into ``sink`` while counting (BUG-006 / BUG-007).

    Returns the number of bytes actually written. This is the only place a tar
    stream is measured, so "bytes moved" cannot drift from "bytes reported":

    * ``counter`` sees every chunk, which is where the file count comes from;
    * ``bwlimit_kbs`` throttles the relay itself — the only point where a rate
      can be enforced when neither endpoint supports a cap;
    * ``timeout`` raises :class:`StreamTimeout` so the caller can kill both
      ends; the caller's ``finally`` still runs.

    The sink is closed when the source ends, even after an error: the consumer
    finishes only when it sees EOF, so a dead producer must not leave it hanging
    until its own timeout.
    """
    written = 0
    limit_bps = bwlimit_kbs * 1024 if bwlimit_kbs else None
    try:
        if source is not None and sink is not None:
            while True:
                block = source.read(1 << 20)
                if not block:
                    break
                if counter is not None:
                    counter.feed(block)
                try:
                    sink.write(block)
                    sink.flush()
                except (BrokenPipeError, OSError, ValueError):
                    # The consumer exited early; its exit code and stderr say why.
                    break
                written += len(block)
                if limit_bps and started is not None:
                    expected = written / limit_bps
                    actual = time.monotonic() - started
                    if expected > actual:
                        time.sleep(expected - actual)
                if (
                    timeout is not None
                    and started is not None
                    and time.monotonic() - started > timeout
                ):
                    raise StreamTimeout(f"stream exceeded {timeout}s")
    finally:
        if sink is not None:
            with contextlib.suppress(OSError, ValueError):
                sink.close()
        if source is not None:
            with contextlib.suppress(OSError, ValueError):
                source.close()
    return written


def _split_remote_path(remote_path: str) -> tuple[str, str]:
    """``/a/b/c`` → ``("/a/b", "c")``; ``/`` → ``("/", "/")``."""
    trimmed = remote_path.rstrip("/") or "/"
    parent, _, leaf = trimmed.rpartition("/")
    return (parent or "/"), (leaf or trimmed)


def _classify(stderr: str) -> str:
    lowered = (stderr or "").lower()
    if "permission denied" in lowered:
        return "permission"
    if "no such file" in lowered or "not found" in lowered:
        return "remote_path_missing"
    if "host key verification failed" in lowered:
        return "host_key"
    if "could not resolve" in lowered or "connection refused" in lowered:
        return "network"
    if "not a directory" in lowered or "cannot create" in lowered:
        return "remote_path_invalid"
    return "transfer_io"


def run_scp(
    source: str,
    destination: str,
    *,
    recursive: bool = False,
    timeout: int = 600,
    port: int | None = None,
    identity: str | None = None,
    ssh_config: str | None = None,
    ssh_options: list[str] | None = None,
    accept_new_hostkey: bool = False,
    preserve: bool = False,
    local_path: Path | None = None,
    verify_remote: tuple[str, str] | None = None,
    delete_local_on_failure: bool = False,
    password: str | None = None,
) -> TransferOutcome:
    """Copy with ``scp`` and, when possible, prove the bytes with sha256.

    ``password`` runs scp under ``SSH_ASKPASS`` instead of ``BatchMode=yes``
    (SSHCORE-026 follow-up), which is what makes the paramiko-free backend
    usable on a password-only host. The secret is written to a 0600 helper and
    deleted in a ``finally``; it never enters argv.
    """
    if not have("scp"):
        return TransferOutcome(
            success=False,
            transport="scp",
            failure_class="dependency_missing",
            stderr="scp not found on PATH (OpenSSH client missing?)",
        )
    argv = scp_argv(
        source, destination,
        recursive=recursive, port=port, identity=identity,
        ssh_config=ssh_config, ssh_options=ssh_options,
        accept_new_hostkey=accept_new_hostkey, preserve=preserve,
        password_auth=bool(password),
    )
    started = time.monotonic()
    env = msys_safe_env()
    ctx = None
    if password:
        ctx = askpass_env(password)
        env = ctx.__enter__()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            env=env, **CHILD_TEXT_KWARGS,
        )
    except subprocess.TimeoutExpired:
        return TransferOutcome(
            success=False, transport="scp", failure_class="timeout",
            stderr=f"scp timed out after {timeout}s", argv=argv,
        )
    except OSError as exc:
        return TransferOutcome(
            success=False, transport="scp", failure_class="dependency_missing",
            stderr=f"failed to spawn scp: {exc}", argv=argv,
        )
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        if delete_local_on_failure and local_path is not None:
            try:
                local_path.unlink()
            except OSError:
                pass
        return TransferOutcome(
            success=False, transport="scp",
            failure_class=_classify(proc.stderr), stderr=proc.stderr.strip(),
            argv=argv, elapsed_s=elapsed,
        )

    size = 0
    if local_path is not None and local_path.is_file():
        size = local_path.stat().st_size
    verified = False
    detail = ""
    if local_path is not None and verify_remote is not None:
        alias, remote_path = verify_remote
        local_hash = sha256_file(local_path)
        # Same auth profile as the copy: on a password host the hash must be
        # readable, or `sha256_verified` silently degrades to false.
        remote_hash = remote_sha256(
            alias, remote_path, password=password, ssh_config=ssh_config,
            accept_new_hostkey=accept_new_hostkey,
        )
        if local_hash and remote_hash:
            verified = local_hash == remote_hash
            if not verified:
                return TransferOutcome(
                    success=False, transport="scp", failure_class="integrity_mismatch",
                    stderr=(
                        "sha256 mismatch after scp: "
                        f"local={local_hash} remote={remote_hash}"
                    ),
                    argv=argv, elapsed_s=elapsed, bytes_transferred=size,
                )
        else:
            detail = "sha256 verification unavailable (no sha256sum/shasum remotely)"
    return TransferOutcome(
        success=True, transport="scp", bytes_transferred=size,
        elapsed_s=elapsed, sha256_verified=verified, argv=argv, detail=detail,
        preserved=True if preserve else None,
    )


# --- rsync / tar-stream backend for server-to-server ------------------------


def run_rsync(
    src_alias: str,
    src_path: str,
    dst_alias: str,
    dst_path: str,
    *,
    recursive: bool = False,
    bwlimit_kbs: int | None = None,
    timeout: int = 1800,
    ssh_config: str | None = None,
    ssh_options: list[str] | None = None,
    accept_new_hostkey: bool = False,
    password: str | None = None,
) -> TransferOutcome:
    """``rsync`` **on the source host**, pushing to the destination (SSH-14).

    rsync refuses a remote source and a remote destination in the same command
    ("The source and destination cannot both be remote"), so the old argv —
    ``rsync … src:/a dst:/b`` from this machine — could never work (FA-04). The
    copy therefore starts where the bytes are::

        ssh <src_alias> "rsync -a -e 'ssh -o BatchMode=yes …' src_path dst_alias:dst_path"

    ``ssh_config`` is forwarded to the inner ``ssh`` as ``-F`` when it is a path
    that also exists on the source host; when it does not (the usual case — a
    client-side config), the remote end falls back to its own ``~/.ssh/config``
    and to the agent, which is the only thing that can authenticate anyway.

    The bytes still never touch this machine's disk; only the inner ssh
    connection's traffic passes through it.
    """
    ssh_flavour = [*auth_options(bool(password)), "-o", "ConnectTimeout=8"]
    if accept_new_hostkey:
        ssh_flavour += ["-o", "StrictHostKeyChecking=accept-new"]
    for option in ssh_options or []:
        ssh_flavour += ["-o", option]
    # ``--stats`` is what makes the envelope honest (BUG-007): rsync already
    # knows how many files it moved and how many bytes it sent, and asking it
    # costs nothing. Without it `ssh_server_transfer --use-rsync` had to invent
    # both numbers.
    remote_rsync = "rsync -a --stats"
    if bwlimit_kbs:
        remote_rsync += f" --bwlimit={int(bwlimit_kbs)}"
    inner_ssh = " ".join(["ssh", *(_quote(token) for token in ssh_flavour)])
    remote_rsync += f" -e {_quote(inner_ssh)}"
    remote_rsync += f" -- {_quote(src_path)} {_quote(dst_alias + ':' + dst_path)}"

    base: list[str] = ["ssh", *ssh_flavour]
    if ssh_config:
        base += ["-F", ssh_config]
    argv = [*base, "--", src_alias, remote_rsync]
    started = time.monotonic()
    env = msys_safe_env()
    ctx = None
    if password:
        ctx = askpass_env(password)
        env = ctx.__enter__()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            env=env, **CHILD_TEXT_KWARGS,
        )
    except subprocess.TimeoutExpired:
        return TransferOutcome(
            success=False, transport="rsync", failure_class="timeout",
            stderr=f"rsync timed out after {timeout}s", argv=argv,
        )
    except OSError as exc:
        return TransferOutcome(
            success=False, transport="rsync", failure_class="dependency_missing",
            stderr=f"failed to spawn ssh for rsync: {exc}", argv=argv,
        )
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        lowered = stderr.lower()
        if "command not found" in lowered or "rsync: not found" in lowered:
            return TransferOutcome(
                success=False, transport="rsync",
                failure_class="dependency_missing",
                stderr=(
                    "rsync is not installed on the source host "
                    f"({src_alias}); use the tar fallback instead: "
                    f"{stderr}"
                ),
                argv=argv, elapsed_s=elapsed,
            )
        return TransferOutcome(
            success=False, transport="rsync",
            failure_class=_classify(stderr) if proc.returncode == 255
            else "transfer_io",
            stderr=stderr or f"rsync exited {proc.returncode}",
            argv=argv, elapsed_s=elapsed,
        )
    files, nbytes = parse_rsync_stats(proc.stdout or "")
    return TransferOutcome(
        success=True, transport="rsync", elapsed_s=elapsed, argv=argv,
        bytes_transferred=nbytes, files_transferred=files,
        detail=(
            "rsync ran on the source host (incremental copy); both endpoints "
            "are remote, so it cannot run from this machine"
        ),
    )


def parse_rsync_stats(output: str) -> tuple[int, int]:
    """``(files, bytes)`` from ``rsync --stats`` on stdout.

    Measured shape (rsync 3.x, single file and 2-file tree)::

        Number of regular files transferred: 2
        Total file size: 4 bytes

    ``Total file size`` is the size of the files in the transfer, which is the
    same thing ``stream`` mode counts as ``bytes``. ``Total bytes sent`` would
    be wire bytes (231 for a 4-byte tree), so it is deliberately not used. A
    missing/old rsync yields ``(0, 0)`` rather than an invented number.
    """
    files = 0
    nbytes = 0
    for line in (output or "").splitlines():
        text = line.strip()
        if text.startswith("Number of regular files transferred:"):
            files = _leading_int(text.rpartition(":")[2])
        elif text.startswith("Total file size:"):
            nbytes = _leading_int(text.rpartition(":")[2])
    return files, nbytes


def _leading_int(text: str) -> int:
    """``" 4 bytes"`` → ``4``; anything unparseable → ``0``."""
    match = re.search(r"\d[\d,]*", text or "")
    if match is None:
        return 0
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return 0


def run_tar_stream(
    src_alias: str,
    src_path: str,
    dst_alias: str,
    dst_path: str,
    *,
    recursive: bool = False,
    timeout: int = 1800,
    bwlimit_kbs: int | None = None,
    ssh_config: str | None = None,
    accept_new_hostkey: bool = False,
    password: str | None = None,
) -> TransferOutcome:
    """Direct server-to-server copy: ``ssh src 'tar -cf -' | ssh dst 'tar -xf -'``.

    The cheapest "direct" transport that needs no extra daemon and works with
    nothing but the OpenSSH client: bytes flow src → this host → dst, but they
    are never staged to disk.

    ``password`` puts both hops under the same ``SSH_ASKPASS`` helper
    (SSHCORE-026 follow-up); both endpoints normally share the credential, and
    a single helper directory is created per call either way.
    """
    if not (have("ssh") and have("tar")):
        return TransferOutcome(
            success=False, transport="direct", failure_class="dependency_missing",
            stderr="ssh/tar missing on this host",
        )
    base = [*auth_options(bool(password)), "-o", "ConnectTimeout=8"]
    if accept_new_hostkey:
        base += ["-o", "StrictHostKeyChecking=accept-new"]
    elif password:
        base += ["-o", "StrictHostKeyChecking=yes"]
    if ssh_config:
        base += ["-F", ssh_config]
    src_dir, leaf = _split_remote_path(src_path)
    producer = [
        "ssh", *base, "--", src_alias,
        f"tar -C {_quote(src_dir)} -cf - {_quote(leaf)}",
    ]
    # Extract into a private staging directory first, then move into place.
    #
    # Extracting straight into the destination directory is destructive when
    # source and destination share a directory (the common same-host case):
    # `tar -C /tmp -xf -` recreates `/tmp/<leaf>` — which *is* the source — and
    # the follow-up `mv` then moves the source file away. Measured: a
    # `--mode direct` copy of /tmp/x to /tmp/y deleted /tmp/x.
    dest_dir = (
        dst_path.rstrip("/") or "/" if recursive
        else (dst_path.rstrip("/").rpartition("/")[0] or "/")
    )
    wanted = dst_path.rstrip("/").rsplit("/", 1)[-1] or leaf
    if recursive:
        # Recursive semantics match `stream` mode: the *contents* of src land in
        # dst_path (`dst/a.txt`, `dst/sub/b.txt`), not `dst/<src-basename>/…`.
        consumer_cmd = (
            f"mkdir -p {_quote(dest_dir)} && "
            f"tmp=$(mktemp -d) && "
            f"tar -C \"$tmp\" -xf - && "
            f"cp -a \"$tmp/{leaf}/.\" {_quote(dest_dir)}/ && "
            f"rm -rf \"$tmp\""
        )
    else:
        consumer_cmd = (
            f"mkdir -p {_quote(dest_dir)} && "
            f"tmp=$(mktemp -d) && "
            f"tar -C \"$tmp\" -xf - && "
            f"mv -f \"$tmp/{leaf}\" {_quote(dest_dir.rstrip('/') + '/' + wanted)} && "
            f"rm -rf \"$tmp\""
        )
    consumer = ["ssh", *base, "--", dst_alias, consumer_cmd]
    started = time.monotonic()
    env = msys_safe_env()
    ctx = None
    if password:
        ctx = askpass_env(password)
        env = ctx.__enter__()
    counter = TarEntryCounter()
    relayed = 0
    try:
        src_proc = subprocess.Popen(
            producer, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        # ``stdin=PIPE`` (not the producer's own handle): the bytes have to pass
        # through this process to be counted, and that is exactly what the
        # envelope promises. ``stdout`` is discarded — the consumer prints
        # nothing, and an undrained pipe is a deadlock waiting for a slow day.
        dst_proc = subprocess.Popen(
            consumer, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=env,
        )
        src_err = StreamDrain(src_proc.stderr)
        dst_err = StreamDrain(dst_proc.stderr)
        try:
            pump_stream(
                src_proc.stdout, dst_proc.stdin,
                counter=counter, bwlimit_kbs=bwlimit_kbs,
                started=started, timeout=timeout,
            )
        except StreamTimeout:
            src_proc.kill()
            dst_proc.kill()
            return TransferOutcome(
                success=False, transport="direct", failure_class="timeout",
                stderr=f"direct tar stream exceeded {timeout}s",
                argv=producer + consumer, elapsed_s=time.monotonic() - started,
            )
        src_err.join()
        dst_err.join()
        dst_rc = dst_proc.wait(timeout=remaining_timeout(started, timeout))
        src_rc = src_proc.wait(timeout=remaining_timeout(started, timeout))
    except (OSError, subprocess.SubprocessError) as exc:
        return TransferOutcome(
            success=False, transport="direct", failure_class="transfer_io",
            stderr=f"tar stream failed: {exc}",
        )
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
    elapsed = time.monotonic() - started
    if src_rc != 0 or dst_rc != 0:
        text = (src_err.text() + dst_err.text()).strip()
        fclass = _classify(text)
        # "mv: cannot stat" means the copy itself worked but the rename did
        # not; that is still a failed transfer, just with a clearer cause.
        if "cannot stat" in text:
            fclass = "dst_path_invalid"
        return TransferOutcome(
            success=False, transport="direct", failure_class=fclass,
            stderr=text[:2000], elapsed_s=elapsed,
        )
    # BUG-007: the counts come from the archive itself, not from a hardcoded 1.
    # ``counter.bytes`` is the payload of the regular files that went through,
    # which is the number every other transport in this skill reports (a 6-byte
    # file costs 1536 bytes of tar, so relaying archive bytes would be a lie).
    detail = "streamed with tar over ssh (no local staging)"
    if counter.stopped:
        detail += "; tar accounting incomplete (unparseable archive stream)"
    return TransferOutcome(
        success=True, transport="direct",
        bytes_transferred=counter.bytes,
        files_transferred=counter.files,
        elapsed_s=elapsed,
        detail=detail,
        # Not a claim of verification: tar over ssh has no hash step.
        sha256_verified=False,
        argv=producer + consumer,
    )


class StreamDrain:
    """Read a child's stderr to EOF on a daemon thread.

    Both ends of a piped transfer have an stderr pipe. Reading them only after
    ``wait()`` deadlocks as soon as one of them says more than a pipe buffer's
    worth (a failing ``tar`` over a big tree does), and the timeout then reports
    a hang instead of the actual error. Draining while the pipe is live is the
    only way to keep both the bytes and the diagnosis.
    """

    def __init__(self, stream: IO[bytes] | None) -> None:
        self._chunks: list[bytes] = []
        self._thread: threading.Thread | None = None
        if stream is not None:
            self._thread = threading.Thread(target=self._run, args=(stream,), daemon=True)
            self._thread.start()

    def _run(self, stream: IO[bytes]) -> None:
        try:
            for block in iter(lambda: stream.read(65536), b""):
                self._chunks.append(block)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    def join(self, timeout: float | None = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", "replace")


def remaining_timeout(started: float, timeout: float) -> float:
    return max(1.0, timeout - (time.monotonic() - started))


__all__ = [
    "REMOTE_PATH_ABSENT",
    "REMOTE_PATH_DIR",
    "REMOTE_PATH_FILE",
    "REMOTE_PATH_LINK",
    "REMOTE_PATH_UNKNOWN",
    "StreamDrain",
    "StreamTimeout",
    "TarEntryCounter",
    "TransferOutcome",
    "have",
    "paramiko_available",
    "parse_rsync_stats",
    "probe_remote_path",
    "probe_remote_path_detail",
    "pump_stream",
    "remote_path_kind",
    "remote_sha256",
    "run_rsync",
    "run_scp",
    "run_tar_stream",
    "scp_argv",
    "sha256_file",
]
