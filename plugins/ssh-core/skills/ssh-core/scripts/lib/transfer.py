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
can keep one envelope shape regardless of transport.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

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
    files: int = 1
    detail: str = ""
    #: ``None`` when `--preserve` was not requested, ``True`` when the transport
    #: honoured it (FA-05: scp now passes `-p` instead of pretending).
    preserved: bool | None = None

    def as_data(self) -> dict:
        return {
            "transport": self.transport,
            "bytes_transferred": self.bytes_transferred,
            "elapsed_s": round(self.elapsed_s, 3),
            "sha256_verified": self.sha256_verified,
            "failure_class": self.failure_class,
            "files_transferred": self.files,
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
    remote_rsync = "rsync -a"
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
    return TransferOutcome(
        success=True, transport="rsync", elapsed_s=elapsed, argv=argv,
        detail=(
            "rsync ran on the source host (incremental copy); both endpoints "
            "are remote, so it cannot run from this machine"
        ),
    )


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
    trimmed = src_path.rstrip("/")
    parent, _, leaf = trimmed.rpartition("/")
    src_dir = parent or "/"
    leaf = leaf or trimmed
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
    try:
        src_proc = subprocess.Popen(
            producer, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        dst_proc = subprocess.Popen(
            consumer, stdin=src_proc.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
        if src_proc.stdout is not None:
            src_proc.stdout.close()
        total = 0
        assert dst_proc.stdout is not None
        chunk_size = 1 << 20
        limit_bps = bwlimit_kbs * 1024 if bwlimit_kbs else None
        while True:
            block = dst_proc.stdout.read(chunk_size)
            if not block:
                break
            total += len(block)
            if limit_bps:
                expected = total / limit_bps
                actual = time.monotonic() - started
                if expected > actual:
                    time.sleep(expected - actual)
            if time.monotonic() - started > timeout:
                src_proc.kill()
                dst_proc.kill()
                return TransferOutcome(
                    success=False, transport="direct", failure_class="timeout",
                    stderr=f"direct tar stream exceeded {timeout}s",
                    argv=producer + consumer, elapsed_s=time.monotonic() - started,
                )
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
        stderr = b""
        for proc in (src_proc, dst_proc):
            if proc.stderr is not None:
                with contextlib.suppress(ValueError):
                    stderr += proc.stderr.read() or b""
        text = stderr.decode("utf-8", "replace").strip()
        fclass = _classify(text)
        # "mv: cannot stat" means the copy itself worked but the rename did
        # not; that is still a failed transfer, just with a clearer cause.
        if "cannot stat" in text:
            fclass = "dst_path_invalid"
        return TransferOutcome(
            success=False, transport="direct", failure_class=fclass,
            stderr=text[:2000], elapsed_s=elapsed,
        )
    return TransferOutcome(
        success=True, transport="direct", bytes_transferred=total,
        elapsed_s=elapsed,
        detail="streamed with tar over ssh (no local staging)",
    )


def remaining_timeout(started: float, timeout: float) -> float:
    return max(1.0, timeout - (time.monotonic() - started))


__all__ = [
    "TransferOutcome",
    "have",
    "paramiko_available",
    "remote_sha256",
    "run_rsync",
    "run_scp",
    "run_tar_stream",
    "scp_argv",
    "sha256_file",
]
