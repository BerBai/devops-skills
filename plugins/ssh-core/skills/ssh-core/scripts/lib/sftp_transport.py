"""sftp_transport.py — shared SFTP transport for ssh_upload / ssh_download.

Two layers:

* **Low-level** ``SftpSession`` — wraps a single ``paramiko.SSHClient`` +
  ``SFTPClient`` pair for one alias. Exposes ``stat`` / ``mkdir_p`` /
  ``chmod`` / ``put_chunked`` / ``get_chunked``. ``put_chunked`` and
  ``get_chunked`` are the only places that touch the SFTP socket in a
  hot loop; they implement chunk-by-chunk transfer with optional
  bandwidth throttling and a per-chunk progress callback.

* **High-level** ``upload_file`` / ``upload_tree`` / ``download_file`` /
  ``download_tree`` — pure functions returning a shared
  ``TransferResult`` dataclass. Scripts (``ssh_upload.py`` /
  ``ssh_download.py``) are thin CLI wrappers that build a
  ``SftpSession``, call one of these, and translate the result into the
  shared JSON envelope.

The four high-level functions are fully implemented (resume, sha256
verification, tree walk, bwlimit, progress). This module is the *SFTP*
backend only: when paramiko is unavailable the scripts fall back to
``scp``/``rsync`` through :mod:`lib.transfer` (SSH-11), so a paramiko-less
host still has file transfer.

Helpers:

* ``make_pct_callback(filename, total, no_progress)`` — 5%-bucket progress
  callback printing to stderr.
* ``sha256_streaming_local(path)`` / ``sha256_streaming_remote(session, path)``
  — streaming SHA-256 of a local / remote file using the configured
  block size.

See:
    references/workflows.md §3 — transfer recipes and transport choice
    references/troubleshooting.md — "dependency_missing" (no paramiko)
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import posixpath
import stat as stat_mod
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # SSHCORE-004: paramiko is a soft dependency.
    import paramiko  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — exercised in the dependency-missing tests
    paramiko = None  # type: ignore[assignment]

# Exception classes re-exported so scripts can write ``except`` clauses even
# when paramiko is absent (the import at the top would otherwise abort
# ``--help``). When paramiko is missing these stubs are never raised —
# SftpSession raises DependencyMissingError before any I/O.
if paramiko is not None:
    AuthenticationException = paramiko.AuthenticationException
    SSHException = paramiko.SSHException
else:

    class AuthenticationException(Exception):  # type: ignore[no-redef]
        """Placeholder when paramiko is not installed."""

    class SSHException(Exception):  # type: ignore[no-redef]
        """Placeholder when paramiko is not installed."""


PARAMIKO_INSTALL_HINT = (
    "ssh-core: SFTP transport requires paramiko (pip install 'paramiko>=3.0,<4'). "
    "Command execution (ssh_execute / ssh_cluster / ssh_tunnel) does not need it."
)


class DependencyMissingError(RuntimeError):
    """Raised when an optional runtime dependency (paramiko) is absent."""

    failure_class = "dependency_missing"
    exit_code = 3

    def __init__(self, message: str = PARAMIKO_INSTALL_HINT) -> None:
        super().__init__(message)


def require_paramiko() -> object:
    """Return the paramiko module or raise :class:`DependencyMissingError`."""
    if paramiko is None:
        raise DependencyMissingError()
    return paramiko


class HostKeyRejectedError(Exception):
    """Host key is unknown or has changed — connection refused (SSHCORE-011)."""

    failure_class = "host_key"
    exit_code = 1


_MissingHostKeyPolicyBase = (
    paramiko.MissingHostKeyPolicy if paramiko is not None else object
)


class _AcceptNewOncePolicy(_MissingHostKeyPolicyBase):  # type: ignore[misc,valid-type]
    """Trust + persist a host key the first time only (accept-new style).

    Unlike ``AutoAddPolicy`` this never overrides a *mismatch*: paramiko
    only consults a missing-host-key policy when the host is absent from
    known_hosts, so a changed key still raises ``BadHostKeyException``.
    """

    def __init__(self, known_hosts_path: Path) -> None:
        self._path = known_hosts_path

    def missing_host_key(self, client, hostname, key) -> None:  # type: ignore[no-untyped-def]
        client._host_keys.add(hostname, key.get_name(), key)
        if client._host_keys_filename is not None:
            client.save_host_keys(client._host_keys_filename)
        entry = f"{hostname} {key.get_name()} {key.get_base64()}\n"
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(entry)
            self._path.chmod(0o600)
        except OSError:
            # Persistence is best-effort; trusting for this connection
            # already happened and the caller opted in.
            pass


DEFAULT_BLOCK_SIZE = 131072  # 128 KiB, matches ssh_upload.py surface default
SHA256_READ_BLOCK = 131072   # 128 KiB streaming read for verify path


# ---------------------------------------------------------------------------
# TransferResult dataclass — shared shape returned by every high-level call.
# ---------------------------------------------------------------------------


@dataclass
class TransferResult:
    """Outcome of a high-level upload/download call.

    The script-shell maps these fields onto the JSON envelope (see
    ``ssh_upload.py`` / ``ssh_download.py``). Fields are populated
    conditionally — for instance ``sha256_local`` / ``sha256_remote`` are
    only set on the resume-verify path, and ``failed_files`` is only set
    for recursive tree calls.
    """

    success: bool
    exit_code: int
    bytes_transferred: int = 0
    elapsed_s: float = 0.0
    sha256_verified: bool = False
    sha256_local: str | None = None
    sha256_remote: str | None = None
    bytes_resumed_from: int | None = None
    failed_files: list[dict[str, Any]] | None = None
    failure_class: str | None = None
    stderr: str = ""
    #: ``--preserve`` (DV-19): True when the local mtime/mode were applied to
    #: the remote side, False when the server refused SETSTAT, None when the
    #: caller did not ask for preservation.
    preserved: bool | None = None
    #: ``--delete`` (DV-19): paths removed on the remote side to mirror the
    #: source, so a mirror run can be reviewed after the fact.
    deleted_files: list[str] | None = None


# ---------------------------------------------------------------------------
# SftpSession — low-level wrapper around paramiko's SSHClient + SFTPClient.
# ---------------------------------------------------------------------------


class SftpSession:
    """One SFTP session per script invocation.

    Resolves the host via ``~/.ssh/config`` (or the path passed to
    ``ssh_config``) using ``paramiko.SSHConfig``. ``ProxyJump`` is
    supported via ``paramiko.ProxyCommand`` when present in config.

    Host-key policy (SSHCORE-011): identical to the exec route.
    ``load_system_host_keys()`` strict-matches anything in
    ``~/.ssh/known_hosts``; unknown hosts are **rejected** by default
    (``RejectPolicy``). ``accept_new_hostkey=True`` reproduces OpenSSH's
    ``StrictHostKeyChecking=accept-new``: trust once on first contact and
    persist the key, but still fail when an existing key no longer
    matches. ``AutoAddPolicy`` is never used — it would silently re-trust
    a changed key on every connect.

    Auth: ssh-agent + key auth (password auth is the pty session's job;
    mixing them here would need the password in process memory).
    """

    def __init__(
        self,
        alias: str,
        *,
        ssh_config: str | None = None,
        accept_new_hostkey: bool = False,
        known_hosts: str | None = None,
        port: int | None = None,
        identity: str | None = None,
    ) -> None:
        if paramiko is None:
            raise DependencyMissingError()
        self.alias = alias
        self.accept_new_hostkey = accept_new_hostkey
        self.known_hosts_path = Path(
            known_hosts or "~/.ssh/known_hosts"
        ).expanduser()
        self._client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None

        config_path = Path(ssh_config or "~/.ssh/config").expanduser()
        ssh_config_obj = paramiko.SSHConfig()
        if config_path.exists():
            with open(config_path) as fh:
                ssh_config_obj.parse(fh)

        resolved = ssh_config_obj.lookup(alias)

        hostname = resolved.get("hostname", alias)
        username = resolved.get("user")
        # FA-11: `--port` / `--identity` were accepted, documented, and then
        # dropped on the SFTP backend — only the scp/tar branches forwarded
        # them. The explicit flags now win over ~/.ssh/config.
        resolved_port = port if port is not None else resolved.get("port", 22)
        port = int(resolved_port or 22)
        identity_files = [identity] if identity else (
            resolved.get("identityfile") or []
        )
        # paramiko.SSHConfig returns identityfile as a list; expand ~ ourselves.
        key_filenames = [str(Path(p).expanduser()) for p in identity_files]

        proxy_command = resolved.get("proxycommand")
        proxy_jump = resolved.get("proxyjump")
        sock: paramiko.ProxyCommand | None = None
        if proxy_command:
            sock = paramiko.ProxyCommand(proxy_command)
        elif proxy_jump:
            # Convert ProxyJump alias(es) into an ssh -W proxy command. paramiko
            # 3.x does not auto-expand ProxyJump from ~/.ssh/config, so we
            # synthesize the equivalent ProxyCommand that native ssh would build.
            # Multiple comma-separated hops are joined back to ssh -J syntax.
            sock = paramiko.ProxyCommand(
                f"ssh -W {hostname}:{port} -J {proxy_jump}"
            )

        connect_kwargs: dict[str, Any] = {
            "hostname": hostname,
            "port": port,
            "allow_agent": True,
            "look_for_keys": True,
        }
        if username:
            connect_kwargs["username"] = username
        if key_filenames:
            connect_kwargs["key_filename"] = key_filenames
        if sock is not None:
            connect_kwargs["sock"] = sock

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # An explicit known_hosts (tests, per-project trust store) is loaded
        # *in addition* to the system one; parameterised host key messages
        # would otherwise have no effect.
        if known_hosts and self.known_hosts_path.is_file():
            with contextlib.suppress(IOError, OSError):
                client.load_host_keys(str(self.known_hosts_path))
        if accept_new_hostkey:
            # Custom policy: save unknown keys once (accept-new semantics),
            # but keep rejecting mismatches against known hosts.
            client.set_missing_host_key_policy(_AcceptNewOncePolicy(self.known_hosts_path))
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(**connect_kwargs)
        except paramiko.BadHostKeyException as e:
            raise HostKeyRejectedError(
                # paramiko's BadHostKeyException repr embeds two key objects;
                # keep the message about the *host*, not the internals.
                f"host key for {alias} does not match known_hosts; refusing to "
                "connect. Verify the change with `ssh-keyscan <host>`, then "
                "remove the stale entry with `ssh-keygen -R <host>`."
            ) from e
        except paramiko.SSHException as e:
            message = str(e)
            if "not found in known_hosts" in message or "Unknown server" in message:
                raise HostKeyRejectedError(
                    f"host key for {alias} is not trusted ({message}). "
                    "Rerun with --accept-new-hostkey after verifying the "
                    "fingerprint, or add it with ssh-keyscan."
                ) from e
            raise

        self._client = client
        self.sftp = client.open_sftp()

    def __enter__(self) -> SftpSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.sftp is not None:
            with contextlib.suppress(Exception):
                self.sftp.close()
            self.sftp = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Filesystem primitives
    # ------------------------------------------------------------------

    def stat(self, remote_path: str) -> paramiko.SFTPAttributes | None:
        """Return SFTPAttributes for ``remote_path`` or ``None`` if missing.

        ``None`` is the canonical "file does not exist" signal consumed by
        the resume path in ``upload_file`` / ``download_file``. Any other
        error (permission denied, network) propagates the underlying
        ``IOError``.
        """
        assert self.sftp is not None
        try:
            return self.sftp.stat(remote_path)
        except FileNotFoundError:
            return None
        except OSError as e:
            if getattr(e, "errno", None) == errno.ENOENT:
                return None
            raise

    def mkdir_p(self, remote_dir: str, mode: int = 0o755) -> None:
        """Idempotent mkdir. Ignores EEXIST (17) and EISDIR-style 21."""
        assert self.sftp is not None
        try:
            self.sftp.mkdir(remote_dir, mode=mode)
        except OSError as e:
            err = getattr(e, "errno", None)
            if err in (errno.EEXIST, 21):
                return
            # paramiko sometimes raises with errno=None and a textual message;
            # double-check by stat-ing — if it exists, treat as success.
            if self.stat(remote_dir) is not None:
                return
            raise

    def chmod(self, remote_path: str, mode: int) -> None:
        assert self.sftp is not None
        self.sftp.chmod(remote_path, mode)

    def utime(self, remote_path: str, times: tuple[float, float]) -> bool:
        """Set the remote access/modify times. Returns False when unsupported.

        ``--preserve`` promises "the remote mtime equals the local one". The
        SFTP ``SETSTAT`` call that implements it is optional in the protocol
        and some embedded servers (network appliances, busybox) answer with
        ``Operation unsupported``; that must degrade to a reported
        ``preserved: false`` instead of failing an otherwise good copy.
        """
        assert self.sftp is not None
        try:
            self.sftp.utime(remote_path, times)
        except (OSError, NotImplementedError):
            return False
        return True

    def listdir_attr(self, remote_dir: str) -> list[Any]:
        """``listdir_attr`` with the paramiko-None guard in one place."""
        assert self.sftp is not None
        return self.sftp.listdir_attr(remote_dir)

    def remove(self, remote_path: str) -> None:
        assert self.sftp is not None
        self.sftp.remove(remote_path)

    def rmdir(self, remote_path: str) -> None:
        assert self.sftp is not None
        self.sftp.rmdir(remote_path)

    # ------------------------------------------------------------------
    # Hot-path I/O: chunked put / get
    # ------------------------------------------------------------------

    def put_chunked(
        self,
        local_path: str,
        remote_path: str,
        *,
        offset: int = 0,
        block_size: int = DEFAULT_BLOCK_SIZE,
        bwlimit_kbs: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Upload ``local_path`` to ``remote_path`` chunk-by-chunk.

        * ``offset`` — number of bytes already present at the destination
          to skip past (resume continuation). ``0`` means fresh upload.
        * ``block_size`` — bytes per ``write`` call.
        * ``bwlimit_kbs`` — optional KB/s cap (``KB`` is 1024 B, matching
          rsync's convention). Throttle is implemented in this chunk loop
          via ``time.sleep``; sha256 verify reads in callers are
          deliberately not throttled.
        * ``progress(done, total)`` — invoked after each chunk. ``done``
          counts from 0 (not from offset) so callers can compute % cleanly.

        Returns total bytes written this call (= ``total - offset``).
        """
        assert self.sftp is not None
        total = os.path.getsize(local_path)

        mode = "r+b" if offset > 0 else "wb"
        target_bps = bwlimit_kbs * 1024 if bwlimit_kbs else None
        start = time.monotonic()
        bytes_written = 0

        if progress is not None:
            progress(offset, total)

        with open(local_path, "rb") as local_fp:
            if offset:
                local_fp.seek(offset)

            remote_fp = self.sftp.open(remote_path, mode)
            try:
                # paramiko buffers internally; explicit set_pipelined keeps
                # large transfers fast over high-latency links.
                remote_fp.set_pipelined(True)
                if offset:
                    remote_fp.seek(offset)

                while True:
                    chunk = local_fp.read(block_size)
                    if not chunk:
                        break
                    remote_fp.write(chunk)
                    bytes_written += len(chunk)
                    done = offset + bytes_written

                    if target_bps:
                        expected_elapsed = bytes_written / target_bps
                        actual_elapsed = time.monotonic() - start
                        if expected_elapsed > actual_elapsed:
                            time.sleep(expected_elapsed - actual_elapsed)

                    if progress is not None:
                        progress(done, total)
            finally:
                remote_fp.close()

        return bytes_written

    def get_chunked(
        self,
        remote_path: str,
        local_path: str,
        *,
        offset: int = 0,
        block_size: int = DEFAULT_BLOCK_SIZE,
        bwlimit_kbs: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Mirror of ``put_chunked`` consumed by ``ssh_download``.

        Direction is reversed: read from ``remote_path`` chunk-by-chunk,
        append (or overwrite from start) into ``local_path``. ``offset``
        is the byte count already present locally to resume past.
        """
        assert self.sftp is not None
        remote_attr = self.sftp.stat(remote_path)
        total = int(remote_attr.st_size or 0)

        local_mode = "r+b" if offset > 0 else "wb"
        target_bps = bwlimit_kbs * 1024 if bwlimit_kbs else None
        start = time.monotonic()
        bytes_written = 0

        if progress is not None:
            progress(offset, total)

        remote_fp = self.sftp.open(remote_path, "rb")
        try:
            remote_fp.set_pipelined(True)
            if offset:
                remote_fp.seek(offset)

            with open(local_path, local_mode) as local_fp:
                if offset:
                    local_fp.seek(offset)

                while True:
                    chunk = remote_fp.read(block_size)
                    if not chunk:
                        break
                    local_fp.write(chunk)
                    bytes_written += len(chunk)
                    done = offset + bytes_written

                    if target_bps:
                        expected_elapsed = bytes_written / target_bps
                        actual_elapsed = time.monotonic() - start
                        if expected_elapsed > actual_elapsed:
                            time.sleep(expected_elapsed - actual_elapsed)

                    if progress is not None:
                        progress(done, total)
        finally:
            remote_fp.close()

        return bytes_written


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_pct_callback(
    filename: str,
    total: int,
    no_progress: bool,
    verb: str = "Uploading",
) -> Callable[[int, int], None] | None:
    """Return a 5%-bucket progress callback printing to stderr.

    Returns ``None`` when ``no_progress`` is true or ``total`` is zero
    (so ``put_chunked`` skips the callback path entirely instead of
    diving into per-chunk noop calls).

    The printed line shape matches the surface docstring in
    ``ssh_upload.py``::

        Uploading <filename>: 25% (262144/1048576 bytes, 850.2 KB/s)

    The leading verb is parametrized so the sibling ``ssh_download``
    task can pass ``verb="Downloading"`` without forking the helper.
    """
    if no_progress or total <= 0:
        return None

    last_bucket = [-1]
    start = time.monotonic()

    def cb(done: int, total_arg: int) -> None:
        if total_arg <= 0:
            return
        pct = int(done * 100 / total_arg)
        bucket = pct // 5
        if bucket > last_bucket[0]:
            last_bucket[0] = bucket
            elapsed = time.monotonic() - start
            kbs = done / 1024 / max(elapsed, 0.001)
            print(
                f"{verb} {filename}: {pct}% ({done}/{total_arg} bytes, {kbs:.1f} KB/s)",
                file=sys.stderr,
            )

    return cb


def sha256_streaming_local(path: str) -> str:
    """Streaming SHA-256 hex digest of a local file."""
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(SHA256_READ_BLOCK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_streaming_remote(session: SftpSession, remote_path: str) -> str:
    """Streaming SHA-256 hex digest of a remote file via SFTP read.

    Not throttled by ``--bwlimit`` — ADR-003 D4 deliberately scopes the
    user's bandwidth budget to the transfer chunk loop only, since
    extending the verify window for pacing reasons is user-hostile.
    """
    assert session.sftp is not None
    h = hashlib.sha256()
    remote_fp = session.sftp.open(remote_path, "rb")
    try:
        remote_fp.set_pipelined(True)
        while True:
            chunk = remote_fp.read(SHA256_READ_BLOCK)
            if not chunk:
                break
            h.update(chunk)
    finally:
        remote_fp.close()
    return h.hexdigest()


# ---------------------------------------------------------------------------
# High-level transfer functions — upload_file / upload_tree / download_file /
# download_tree. All four are implemented (resume, sha256, bwlimit, progress).
# ---------------------------------------------------------------------------


def upload_file(
    session: SftpSession,
    local: str,
    remote: str,
    *,
    resume: bool = False,
    chmod: int | None = None,
    block_size: int = DEFAULT_BLOCK_SIZE,
    bwlimit_kbs: int | None = None,
    progress: bool = True,
    preserve: bool = False,
) -> TransferResult:
    """Single-file upload. See design.md §2.2 for the full state machine.

    Behaviour:

    * ``resume=False`` (fast path): transfer ``local`` from offset 0 to
      ``remote``, then (optionally) chmod, then return success.
      sha256 verify is **skipped** on the fast path (PRD D2).
    * ``resume=True``: stat the remote first; choose a continuation
      offset (0 if missing, ``remote_size`` if smaller than local,
      ``local_size`` if equal, refuse if larger), transfer the
      remaining bytes, then **always** sha256-verify both ends. If the
      hashes mismatch, return exit_code=9 (transfer_verify_failed).
    * ``preserve=True`` (DV-19): after the copy, apply the local file's
      mtime and permission bits to the remote copy, exactly like ``cp -p``.
      ``TransferResult.preserved`` says whether the server accepted it; a
      server without SETSTAT support yields ``preserved=False`` and a
      ``preserve_note`` in ``stderr`` rather than a failed transfer.

    Defensive validation: this function is also called directly by tests
    and by ``upload_tree``, so it returns a failure ``TransferResult``
    instead of raising when ``local`` is missing or is a directory. The
    CLI shell (``ssh_upload.py``) repeats these checks before opening a
    session for the cheaper UX of failing without a network round-trip.
    """
    # ------------------------------------------------------------------
    # 1) Validate local path
    # ------------------------------------------------------------------
    local_path = Path(local)
    if not local_path.exists():
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=f"local path not found: {local}",
        )
    if local_path.is_dir():
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=f"'{local}' is a directory; pass --recursive",
        )

    local_size = os.path.getsize(local)
    cb = make_pct_callback(
        filename=os.path.basename(local),
        total=local_size,
        no_progress=not progress,
    )

    start = time.monotonic()

    # ------------------------------------------------------------------
    # 2) Fast path (no resume): always transfer from offset 0.
    # ------------------------------------------------------------------
    if not resume:
        session.put_chunked(
            local,
            remote,
            offset=0,
            block_size=block_size,
            bwlimit_kbs=bwlimit_kbs,
            progress=cb,
        )
        if chmod is not None:
            session.chmod(remote, chmod)
        preserved = None
        stderr = ""
        if preserve:
            preserved, note = _apply_preserve(session, local_path, remote)
            stderr = note
        return TransferResult(
            success=True,
            exit_code=0,
            bytes_transferred=local_size,
            elapsed_s=round(time.monotonic() - start, 3),
            sha256_verified=False,
            sha256_local=None,
            sha256_remote=None,
            bytes_resumed_from=None,
            failed_files=None,
            failure_class=None,
            stderr=stderr,
            preserved=preserved,
        )

    # ------------------------------------------------------------------
    # 3) Resume path: classify remote state, choose offset, transfer,
    #    verify sha256 on both ends.
    # ------------------------------------------------------------------
    remote_attr = session.stat(remote)
    remote_size = int(remote_attr.st_size or 0) if remote_attr is not None else 0
    resume_offset = 0  # bytes already on the remote at function entry

    if remote_attr is None:
        # Missing remote → behave like fast-path transfer but still verify.
        resume_offset = 0
        session.put_chunked(
            local,
            remote,
            offset=0,
            block_size=block_size,
            bwlimit_kbs=bwlimit_kbs,
            progress=cb,
        )
    elif remote_size == local_size:
        # Already complete in size — skip transfer, only verify.
        resume_offset = local_size
        # No put_chunked call here.
    elif remote_size < local_size:
        # Partial — continue from remote_size.
        resume_offset = remote_size
        session.put_chunked(
            local,
            remote,
            offset=remote_size,
            block_size=block_size,
            bwlimit_kbs=bwlimit_kbs,
            progress=cb,
        )
    else:
        # remote_size > local_size → refuse to truncate.
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=(
                f"remote ({remote_size}B) larger than local ({local_size}B); "
                "refuse to truncate"
            ),
        )

    # sha256 verify (resume path always verifies, even when nothing was sent).
    sha_local = sha256_streaming_local(local)
    sha_remote = sha256_streaming_remote(session, remote)

    if sha_local != sha_remote:
        return TransferResult(
            success=False,
            exit_code=9,
            bytes_transferred=local_size - resume_offset,
            elapsed_s=round(time.monotonic() - start, 3),
            sha256_verified=False,
            sha256_local=sha_local,
            sha256_remote=sha_remote,
            bytes_resumed_from=resume_offset,
            failed_files=None,
            failure_class="transfer_verify_failed",
            stderr=(
                f"sha256 verification failed: local={sha_local} remote={sha_remote}"
            ),
        )

    if chmod is not None:
        session.chmod(remote, chmod)

    preserved = None
    stderr = ""
    if preserve:
        preserved, note = _apply_preserve(session, local_path, remote)
        stderr = note

    return TransferResult(
        success=True,
        exit_code=0,
        bytes_transferred=local_size - resume_offset,
        elapsed_s=round(time.monotonic() - start, 3),
        sha256_verified=True,
        sha256_local=sha_local,
        sha256_remote=sha_remote,
        bytes_resumed_from=resume_offset,
        failed_files=None,
        failure_class=None,
        stderr=stderr,
        preserved=preserved,
    )


def _apply_preserve(
    session: SftpSession,
    local_path: Path,
    remote: str,
) -> tuple[bool, str]:
    """Copy the local mtime and permission bits onto ``remote``.

    Returns ``(preserved, note)``. ``preserved`` is False when the server
    rejected the metadata update, and ``note`` then carries a human sentence
    that the caller folds into ``stderr``/``data.preserve_note`` — a copy that
    succeeded must not be reported as failed just because SETSTAT is
    unsupported (a lot of embedded SFTP servers do not implement it).
    """
    try:
        info = local_path.stat()
    except OSError as exc:
        return False, f"ssh-core: --preserve could not stat the local file: {exc}"
    ok = session.utime(remote, (info.st_atime, info.st_mtime))
    if not ok:
        return False, (
            "ssh-core: --preserve: the remote SFTP server rejected the "
            "timestamp update (SETSTAT unsupported); the file content is "
            "intact but its mtime was not preserved."
        )
    # chmod is best-effort too: the remote umask/ACL may forbid it, and
    # --chmod already has its own explicit path for callers who care.
    try:
        session.chmod(remote, stat_mod.S_IMODE(info.st_mode))
    except (OSError, NotImplementedError):
        return True, (
            "ssh-core: --preserve: mtime preserved, but permission bits were "
            "refused by the remote server."
        )
    return True, ""


def upload_tree(
    session: SftpSession,
    local_dir: str,
    remote_dir: str,
    *,
    resume: bool = False,
    chmod: int | None = None,
    block_size: int = DEFAULT_BLOCK_SIZE,
    bwlimit_kbs: int | None = None,
    progress: bool = True,
    preserve: bool = False,
    delete: bool = False,
) -> TransferResult:
    """Recursive directory upload. See design.md §2.3 for the contract.

    Walk strategy (PRD D3 — scp-like):
        * follow symlinks (``os.walk(local_dir, followlinks=True)``);
        * do not preserve link structure (resolved file is uploaded);
        * ``preserve=True`` applies each file's mtime/mode (DV-19);
        * apply ``chmod`` to files only — directories are created with
          mode 0o755 and rely on the remote umask, per PRD D4(b).

    ``delete=True`` (DV-19) makes this a *mirror*: after the copy, anything
    under ``remote_dir`` that does not exist under ``local_dir`` is removed.
    That is destructive by nature, so the CLI layer demands ``--yes``; the
    removed paths are reported in ``TransferResult.deleted_files`` and no
    file is removed when the copy itself recorded a failure.

    Error policy (PRD D3 — continue-on-error):
        Per-file failures are caught (broad ``Exception`` because
        ``upload_file`` is defensive and returns a failing
        ``TransferResult`` rather than raising, but the underlying
        paramiko API can still raise for ``stat`` / ``open`` /
        ``mkdir_p``). On failure we append ``{"path", "error"}`` to
        ``failed_files`` and continue with the next file.

    File-count strategy: we materialize ``os.walk`` into a list of
    ``(local_path, remote_path)`` pairs **once** before transferring.
    This pays a single ``os.walk`` traversal for the progress separator
    line's ``i/N`` count, which is cheap relative to the upload itself
    and avoids two-pass logic.

    Path joining: local paths use ``os.path.join`` (native separator);
    remote paths use ``posixpath.join`` always (POSIX remote, even when
    the script runs on Windows — PRD R10).
    """
    # ------------------------------------------------------------------
    # 1) Validate local_dir (defensive — CLI shell pre-flighted but
    #    direct callers like tests / upload_tree itself may bypass).
    # ------------------------------------------------------------------
    local_dir_path = Path(local_dir)
    if not local_dir_path.exists() or not local_dir_path.is_dir():
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=f"local path '{local_dir}' is not a directory",
        )

    # ------------------------------------------------------------------
    # 2) Materialize the walk once: collect (local_file, remote_file)
    #    pairs and the set of remote directories that need mkdir_p.
    # ------------------------------------------------------------------
    file_pairs: list[tuple[str, str]] = []
    dirs_to_create: list[str] = [remote_dir]  # always ensure root exists
    for current_root, _subdirs, filenames in os.walk(local_dir, followlinks=True):
        rel = os.path.relpath(current_root, local_dir)
        # Translate native separators to POSIX for the remote leg.
        rel_posix = "" if rel in (".", "") else rel.replace(os.sep, "/")
        remote_subdir = (
            remote_dir if not rel_posix else posixpath.join(remote_dir, rel_posix)
        )
        if remote_subdir not in dirs_to_create:
            dirs_to_create.append(remote_subdir)
        for fname in filenames:
            local_file = os.path.join(current_root, fname)
            remote_file = posixpath.join(remote_subdir, fname)
            file_pairs.append((local_file, remote_file))

    total_files = len(file_pairs)
    start = time.monotonic()

    # ------------------------------------------------------------------
    # 3) Create remote directories (continue-on-error: a failed mkdir
    #    means the child files in that dir will also fail and be
    #    recorded individually).
    # ------------------------------------------------------------------
    failed_files: list[dict[str, Any]] = []
    for rdir in dirs_to_create:
        try:
            session.mkdir_p(rdir, mode=0o755)
        except Exception as e:  # noqa: BLE001 — continue-on-error policy
            # mkdir failures are rare; we still try the files (their
            # individual open will fail and be captured below).
            failed_files.append({"path": rdir, "error": f"mkdir failed: {e}"})

    # ------------------------------------------------------------------
    # 4) Transfer each file.
    # ------------------------------------------------------------------
    total_bytes = 0
    preserved_any: bool | None = None
    preserve_notes: list[str] = []
    for i, (local_file, remote_file) in enumerate(file_pairs, start=1):
        if progress:
            print(
                f"--- file {i}/{total_files}: {local_file} ---",
                file=sys.stderr,
            )
        try:
            tr = upload_file(
                session,
                local_file,
                remote_file,
                resume=resume,
                chmod=chmod,
                block_size=block_size,
                bwlimit_kbs=bwlimit_kbs,
                progress=progress,
                preserve=preserve,
            )
            if tr.success:
                total_bytes += tr.bytes_transferred
                if tr.preserved is not None:
                    preserved_any = bool(preserved_any) or tr.preserved
                if tr.preserved is False and tr.stderr:
                    preserve_notes.append(tr.stderr.strip())
            else:
                failed_files.append(
                    {"path": local_file, "error": tr.stderr or "upload failed"}
                )
        except Exception as e:  # noqa: BLE001 — continue-on-error
            failed_files.append({"path": local_file, "error": str(e)})

    elapsed = round(time.monotonic() - start, 3)

    # ------------------------------------------------------------------
    # 5) Mirror mode: remove remote entries the source no longer has.
    #    Never runs after a failed copy — deleting into a half-copied tree
    #    is how a --delete run destroys data it was supposed to mirror.
    # ------------------------------------------------------------------
    deleted: list[str] = []
    if delete:
        if failed_files:
            failed_files.append({
                "path": remote_dir,
                "error": (
                    "--delete skipped: the copy recorded failures, so pruning "
                    "the destination would leave it inconsistent"
                ),
            })
        else:
            deleted, prune_errors = _prune_remote(
                session, local_dir, remote_dir, progress=progress
            )
            failed_files.extend(prune_errors)

    # ------------------------------------------------------------------
    # 6) Aggregate result.
    # ------------------------------------------------------------------
    if not failed_files:
        return TransferResult(
            success=True,
            exit_code=0,
            bytes_transferred=total_bytes,
            elapsed_s=elapsed,
            sha256_verified=False,
            failed_files=None,
            failure_class=None,
            stderr="\n".join(preserve_notes),
            preserved=preserved_any,
            deleted_files=deleted or None,
        )

    return TransferResult(
        success=False,
        exit_code=1,
        bytes_transferred=total_bytes,
        elapsed_s=elapsed,
        sha256_verified=False,
        failed_files=failed_files,
        failure_class="remote_error",
        stderr=f"{len(failed_files)} of {total_files} files failed to upload",
        preserved=preserved_any,
        deleted_files=deleted or None,
    )


def _prune_remote(
    session: SftpSession,
    local_dir: str,
    remote_dir: str,
    *,
    progress: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Remove remote files/dirs under ``remote_dir`` absent from ``local_dir``.

    Walks the destination, keeps a set of the relative paths that exist on
    the source (computed with the same ``os.walk(followlinks=True)`` the
    upload used, so the two agree), and deletes the difference bottom-up so
    a directory can only be removed once it is empty.

    Returns ``(deleted_paths, errors)``; an unreadable directory is an error,
    not a silent skip, because a mirror that quietly misses half the tree is
    worse than one that stops.
    """
    keep: set[str] = set()
    for current_root, _subdirs, filenames in os.walk(local_dir, followlinks=True):
        rel = os.path.relpath(current_root, local_dir)
        rel_posix = "" if rel in (".", "") else rel.replace(os.sep, "/")
        keep.add(rel_posix)
        for fname in filenames:
            keep.add(posixpath.join(rel_posix, fname) if rel_posix else fname)

    remote_entries: list[tuple[str, bool]] = []  # (relative path, is_dir)

    def _walk(rel_dir: str) -> None:
        remote_here = (
            remote_dir if not rel_dir else posixpath.join(remote_dir, rel_dir)
        )
        for entry in session.listdir_attr(remote_here):
            rel_child = posixpath.join(rel_dir, entry.filename) if rel_dir else entry.filename
            is_dir = stat_mod.S_ISDIR(entry.st_mode or 0)
            if is_dir:
                _walk(rel_child)
            remote_entries.append((rel_child, is_dir))

    try:
        _walk("")
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return [], [
            {"path": remote_dir, "error": f"--delete could not list: {exc}"}
        ]

    deleted: list[str] = []
    errors: list[dict[str, Any]] = []
    # Deeper paths first: a directory can only go once it is empty.
    for rel_child, is_dir in sorted(
        remote_entries, key=lambda item: item[0].count("/"), reverse=True
    ):
        if rel_child in keep:
            continue
        full = posixpath.join(remote_dir, rel_child)
        try:
            if is_dir:
                session.rmdir(full)
            else:
                session.remove(full)
        except Exception as exc:  # noqa: BLE001 — report and continue
            errors.append({"path": full, "error": f"--delete failed: {exc}"})
            continue
        deleted.append(full)
        if progress:
            print(f"--- deleted (mirror): {full} ---", file=sys.stderr)
    return deleted, errors


def download_file(
    session: SftpSession,
    remote: str,
    local: str,
    *,
    resume: bool = False,
    block_size: int = DEFAULT_BLOCK_SIZE,
    bwlimit_kbs: int | None = None,
    progress: bool = True,
    preserve: bool = False,
) -> TransferResult:
    """Single-file download — mirror of ``upload_file``.

    Behaviour:

    * ``resume=False`` (fast path): transfer ``remote`` from offset 0 to
      ``local`` (overwriting any existing file at ``local``), then return
      success. sha256 verify is **skipped** on the fast path, mirroring
      ``upload_file`` (PRD D2).
    * ``resume=True``: stat the local file first; choose a continuation
      offset (0 if missing, ``local_size`` if smaller than remote,
      ``remote_size`` if equal, refuse if larger than remote), transfer
      the remaining bytes, then **always** sha256-verify both ends. If
      the hashes mismatch, return exit_code=9 (transfer_verify_failed).
    * ``preserve=True`` (DV-19): apply the remote mtime and mode to the
      local copy after it lands, so a download is byte- *and*
      metadata-identical to the source.

    Defensive validation: this function returns a failure
    ``TransferResult`` instead of raising when ``remote`` is missing or
    is a directory. The CLI shell (``ssh_download.py``) doesn't pre-flight
    these (they require a network round-trip), so the guards live here.
    """
    # ------------------------------------------------------------------
    # 1) Validate remote path
    # ------------------------------------------------------------------
    remote_attr = session.stat(remote)
    if remote_attr is None:
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=f"remote path not found: {remote}",
        )
    if stat_mod.S_ISDIR(remote_attr.st_mode or 0):
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=f"remote '{remote}' is a directory; pass --recursive",
        )

    remote_size = int(remote_attr.st_size or 0)

    # Ensure parent directory exists locally.
    local_path = Path(local)
    if local_path.parent and not local_path.parent.exists():
        local_path.parent.mkdir(parents=True, exist_ok=True)

    cb = make_pct_callback(
        filename=posixpath.basename(remote),
        total=remote_size,
        no_progress=not progress,
        verb="Downloading",
    )

    start = time.monotonic()

    # ------------------------------------------------------------------
    # 2) Fast path (no resume): always transfer from offset 0.
    # ------------------------------------------------------------------
    if not resume:
        session.get_chunked(
            remote,
            local,
            offset=0,
            block_size=block_size,
            bwlimit_kbs=bwlimit_kbs,
            progress=cb,
        )
        preserved = None
        stderr = ""
        if preserve:
            preserved, stderr = _apply_local_preserve(local_path, remote_attr)
        return TransferResult(
            success=True,
            exit_code=0,
            bytes_transferred=remote_size,
            elapsed_s=round(time.monotonic() - start, 3),
            sha256_verified=False,
            sha256_local=None,
            sha256_remote=None,
            bytes_resumed_from=None,
            failed_files=None,
            failure_class=None,
            stderr=stderr,
            preserved=preserved,
        )

    # ------------------------------------------------------------------
    # 3) Resume path: classify local state, choose offset, transfer,
    #    verify sha256 on both ends.
    # ------------------------------------------------------------------
    local_size = local_path.stat().st_size if local_path.exists() else 0
    resume_offset = 0  # bytes already present locally at entry

    if not local_path.exists():
        resume_offset = 0
        session.get_chunked(
            remote,
            local,
            offset=0,
            block_size=block_size,
            bwlimit_kbs=bwlimit_kbs,
            progress=cb,
        )
    elif local_size == remote_size:
        # Already complete in size — skip transfer, only verify.
        resume_offset = local_size
        # No get_chunked call here.
    elif local_size < remote_size:
        # Partial — continue from local_size.
        resume_offset = local_size
        session.get_chunked(
            remote,
            local,
            offset=local_size,
            block_size=block_size,
            bwlimit_kbs=bwlimit_kbs,
            progress=cb,
        )
    else:
        # local_size > remote_size → refuse to truncate.
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=(
                f"local ({local_size}B) larger than remote ({remote_size}B); "
                "refuse to truncate"
            ),
        )

    # sha256 verify (resume path always verifies, even when nothing was sent).
    sha_local = sha256_streaming_local(local)
    sha_remote = sha256_streaming_remote(session, remote)

    if sha_local != sha_remote:
        return TransferResult(
            success=False,
            exit_code=9,
            bytes_transferred=remote_size - resume_offset,
            elapsed_s=round(time.monotonic() - start, 3),
            sha256_verified=False,
            sha256_local=sha_local,
            sha256_remote=sha_remote,
            bytes_resumed_from=resume_offset,
            failed_files=None,
            failure_class="transfer_verify_failed",
            stderr=(
                f"sha256 verification failed: local={sha_local} remote={sha_remote}"
            ),
        )

    preserved = None
    stderr = ""
    if preserve:
        preserved, stderr = _apply_local_preserve(local_path, remote_attr)

    return TransferResult(
        success=True,
        exit_code=0,
        bytes_transferred=remote_size - resume_offset,
        elapsed_s=round(time.monotonic() - start, 3),
        sha256_verified=True,
        sha256_local=sha_local,
        sha256_remote=sha_remote,
        bytes_resumed_from=resume_offset,
        failed_files=None,
        failure_class=None,
        stderr=stderr,
        preserved=preserved,
    )


def _apply_local_preserve(
    local_path: Path,
    remote_attr: Any,
) -> tuple[bool, str]:
    """Apply the remote file's mtime/mode to the freshly downloaded copy."""
    mtime = getattr(remote_attr, "st_mtime", None)
    mode = getattr(remote_attr, "st_mode", None)
    if mtime is None:
        return False, (
            "ssh-core: --preserve: the SFTP server did not report an mtime "
            "for the source; the download itself succeeded."
        )
    try:
        os.utime(local_path, (mtime, mtime))
    except OSError as exc:
        return False, f"ssh-core: --preserve failed to set the local mtime: {exc}"
    if mode is None:
        return True, ""
    try:
        os.chmod(local_path, stat_mod.S_IMODE(mode))
    except OSError:
        return True, (
            "ssh-core: --preserve: mtime applied, but the local permission "
            "bits could not be set."
        )
    return True, ""


def download_tree(
    session: SftpSession,
    remote_dir: str,
    local_dir: str,
    *,
    resume: bool = False,
    block_size: int = DEFAULT_BLOCK_SIZE,
    bwlimit_kbs: int | None = None,
    progress: bool = True,
    preserve: bool = False,
    delete: bool = False,
) -> TransferResult:
    """Recursive directory download — mirror of ``upload_tree``.

    Walk strategy: ``session.sftp.listdir_attr`` recursively materialized
    into a list of ``(remote_path, local_path)`` pairs **once** before
    transferring — same single-pass-materialize strategy as
    ``upload_tree`` so the per-file progress separator can show ``i/N``.

    Error policy: continue-on-error. Per-file failures are caught and
    accumulated into ``failed_files``; the call still returns and the
    caller can decide whether non-empty ``failed_files`` is a problem
    (it always maps to exit_code=1 + failure_class="remote_error").

    Path joining: remote paths always use ``posixpath.join``; local paths
    use ``os.path.join`` (native separator). The CLI surface guarantees
    the remote is POSIX even when the script runs on Windows.
    """
    assert session.sftp is not None

    # ------------------------------------------------------------------
    # 1) Validate remote_dir
    # ------------------------------------------------------------------
    rs = session.stat(remote_dir)
    if rs is None:
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=f"remote path not found: {remote_dir}",
        )
    if not stat_mod.S_ISDIR(rs.st_mode or 0):
        return TransferResult(
            success=False,
            exit_code=1,
            failure_class="remote_error",
            stderr=f"remote '{remote_dir}' is a file; omit --recursive",
        )

    # Ensure local root exists.
    Path(local_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 2) Walk remote and materialize (remote_file, local_target) pairs.
    # ------------------------------------------------------------------
    failed_files: list[dict[str, Any]] = []
    file_pairs: list[tuple[str, str]] = []

    def _walk(rd: str, local_rd: Path) -> None:
        try:
            entries = session.sftp.listdir_attr(rd)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001 — continue-on-error policy
            failed_files.append({"path": rd, "error": f"listdir failed: {e}"})
            return
        for entry in entries:
            full_remote = posixpath.join(rd, entry.filename)
            full_local = local_rd / entry.filename
            if stat_mod.S_ISDIR(entry.st_mode or 0):
                try:
                    full_local.mkdir(parents=True, exist_ok=True)
                except Exception as e:  # noqa: BLE001
                    failed_files.append(
                        {"path": full_remote, "error": f"mkdir failed: {e}"}
                    )
                    continue
                _walk(full_remote, full_local)
            else:
                file_pairs.append((full_remote, str(full_local)))

    _walk(remote_dir, Path(local_dir))

    total_files = len(file_pairs)
    start = time.monotonic()
    total_bytes = 0
    preserved_any: bool | None = None
    preserve_notes: list[str] = []

    # ------------------------------------------------------------------
    # 3) Transfer each file.
    # ------------------------------------------------------------------
    for i, (remote_file, local_target) in enumerate(file_pairs, start=1):
        if progress:
            print(
                f"--- file {i}/{total_files}: {remote_file} ---",
                file=sys.stderr,
            )
        try:
            tr = download_file(
                session,
                remote_file,
                local_target,
                resume=resume,
                block_size=block_size,
                bwlimit_kbs=bwlimit_kbs,
                progress=progress,
                preserve=preserve,
            )
            if tr.success:
                total_bytes += tr.bytes_transferred
                if tr.preserved is not None:
                    preserved_any = bool(preserved_any) or tr.preserved
                if tr.preserved is False and tr.stderr:
                    preserve_notes.append(tr.stderr.strip())
            else:
                failed_files.append(
                    {"path": remote_file, "error": tr.stderr or "download failed"}
                )
        except Exception as e:  # noqa: BLE001 — continue-on-error
            failed_files.append({"path": remote_file, "error": str(e)})

    elapsed = round(time.monotonic() - start, 3)

    # ------------------------------------------------------------------
    # 3b) Mirror mode: remove local entries the source no longer has.
    # ------------------------------------------------------------------
    deleted: list[str] = []
    if delete:
        if failed_files:
            failed_files.append({
                "path": local_dir,
                "error": (
                    "--delete skipped: the download recorded failures, so "
                    "pruning the destination would leave it inconsistent"
                ),
            })
        else:
            deleted, prune_errors = _prune_local_tree(
                local_dir, remote_dir, session, progress=progress
            )
            failed_files.extend(prune_errors)

    # ------------------------------------------------------------------
    # 4) Aggregate result.
    # ------------------------------------------------------------------
    if not failed_files:
        return TransferResult(
            success=True,
            exit_code=0,
            bytes_transferred=total_bytes,
            elapsed_s=elapsed,
            sha256_verified=False,
            failed_files=None,
            failure_class=None,
            stderr="\n".join(preserve_notes),
            preserved=preserved_any,
            deleted_files=deleted or None,
        )

    return TransferResult(
        success=False,
        exit_code=1,
        bytes_transferred=total_bytes,
        elapsed_s=elapsed,
        sha256_verified=False,
        failed_files=failed_files,
        failure_class="remote_error",
        stderr=f"{len(failed_files)} of {total_files} files failed to download",
        preserved=preserved_any,
        deleted_files=deleted or None,
    )


def _prune_local_tree(
    local_dir: str,
    remote_dir: str,
    session: SftpSession,
    *,
    progress: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Remove local files/dirs under ``local_dir`` that the remote tree lacks.

    The keep-set is recomputed by walking the *remote* tree (the mirror
    source), so the decision is made from the same data the download used.
    Deletion is bottom-up so a directory is only removed once empty.
    """
    keep: set[str] = set()

    def _collect(rel_dir: str) -> None:
        remote_here = (
            remote_dir if not rel_dir else posixpath.join(remote_dir, rel_dir)
        )
        try:
            entries = session.listdir_attr(remote_here)
        except Exception:  # noqa: BLE001 — the walk above already reported it
            return
        for entry in entries:
            rel_child = (
                posixpath.join(rel_dir, entry.filename) if rel_dir else entry.filename
            )
            keep.add(rel_child)
            if stat_mod.S_ISDIR(entry.st_mode or 0):
                _collect(rel_child)

    _collect("")

    local_root = Path(local_dir)
    entries: list[tuple[Path, str, bool]] = []  # (absolute, rel_posix, is_dir)
    try:
        for current_root, subdirs, filenames in os.walk(local_root, topdown=True):
            rel = os.path.relpath(current_root, local_root)
            rel_posix = "" if rel in (".", "") else rel.replace(os.sep, "/")
            for name in subdirs:
                child_rel = posixpath.join(rel_posix, name) if rel_posix else name
                entries.append((Path(current_root) / name, child_rel, True))
            for name in filenames:
                child_rel = posixpath.join(rel_posix, name) if rel_posix else name
                entries.append((Path(current_root) / name, child_rel, False))
    except OSError as exc:
        return [], [{"path": local_dir, "error": f"--delete could not walk: {exc}"}]

    deleted: list[str] = []
    errors: list[dict[str, Any]] = []
    # Deeper first: files inside a doomed directory must go before it does.
    for path, rel_posix, is_dir in sorted(
        entries, key=lambda item: item[1].count("/"), reverse=True
    ):
        if rel_posix in keep:
            continue
        try:
            if is_dir:
                path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            errors.append({"path": str(path), "error": f"--delete failed: {exc}"})
            continue
        deleted.append(str(path))
        if progress:
            print(f"--- deleted (mirror): {path} ---", file=sys.stderr)
    return deleted, errors


__all__ = [
    "DEFAULT_BLOCK_SIZE",
    "SftpSession",
    "TransferResult",
    "download_file",
    "download_tree",
    "make_pct_callback",
    "sha256_streaming_local",
    "sha256_streaming_remote",
    "upload_file",
    "upload_tree",
]
