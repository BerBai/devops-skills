#!/usr/bin/env python3
"""deploy_pubkey.py — install your public key on a remote host (v1.4.0).

Public surface::

    python deploy_pubkey.py <alias>
        [--key PATH]                     default: first of id_ed25519/ecdsa/rsa
        [--connect-timeout N]            default: 8
        [--timeout N]                    default: 180
        [--ssh-config PATH]
        [--accept-new-hostkey]
        [--password]                     headless: use $SSH_PASSWORD
        [--password-env VAR]
        [--promote-auth]                 set # auth: key + IdentityFile on success
        [--audit-log|--no-audit] [--redact|--no-redact]
        [--json] [--version]

How the key gets installed (SSH-10)
-----------------------------------
v1.0 shelled out to ``ssh-copy-id``. That binary is awkward everywhere:
Git-for-Windows ships it as an *extensionless shell script*, so
``shutil.which()`` never finds it and ``CreateProcess`` cannot run it — on
Windows the script was permanently ``tool_missing``.

v1.1 does the install itself over a normal SSH exec channel::

    umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh
    touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys
    if grep -qxF '<key>' ~/.ssh/authorized_keys; then echo ssh-core:key-present
    else echo '<key>' >> ~/.ssh/authorized_keys; echo ssh-core:key-added; fi

which is idempotent, needs no helper binary, and works on Windows, Linux and
macOS alike. The key is then **verified** with a key-only ssh call before
success is reported, so "installed but still not usable" is a failure with a
reason (``verify_failed``), not a false positive.

Two authentication paths:

* **key already works** — the exec channel runs over the existing key (the
  normal case: adding a *second* key, or re-running).
* **``--password``** — the exec channel is opened with ``$SSH_PASSWORD``
  through :mod:`lib.session` (pty on POSIX, ``ssh -tt`` + askpass elsewhere),
  so a fresh Windows box can bootstrap a Linux host with no ``ssh-copy-id``,
  no WSL and no pty.

Exit codes (shared table, :mod:`lib.exit_codes`): ``key_file_missing`` 141,
``tool_missing`` 140, ``auth`` 100, ``host_key`` 101, ``network`` 102,
``password_required`` 105, ``unsupported_target`` 108,
``ssh_config_unsupported`` 142, ``verify_failed`` 9, ``timeout`` 124.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    AUTH_PATTERN,
    CHILD_TEXT_KWARGS,
    HOST_KEY_PATTERN,
    NETWORK_PATTERN,
    append_audit,
    audit_cli_flags,
    audit_enabled,
    emit,
    json_result,
    msys_safe_env,
)
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AUTH,
    EXIT_HOST_KEY,
    EXIT_KEY_FILE_MISSING,
    EXIT_NETWORK,
    EXIT_NO_TTY,
    EXIT_OK,
    EXIT_PASSWORD_REQUIRED,
    EXIT_PERMISSION,
    EXIT_REMOTE_ERROR,
    EXIT_SSH_CONFIG_UNSUPPORTED,
    EXIT_TIMEOUT,
    EXIT_TOOL_MISSING,
    EXIT_UNSUPPORTED_TARGET,
    EXIT_VERIFY_FAILED,
    exit_code_for,
)
from lib.redact import redactor_for  # noqa: E402
from lib.pubkey import (  # noqa: E402
    MARK_ALREADY as _LIB_MARK_ALREADY,
    MARK_INSTALLED as _LIB_MARK_INSTALLED,
    PUBKEY_PEEK_BYTES,
    PubkeyError,
    build_install_script,
    load_pubkey,
    looks_like_pubkey_text,
    parse_install_output,
    pick_default_pubkey,
    pubkey_from_text,
    resolve_key_path,
    sh_quote,
)

# SSHCORE-012 A: probe the usual pubkeys in modern-to-legacy order.
_DEFAULT_KEY_CANDIDATES: tuple[str, ...] = (
    "~/.ssh/id_ed25519.pub",
    "~/.ssh/id_ecdsa.pub",
    "~/.ssh/id_rsa.pub",
    "~/.ssh/id_dsa.pub",
)
_DEFAULT_KEY = _DEFAULT_KEY_CANDIDATES[0]
_DEFAULT_CONNECT_TIMEOUT = 8
_DEFAULT_TIMEOUT = 180

FAIL_TOOL_MISSING = "tool_missing"
FAIL_KEY_FILE_MISSING = "key_file_missing"
FAIL_NO_TTY = "no_tty_for_password"
FAIL_AUTH = "auth"
FAIL_HOST_KEY_UNKNOWN = "host_key"
FAIL_NETWORK = "network"
FAIL_TIMEOUT = "timeout"
FAIL_UNSUPPORTED_TARGET = "unsupported_target"
FAIL_SSH_CONFIG_UNSUPPORTED = "ssh_config_unsupported"
FAIL_VERIFY_FAILED = "verify_failed"
FAIL_PASSWORD_REQUIRED = "password_required"
FAIL_PERMISSION = "permission"
FAIL_REMOTE_ERROR = "remote_error"

_EXIT_CODES: dict[str, int] = {
    FAIL_TOOL_MISSING: EXIT_TOOL_MISSING,
    FAIL_KEY_FILE_MISSING: EXIT_KEY_FILE_MISSING,
    FAIL_NO_TTY: EXIT_NO_TTY,
    FAIL_AUTH: EXIT_AUTH,
    FAIL_HOST_KEY_UNKNOWN: EXIT_HOST_KEY,
    FAIL_NETWORK: EXIT_NETWORK,
    FAIL_TIMEOUT: EXIT_TIMEOUT,
    FAIL_UNSUPPORTED_TARGET: EXIT_UNSUPPORTED_TARGET,
    FAIL_SSH_CONFIG_UNSUPPORTED: EXIT_SSH_CONFIG_UNSUPPORTED,
    FAIL_VERIFY_FAILED: EXIT_VERIFY_FAILED,
    FAIL_PASSWORD_REQUIRED: EXIT_PASSWORD_REQUIRED,
    FAIL_PERMISSION: EXIT_PERMISSION,
    FAIL_REMOTE_ERROR: EXIT_REMOTE_ERROR,
}

_HUMAN_HINTS: dict[str, str] = {
    FAIL_TOOL_MISSING: "the ssh client is not on PATH.",
    FAIL_KEY_FILE_MISSING: (
        "public key not found or invalid: {path}. Generate one with `ssh-keygen` "
        "or pass --key <path>."
    ),
    FAIL_NO_TTY: (
        "this host needs a password but none is available. Export "
        "$SSH_PASSWORD and pass --password (works on Windows too), or run from "
        "a terminal."
    ),
    FAIL_AUTH: (
        "authentication to {alias} failed. Check the password, or install the "
        "key with --password / --accept-new-hostkey first."
    ),
    FAIL_HOST_KEY_UNKNOWN: (
        "{alias}'s host key is not in known_hosts. Re-run with "
        "--accept-new-hostkey to trust it on first use."
    ),
    FAIL_NETWORK: "could not reach {alias}: {first_stderr_line}",
    FAIL_TIMEOUT: "deploy_pubkey timed out after {timeout}s while talking to {alias}",
    FAIL_UNSUPPORTED_TARGET: (
        "{alias} does not support pubkey deployment (no shell or no ~/.ssh — "
        "typical for network devices). Use ssh_session.py with password auth."
    ),
    FAIL_SSH_CONFIG_UNSUPPORTED: "this ssh client does not support -F.",
    FAIL_PASSWORD_REQUIRED: (
        "--password needs the password in the environment: export $SSH_PASSWORD "
        "(or name another variable with --password-env). Passwords are never "
        "taken from the command line."
    ),
    FAIL_VERIFY_FAILED: (
        "the key was appended on {alias} but key-only auth still fails: "
        "{first_stderr_line}. Check ~/.ssh (0700) and authorized_keys (0600) "
        "permissions, or the server's PubkeyAuthentication setting."
    ),
    FAIL_PERMISSION: "permission denied while writing ~/.ssh on {alias}: {first_stderr_line}",
    FAIL_REMOTE_ERROR: "key deployment failed: {first_stderr_line}",
}

_ALREADY_RE = re.compile(r"All keys were skipped because they already exist")
_UNSUPPORTED_TARGET_RE = re.compile(
    r"(subsystem request failed|scp: not found|sftp: not found|"
    r"does not support sftp|exec request failed|"
    r"ERROR: (?:sftp|scp) not found|sh: .*not found|command not found)",
    re.IGNORECASE,
)


#: Re-exported so older tests (and any external caller) keep importing the
#: marker names from this script. The values live in :mod:`lib.pubkey` only.
_MARKER_ADDED = _LIB_MARK_INSTALLED
_MARKER_PRESENT = _LIB_MARK_ALREADY


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="deploy_pubkey.py",
        description=(
            "Install a public key on a remote host over SSH (no ssh-copy-id "
            "needed), verify that key-only auth works, and optionally promote "
            "the host's # auth: metadata to key."
        ),
    )
    p.add_argument("alias", help="Host alias from ~/.ssh/config (or user@host)")
    p.add_argument(
        "--key", default=None,
        help="Path to the .pub file (default: first of "
             + " / ".join(_DEFAULT_KEY_CANDIDATES) + ")",
    )
    p.add_argument("--connect-timeout", type=int, default=_DEFAULT_CONNECT_TIMEOUT)
    p.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT)
    p.add_argument("--ssh-config", default=None, help="Pass -F <path> to ssh")
    p.add_argument(
        "--accept-new-hostkey", action="store_true",
        help="Opt-in: StrictHostKeyChecking=accept-new for first-touch trust",
    )
    p.add_argument(
        "--password", action="store_true",
        help=(
            "Headless bootstrap: log in with $SSH_PASSWORD, append the key, "
            "then verify key-only auth. No TTY or ssh-copy-id needed."
        ),
    )
    p.add_argument("--password-env", default="SSH_PASSWORD")
    p.add_argument(
        "--promote-auth", action="store_true",
        help=(
            "After a verified install: rewrite '# auth:' to key and add "
            "IdentityFile/IdentitiesOnly for this alias"
        ),
    )
    p.add_argument(
        "--redact", dest="redact", action="store_true", default=True,
        help="Hide host identifiers in the envelope (default: on)",
    )
    p.add_argument("--no-redact", dest="redact", action="store_false", help=argparse.SUPPRESS)
    audit_cli_flags(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--version", "-V", action="store_true")
    return p


# --- preflight --------------------------------------------------------------


def _resolve_key(path: str) -> Path:
    return resolve_key_path(path)


def _pick_default_key() -> tuple[str, str]:
    return pick_default_pubkey()


def _looks_like_openssh_pubkey(key_path: Path) -> bool:
    """Read the first bytes and hand the judgement to :mod:`lib.pubkey`.

    The *prefix table* lives in the library only; this function is a thin
    file-reading adapter, because a preflight that does not read the file cannot
    tell a public key from a private one.
    """
    try:
        with key_path.open("rb") as fh:
            head = fh.read(PUBKEY_PEEK_BYTES)
    except OSError:
        return False
    return looks_like_pubkey_text(head.decode("utf-8", errors="replace"))


def _preflight(args: argparse.Namespace) -> dict | None:
    """Refuse before any network I/O."""
    key_path = _resolve_key(args.key)
    if not key_path.exists() or not os.access(key_path, os.R_OK):
        return _failure_envelope(args, key_path, FAIL_KEY_FILE_MISSING, 0.0, "")
    if not _looks_like_openssh_pubkey(key_path):
        return _failure_envelope(
            args, key_path, FAIL_KEY_FILE_MISSING, 0.0,
            f"{key_path} doesn't look like an OpenSSH public key",
        )
    return None


# --- remote install ---------------------------------------------------------


def _install_command(pubkey_text: str) -> str:
    """The idempotent install snippet for ``pubkey_text``.

    Delegates to :func:`lib.pubkey.build_install_script` so there is exactly one
    implementation of "append this key to authorized_keys without ssh-copy-id".
    Before 1.4.0 this function was a second, simpler copy — and the library's
    copy was the one with the SELinux restore and the error markers.
    """
    key = pubkey_from_text(pubkey_text)
    return build_install_script(key)


def _remote_install_commands(pubkey_text: str) -> str:
    """Backwards-compatible alias for :func:`_install_command`."""
    return _install_command(pubkey_text)


def _ssh_base_argv(args: argparse.Namespace) -> list[str]:
    argv = ["ssh", "-o", f"ConnectTimeout={args.connect_timeout}"]
    if args.ssh_config:
        argv += ["-F", args.ssh_config]
    return argv


def _run_key_install(args: argparse.Namespace, command: str) -> dict:
    """Install over the *existing* key (no password involved)."""
    argv = _ssh_base_argv(args) + [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=" + (
            "accept-new" if args.accept_new_hostkey else "yes"
        ),
        "--", args.alias, command,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=args.timeout,
            check=False, env=msys_safe_env(), **CHILD_TEXT_KWARGS,
        )
    except subprocess.TimeoutExpired:
        return json_result(
            False, EXIT_TIMEOUT, stderr=f"deploy_pubkey timed out after {args.timeout}s"
        )
    except OSError as e:
        return json_result(
            False, EXIT_TOOL_MISSING, stderr=f"cannot run ssh: {e}",
            data={"failure_class": FAIL_TOOL_MISSING},
        )
    return json_result(
        proc.returncode == 0,
        EXIT_OK if proc.returncode == 0 else EXIT_AUTH,
        stdout=proc.stdout,
        stderr=proc.stderr,
        data={"rc": proc.returncode},
    )


def _run_password_install(args: argparse.Namespace, command: str) -> dict:
    """Install over a password session (works without a pty, e.g. Windows)."""
    import ssh_session  # local import keeps --help dependency-free

    password_env = getattr(args, "password_env", "SSH_PASSWORD")
    if not os.environ.get(password_env):
        return json_result(
            False, EXIT_PASSWORD_REQUIRED,
            stderr="deploy_pubkey: $%s is not set\n" % password_env,
            data={"failure_class": FAIL_PASSWORD_REQUIRED},
        )
    result = ssh_session.execute_single(
        args.alias,
        command,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        ssh_config=args.ssh_config,
        password_env=password_env,
        accept_new_hostkey=args.accept_new_hostkey,
    )
    return {
        "success": result["success"],
        "exit_code": result["exit_code"],
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "data": {
            "rc": result["exit_code"],
            "failure_class": result.get("data", {}).get("failure_class"),
            "auth_method": "password",
            "transport": result.get("data", {}).get("transport"),
        },
    }


def _verify_key_auth(args: argparse.Namespace, key_path: Path) -> dict:
    """Prove the key alone can log in (no password, no agent)."""
    private_key = key_path.with_suffix("")
    argv = _ssh_base_argv(args) + [
        "-o", "BatchMode=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "PubkeyAuthentication=yes",
        "-o", "StrictHostKeyChecking=" + (
            "accept-new" if args.accept_new_hostkey else "yes"
        ),
        "-i", str(private_key),
        "-o", "IdentitiesOnly=yes",
        "--", args.alias, "true",
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=args.connect_timeout + 30, check=False, env=msys_safe_env(),
            **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return json_result(False, EXIT_VERIFY_FAILED, stderr=str(e))
    return json_result(
        proc.returncode == 0,
        EXIT_OK if proc.returncode == 0 else EXIT_VERIFY_FAILED,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _promote_auth_metadata(args: argparse.Namespace, key_path: Path) -> bool:
    """Flip ``# auth:`` to key and point IdentityFile at the new key."""
    try:
        from lib.ssh_config import adopt_key_auth

        outcome = adopt_key_auth(
            args.alias,
            str(key_path.with_suffix("")),
            ssh_config=args.ssh_config,
        )
    except Exception:  # noqa: BLE001 — metadata promotion must never fail a deploy
        return False
    return outcome.changed


# --- envelopes --------------------------------------------------------------


def _first_meaningful_stderr_line(raw_stderr: str) -> str:
    lines = [ln for ln in raw_stderr.splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in lines:
        if "ERROR" in ln or "error" in ln or "denied" in ln.lower():
            return ln.strip()
    return lines[0].strip()


def _success_envelope(
    args: argparse.Namespace, key_path: Path, outcome: str,
    keys_added: int | None, argv: list[str], elapsed: float, raw_stderr: str,
    extra: dict | None = None,
) -> dict:
    if outcome == "already_installed":
        human = f"key already on {args.alias} (key={key_path.name})"
    elif keys_added is None:
        human = f"installed key on {args.alias} (key={key_path.name})"
    else:
        human = f"installed key on {args.alias} (key={key_path.name}, keys_added={keys_added})"
    data = {
        "alias": args.alias,
        "key_name": key_path.name,
        "key_path": str(key_path),
        "key_path_source": getattr(args, "key_path_source", "explicit"),
        "outcome": outcome,
        "keys_added": keys_added,
        "failure_class": None,
        "ssh_argv": argv,
        "elapsed_s": elapsed,
        "raw_stderr": raw_stderr,
    }
    if extra:
        data.update(extra)
    return json_result(
        success=True, exit_code=EXIT_OK, stdout=human + "\n", stderr="", data=data
    )


def _failure_envelope(
    args: argparse.Namespace, key_path: Path, fclass: str,
    elapsed: float, raw_stderr: str, *, argv: list[str] | None = None,
    rc: int = 0,
) -> dict:
    hint = _HUMAN_HINTS.get(fclass, "").format(
        alias=args.alias,
        path=str(key_path),
        timeout=getattr(args, "timeout", _DEFAULT_TIMEOUT),
        rc=rc,
        first_stderr_line=_first_meaningful_stderr_line(raw_stderr),
    )
    if fclass in (FAIL_KEY_FILE_MISSING, FAIL_PASSWORD_REQUIRED, FAIL_AUTH) and raw_stderr:
        hint = raw_stderr.strip() or hint
    return json_result(
        success=False,
        exit_code=_EXIT_CODES.get(fclass, exit_code_for(fclass)),
        stdout="",
        stderr=hint if hint.endswith("\n") else hint + "\n",
        data={
            "alias": args.alias,
            "key_name": key_path.name,
            "key_path": str(key_path),
            "outcome": None,
            "keys_added": None,
            "failure_class": fclass,
            "ssh_argv": argv or [],
            "elapsed_s": elapsed,
            "raw_stderr": raw_stderr,
        },
    )


def _classify_failure(rc: int, stderr: str, timed_out: bool) -> str:
    """Map (rc, stderr) to one failure_class."""
    if timed_out:
        return FAIL_TIMEOUT
    if _UNSUPPORTED_TARGET_RE.search(stderr):
        return FAIL_UNSUPPORTED_TARGET
    if NETWORK_PATTERN.search(stderr):
        return FAIL_NETWORK
    if AUTH_PATTERN.search(stderr):
        return FAIL_AUTH
    if HOST_KEY_PATTERN.search(stderr):
        return FAIL_HOST_KEY_UNKNOWN
    if "permission denied" in stderr.lower():
        return FAIL_PERMISSION
    return FAIL_REMOTE_ERROR


def _audit(args: argparse.Namespace, result: dict) -> None:
    if not audit_enabled(getattr(args, "audit", None), default=False):
        return
    data = result.get("data") or {}
    append_audit(
        args.alias,
        {
            "host": args.alias,
            "route": "deploy_pubkey",
            "key_name": data.get("key_name"),
            "outcome": data.get("outcome"),
            "auth_method": data.get("auth_method", "key"),
            "success": result.get("success"),
            "exit_code": result.get("exit_code"),
            "failure_class": data.get("failure_class"),
            "elapsed_s": data.get("elapsed_s"),
        },
    )


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("deploy_pubkey.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

    if args.key is None:
        chosen, source = _pick_default_key()
        args.key = chosen
        args.key_path_source = source
    else:
        args.key_path_source = "explicit"

    pre = _preflight(args)
    if pre is not None:
        emit(pre, as_json=args.json)
        return int(pre["exit_code"])

    key_path = _resolve_key(args.key)
    pubkey_text = key_path.read_text(encoding="utf-8", errors="replace").strip()
    command = _remote_install_commands(pubkey_text)

    started = time.monotonic()
    install = (
        _run_password_install(args, command)
        if getattr(args, "password", False)
        else _run_key_install(args, command)
    )
    if not install.get("success"):
        fclass = (install.get("data") or {}).get("failure_class") or _classify_failure(
            int(install.get("exit_code", 1)), install.get("stderr", ""), False
        )
        # A key-route install that fails on auth is not necessarily fatal: the
        # key may simply not be there yet, which is what --password is for.
        if fclass in ("auth", "host_key") and not getattr(args, "password", False):
            fclass = FAIL_AUTH if fclass == "auth" else FAIL_HOST_KEY_UNKNOWN
        result = _failure_envelope(
            args, key_path, fclass, round(time.monotonic() - started, 3),
            install.get("stderr", ""), argv=install.get("data", {}).get("ssh_argv", []),
            rc=int(install.get("exit_code", 1)),
        )
    else:
        output = install.get("stdout") or ""
        parsed = parse_install_output(output)
        outcome = (
            "already_installed"
            if parsed.outcome == "already_installed"
            else "installed"
        )
        verify = _verify_key_auth(args, key_path)
        promoted = False
        if verify["success"] and getattr(args, "promote_auth", False):
            promoted = _promote_auth_metadata(args, key_path)
        if not verify["success"]:
            result = _failure_envelope(
                args, key_path, FAIL_VERIFY_FAILED,
                round(time.monotonic() - started, 3),
                verify.get("stderr", ""),
                rc=verify["exit_code"],
            )
            result["data"]["outcome"] = outcome
        else:
            result = _success_envelope(
                args, key_path, outcome,
                0 if outcome == "already_installed" else None,
                install.get("data", {}).get("ssh_argv", []),
                round(time.monotonic() - started, 3),
                install.get("stderr", ""),
                extra={
                    "verified": True,
                    "auth_method": "password" if getattr(args, "password", False) else "key",
                    "transport": install.get("data", {}).get("transport"),
                    "auth_metadata_promoted": promoted,
                },
            )

    _audit(args, result)
    redactor = redactor_for(args.alias, args.ssh_config, enabled=args.redact)
    emit(result, as_json=args.json, redactor=redactor)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(guarded_main("deploy_pubkey.py", main))
