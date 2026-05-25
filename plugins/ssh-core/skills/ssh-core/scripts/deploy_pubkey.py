#!/usr/bin/env python3
"""deploy_pubkey.py — install your public key on a remote host (v1.0).

Public surface::

    python deploy_pubkey.py <alias>
        [--key PATH]                     default: ~/.ssh/id_ed25519.pub
        [--connect-timeout N]            default: 8
        [--timeout N]                    default: 180
        [--ssh-config PATH]              default: unset (ssh-copy-id reads ~/.ssh/config)
        [--accept-new-hostkey]           default: off (opt-in)
        [--json]

Wraps the system ``ssh-copy-id`` binary. ``ssh-copy-id`` handles the
remote ``~/.ssh`` perms, ``authorized_keys`` perms, and dedupe-before-
append; we add the JSON envelope, preflight refusals, and failure
classification.

First-touch auth is **interactive only**: ``subprocess.run`` inherits the
parent's stdin so the controlling tty handles the one-time password
prompt. Non-TTY callers get a clean ``no_tty_for_password`` refusal.

Idempotency: ssh-copy-id reporting "All keys were skipped because they
already exist" maps to ``data.outcome = "already_installed"``,
``success = True``, even when older OpenSSH builds exit non-zero on it.

Exit codes (per-script namespace, mirrors ssh_config_manager precedent)::

    0    success (outcome=installed OR already_installed)
    1    remote_error (ssh-copy-id non-zero, no classification match)
    10   tool_missing — ssh-copy-id not on PATH
    11   key_file_missing — --key file missing / unreadable / not a pubkey
    12   no_tty_for_password — stdin not a TTY and password prompt blocked
    13   auth — Permission denied
    14   host_key_unknown — Host key verification failed
    15   network — unreachable / DNS / connect timeout
    124  timeout — subprocess.TimeoutExpired (GNU timeout(1) convention)

See:
    .trellis/tasks/05-24-ssh-core-deploy-pubkey/prd.md      — D1-D7 decisions
    .trellis/tasks/05-24-ssh-core-deploy-pubkey/design.md   — regex tables, exit codes
    .trellis/spec/backend/adr-001-ssh-execute.md            — sibling native-OpenSSH ADR
    plugins/ssh-core/skills/ssh-core/references/safety.md   — pubkey deploy philosophy
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import emit, json_result, msys_safe_env  # noqa: E402, I001

# --- Constants --------------------------------------------------------------

_DEFAULT_KEY = "~/.ssh/id_ed25519.pub"
_DEFAULT_CONNECT_TIMEOUT = 8
_DEFAULT_TIMEOUT = 180

# Failure classes (per-script namespace, deploy_pubkey-local).
FAIL_TOOL_MISSING = "tool_missing"
FAIL_KEY_FILE_MISSING = "key_file_missing"
FAIL_NO_TTY = "no_tty_for_password"
FAIL_AUTH = "auth"
FAIL_HOST_KEY_UNKNOWN = "host_key_unknown"
FAIL_NETWORK = "network"
FAIL_TIMEOUT = "timeout"
FAIL_REMOTE_ERROR = "remote_error"

# Exit codes (per design.md exit-code pin; verified non-colliding with
# error-handling.md's documented set {0,1,2,4-9,124} on 2026-05-25).
_EXIT_CODES: dict[str, int] = {
    FAIL_TOOL_MISSING: 10,
    FAIL_KEY_FILE_MISSING: 11,
    FAIL_NO_TTY: 12,
    FAIL_AUTH: 13,
    FAIL_HOST_KEY_UNKNOWN: 14,
    FAIL_NETWORK: 15,
    FAIL_TIMEOUT: 124,
    FAIL_REMOTE_ERROR: 1,
}

# One short actionable stderr line per failure class.
_HUMAN_HINTS: dict[str, str] = {
    FAIL_TOOL_MISSING: "ssh-copy-id not found on PATH. Install openssh-client (linux) or openssh (macOS via brew).",
    FAIL_KEY_FILE_MISSING: "public key not found or invalid: {path}. Generate with `ssh-keygen` or pass --key <path>.",
    FAIL_NO_TTY: "first-touch deploy requires an interactive terminal for the password prompt. Re-run from a shell, or run `ssh-copy-id` manually then re-invoke deploy_pubkey.",
    FAIL_AUTH: "authentication to {alias} failed. Check the password or use --accept-new-hostkey if first connection.",
    FAIL_HOST_KEY_UNKNOWN: "{alias}'s host key is not in ~/.ssh/known_hosts. Re-run with --accept-new-hostkey to trust on first use.",
    FAIL_NETWORK: "could not reach {alias}: {first_stderr_line}",
    FAIL_TIMEOUT: "deploy_pubkey timed out after {timeout}s while talking to {alias}",
    FAIL_REMOTE_ERROR: "ssh-copy-id failed with exit {rc}. See data.raw_stderr for full output.",
}

# Sentinel returncode for _run_ssh_copy_id on TimeoutExpired (real shell
# exit codes are 0-255 so -1 is unambiguous).
_RC_TIMEOUT_SENTINEL = -1
_PUBKEY_PEEK_BYTES = 200

# OpenSSH public-key type prefixes; first whitespace-delimited token of
# any ``.pub`` file. Extending the tuple supports new key types without
# any other code change.
_PUBKEY_PREFIXES: tuple[str, ...] = (
    "ssh-rsa",
    "ssh-ed25519",
    "ssh-dss",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)

# Compiled regexes (per design.md "stderr parsing" + "Failure-class table").
_INSTALLED_RE = re.compile(r"Number of key\(s\) added:\s*(\d+)")
_ALREADY_RE = re.compile(r"All keys were skipped because they already exist")
_AUTH_RE = re.compile(r"Permission denied \([^)]*\)")
_HOST_KEY_RE = re.compile(r"(Host key verification failed|No matching host key)")
_NETWORK_RE = re.compile(
    r"(Could not resolve hostname|Connection refused|"
    r"Connection timed out|Network is unreachable|No route to host)"
)
_PROMPT_FAIL_RE = re.compile(
    r"(Permission denied, please try again|password:|Authentication failed)"
)


# --- Parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy_pubkey.py",
        description=(
            "Bootstrap public-key auth on a remote host by wrapping system "
            "ssh-copy-id. Inherits the controlling tty for the one-time "
            "password prompt; emits the shared JSON envelope with --json."
        ),
    )
    p.add_argument("alias", help="Host alias from ~/.ssh/config (or user@host if not aliased)")
    p.add_argument(
        "--key", default=_DEFAULT_KEY,
        help=f"Path to the .pub file to install (default: {_DEFAULT_KEY})",
    )
    p.add_argument(
        "--connect-timeout", type=int, default=_DEFAULT_CONNECT_TIMEOUT,
        help=f"SSH connect timeout in seconds, applied as -o ConnectTimeout (default: {_DEFAULT_CONNECT_TIMEOUT})",
    )
    p.add_argument(
        "--timeout", type=int, default=_DEFAULT_TIMEOUT,
        help=f"Total wall-clock budget for ssh-copy-id, in seconds (default: {_DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--ssh-config", default=None,
        help="Pass -F <path> to ssh-copy-id (default: ssh-copy-id reads ~/.ssh/config)",
    )
    p.add_argument(
        "--accept-new-hostkey", action="store_true",
        help="Opt-in: pass -o StrictHostKeyChecking=accept-new for first-touch trust",
    )
    p.add_argument("--json", action="store_true", help="Emit the shared JSON output contract on stdout")
    return p


# --- Preflight + arg munging -----------------------------------------------


def _resolve_key(path: str) -> Path:
    """Expand ``~`` and make absolute. Does NOT check existence."""
    return Path(path).expanduser().resolve()


def _looks_like_openssh_pubkey(key_path: Path) -> bool:
    """Read first 200 bytes; return True iff the file starts with a known
    OpenSSH public-key type token. Catches the "user passed a private key"
    catastrophe before ssh-copy-id appends it verbatim to authorized_keys.
    """
    try:
        with key_path.open("rb") as fh:
            head = fh.read(_PUBKEY_PEEK_BYTES)
    except OSError:
        return False
    text = head.decode("utf-8", errors="replace").lstrip()
    return any(text.startswith(prefix + " ") for prefix in _PUBKEY_PREFIXES)


def _preflight(args: argparse.Namespace) -> dict | None:
    """Refuse before any subprocess spawn. Three refusal paths:
    tool_missing / key_file_missing(does-not-exist) / key_file_missing(not-a-pubkey).
    Returns None when preflight passes.
    """
    if shutil.which("ssh-copy-id") is None:
        return _failure_envelope(
            args, _resolve_key(args.key), FAIL_TOOL_MISSING, [], 0.0, "",
        )
    key_path = _resolve_key(args.key)
    if not key_path.exists() or not os.access(key_path, os.R_OK):
        return _failure_envelope(
            args, key_path, FAIL_KEY_FILE_MISSING, [], 0.0, "",
        )
    if not _looks_like_openssh_pubkey(key_path):
        return _failure_envelope(
            args, key_path, FAIL_KEY_FILE_MISSING, [], 0.0,
            f"{key_path} doesn't look like an OpenSSH public key",
        )
    return None


# --- ssh-copy-id argv assembly ---------------------------------------------


def _build_argv(args: argparse.Namespace, key_path: Path) -> list[str]:
    """Compose ssh-copy-id argv:
        ssh-copy-id -i <key_path> -o ConnectTimeout=N
            [-F <ssh_config>] [-o StrictHostKeyChecking=accept-new] <alias>

    Never adds ``-o BatchMode=yes`` — that would defeat the password prompt
    that is the whole point of deploy_pubkey.
    """
    argv: list[str] = [
        "ssh-copy-id", "-i", str(key_path),
        "-o", f"ConnectTimeout={args.connect_timeout}",
    ]
    if args.ssh_config:
        argv += ["-F", args.ssh_config]
    if args.accept_new_hostkey:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    argv += [args.alias]
    return argv


# --- subprocess wrapper ----------------------------------------------------


def _run_ssh_copy_id(argv: list[str], timeout: int) -> tuple[int, str, str, float, bool]:
    """Spawn ssh-copy-id with controlling-tty inheritance.

    Returns ``(returncode, stdout, stderr, elapsed_s, timed_out)``. Raises
    nothing — TimeoutExpired is caught and surfaced via the ``timed_out``
    flag + sentinel returncode. ``stdin=None`` is the critical invariant:
    ssh-copy-id's underlying ssh prompts the parent's controlling tty for
    the password; ``stdin=PIPE`` would break that path.
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            stdin=None,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=msys_safe_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _RC_TIMEOUT_SENTINEL, "", "", float(timeout), True

    elapsed = round(time.monotonic() - start, 3)
    return proc.returncode, proc.stdout, proc.stderr, elapsed, False


# --- Result classification -------------------------------------------------


def _parse_success_outcome(stderr: str) -> tuple[str, int | None]:
    """For rc==0 (or the idempotency override): return (outcome, keys_added).

    outcome in {'installed', 'already_installed'}; keys_added is the parsed
    integer ("Number of key(s) added: N") or 0 for already_installed, or
    None if ssh-copy-id stderr wording shifted and we couldn't match.
    """
    m = _INSTALLED_RE.search(stderr)
    if m is not None:
        return "installed", int(m.group(1))
    if _ALREADY_RE.search(stderr):
        return "already_installed", 0
    # Fallback: success but we couldn't parse the number — null keys_added
    # rather than misreport.
    return "installed", None


def _classify_failure(
    rc: int, stderr: str, is_tty: bool, timed_out: bool
) -> str:
    """Map (rc, stderr, is_tty, timed_out) to one failure_class string.

    Precedence (per design.md table):
        timeout > no_tty_for_password > auth > host_key_unknown
              > network > remote_error
    """
    if timed_out:
        return FAIL_TIMEOUT
    # no_tty special case: stdin isn't a TTY AND ssh-copy-id failed on a
    # password-prompt-shaped stderr. Must precede the auth match so users
    # in CI/headless contexts get the right remediation hint.
    if not is_tty and _PROMPT_FAIL_RE.search(stderr):
        return FAIL_NO_TTY
    if _AUTH_RE.search(stderr):
        return FAIL_AUTH
    if _HOST_KEY_RE.search(stderr):
        return FAIL_HOST_KEY_UNKNOWN
    if _NETWORK_RE.search(stderr):
        return FAIL_NETWORK
    return FAIL_REMOTE_ERROR


# --- Envelope construction -------------------------------------------------


def _success_envelope(
    args: argparse.Namespace, key_path: Path, outcome: str,
    keys_added: int | None, argv: list[str], elapsed: float, raw_stderr: str,
) -> dict:
    """Build the success envelope per PRD D6."""
    if outcome == "already_installed":
        human = f"key already on {args.alias} (key={key_path.name})"
    elif keys_added is None:
        human = f"installed key on {args.alias} (key={key_path.name})"
    else:
        human = f"installed key on {args.alias} (key={key_path.name}, keys_added={keys_added})"
    return json_result(
        success=True, exit_code=0, stdout=human + "\n", stderr="",
        data={
            "alias": args.alias,
            "key_path": str(key_path),
            "outcome": outcome,
            "keys_added": keys_added,
            "failure_class": None,
            "ssh_copy_id_argv": argv,
            "elapsed_s": elapsed,
            "raw_stderr": raw_stderr,
        },
    )


def _first_meaningful_stderr_line(raw_stderr: str) -> str:
    """First ERROR-flavoured line of ssh-copy-id stderr, falling back to the
    first non-empty line. Used for the human-mode network hint string."""
    lines = [ln for ln in raw_stderr.splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in lines:
        if "ERROR" in ln or "error" in ln:
            return ln.strip()
    return lines[0].strip()


def _failure_envelope(
    args: argparse.Namespace, key_path: Path, fclass: str,
    argv: list[str], elapsed: float, raw_stderr: str, rc: int = 0,
) -> dict:
    """Build a failure envelope per PRD D6. ``rc`` feeds the remote_error
    hint; other hint fields come from args / key_path / raw_stderr."""
    hint = _HUMAN_HINTS[fclass].format(
        alias=args.alias,
        path=str(key_path),
        timeout=getattr(args, "timeout", _DEFAULT_TIMEOUT),
        rc=rc,
        first_stderr_line=_first_meaningful_stderr_line(raw_stderr),
    )
    # On the non-pubkey-content path the preflight stashed the hint into
    # raw_stderr — preserve that detail.
    if fclass == FAIL_KEY_FILE_MISSING and raw_stderr:
        hint = raw_stderr
    return json_result(
        success=False,
        exit_code=_EXIT_CODES[fclass],
        stdout="",
        stderr=hint if hint.endswith("\n") else hint + "\n",
        data={
            "alias": args.alias,
            "key_path": str(key_path),
            "outcome": None,
            "keys_added": None,
            "failure_class": fclass,
            "ssh_copy_id_argv": argv,
            "elapsed_s": elapsed,
            "raw_stderr": raw_stderr,
        },
    )


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    pre = _preflight(args)
    if pre is not None:
        emit(pre, as_json=args.json)
        return int(pre["exit_code"])

    key_path = _resolve_key(args.key)
    argv_list = _build_argv(args, key_path)
    rc, _stdout, stderr, elapsed, timed_out = _run_ssh_copy_id(argv_list, args.timeout)

    if rc == 0:
        outcome, keys_added = _parse_success_outcome(stderr)
        result = _success_envelope(
            args, key_path, outcome, keys_added, argv_list, elapsed, stderr
        )
    elif not timed_out and _ALREADY_RE.search(stderr):
        # Idempotency override: some OpenSSH builds exit non-zero when every
        # key is already installed. Treat as success/already_installed.
        result = _success_envelope(
            args, key_path, "already_installed", 0, argv_list, elapsed, stderr
        )
    else:
        fclass = _classify_failure(rc, stderr, sys.stdin.isatty(), timed_out)
        result = _failure_envelope(
            args, key_path, fclass, argv_list, elapsed, stderr, rc=rc
        )

    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
