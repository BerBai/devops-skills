"""One global failure_class → exit_code table (SSH-19).

Before this module every script invented its own numbers: ``ssh_execute``
returned 3 for a missing dependency while ``deploy_pubkey`` returned 10,
``ssh_upload`` reported a local file that does not exist as
``remote_error`` (exit 1), and so on. An agent that only looks at
``exit_code`` therefore could not tell two very different worlds apart.

The contract now is:

* **One class, one code.** ``EXIT_CODES`` is the only mapping. Scripts must
  call :func:`exit_code_for` instead of hardcoding integers.
* **Stable buckets** (0-9) are shared by every script. Anything a script
  needs on top of that lives in its own hundred block, so the number alone
  identifies the script family:

  =====  ==========================================
  0      success
  1      internal / unclassified error
  2      invalid arguments (argv, refused option)
  3      dependency_missing (paramiko, pty, scp, ...)
  4      not_found
  5      already_exists
  6      config_missing
  7      parse_error
  8      permission
  9      verify_failed
  =====  ==========================================

  100s   ssh_execute / ssh_session / ssh_cluster
  120s   ssh_tunnel
  140s   deploy_pubkey
  160s   transfer (upload / download / server_transfer), including 168 for
         "the destination already exists and nobody said --overwrite"
  180s   config_manager write-path extras
  200s   daemon
  220s   audit tooling (summarize / prune / rotate)

``124`` keeps the GNU ``timeout`` meaning (timed out) everywhere.
``255`` is OpenSSH's own client-failure code and is passed through
unchanged; it is not part of this table.
"""

from __future__ import annotations

# --- buckets (shared by every script) ---------------------------------------
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INVALID_ARGUMENTS = 2
EXIT_DEPENDENCY_MISSING = 3
EXIT_NOT_FOUND = 4
EXIT_ALREADY_EXISTS = 5
EXIT_CONFIG_MISSING = 6
EXIT_PARSE_ERROR = 7
EXIT_PERMISSION = 8
EXIT_VERIFY_FAILED = 9
EXIT_TIMEOUT = 124

# --- exec / session family (100s) -------------------------------------------
EXIT_AUTH = 100
EXIT_HOST_KEY = 101
EXIT_NETWORK = 102
EXIT_KEX = 103
EXIT_REMOTE_ERROR = 104
EXIT_PASSWORD_REQUIRED = 105
EXIT_NO_TTY = 106
EXIT_INVALID_OPTION = 107
EXIT_UNSUPPORTED_TARGET = 108
EXIT_CONFIRMATION_REQUIRED = 109
EXIT_PROMPT_TIMEOUT = 110
EXIT_CONNECTION_REUSED_ONLY = 111
EXIT_SUDO_REQUIRED = 112
EXIT_SUDO_AUTH_FAILED = 113
# REV-08 (2026-11 review A2): the askpass helper could not be staged, so
# password auth is impossible on this machine. It used to escape as a bare
# traceback, breaking the "every path has an envelope" contract.
EXIT_ASKPASS_UNAVAILABLE = 114
# REV-04 (2026-11 review B1): password auth was requested but no username could
# be determined (no `user@`, no `--user`, no `User` in the config), so ssh would
# silently log in as the *local* account and report a bare "Permission denied".
EXIT_USER_REQUIRED = 115

# --- tunnel family (120s) ----------------------------------------------------
EXIT_POSIX_ONLY = 120
EXIT_PORT_BUSY = 121
EXIT_PORT_POOL_EXHAUSTED = 122
EXIT_SSH_CONNECT_FAILED = 123
EXIT_FORWARD_SETUP_FAILED = 125
EXIT_TUNNEL_NOT_FOUND = 126
EXIT_AMBIGUOUS_STOP = 127
EXIT_STALE_STATE_ONLY = 128
EXIT_KILL_FAILED = 129

# --- deploy_pubkey family (140s) --------------------------------------------
EXIT_TOOL_MISSING = 140
EXIT_KEY_FILE_MISSING = 141
EXIT_SSH_CONFIG_UNSUPPORTED = 142

# --- transfer family (160s) --------------------------------------------------
EXIT_LOCAL_PATH_MISSING = 160
EXIT_LOCAL_PATH_IS_DIRECTORY = 161
EXIT_REMOTE_PATH_MISSING = 162
EXIT_REMOTE_PATH_INVALID = 163
EXIT_TRANSFER_IO = 164
EXIT_TRANSFER_INTEGRITY = 165
EXIT_MODE_UNSUPPORTED = 166
EXIT_TRANSFER_TOOL_MISSING = 167
# GAP-003: the transfer refused to clobber an existing destination. It is its
# own number (not `already_exists` = 5) because 5 is a *bucket* code shared by
# every script, while this one is the transfer family's "say --overwrite".
EXIT_REMOTE_PATH_EXISTS = 168

# --- config_manager extras (180s) -------------------------------------------
EXIT_WILDCARD_REFUSED = 180
EXIT_AMBIGUOUS_ALIAS = 181

# --- daemon (200s) -----------------------------------------------------------
EXIT_DAEMON_NOT_RUNNING = 200
EXIT_DAEMON_ALREADY_RUNNING = 201
EXIT_DAEMON_PROTOCOL = 202

# --- audit tooling (220s) ----------------------------------------------------
EXIT_AUDIT_NO_RECORDS = 220
EXIT_AUDIT_BAD_SINCE = 221

#: ``failure_class`` → exit code. This is the authoritative table.
EXIT_CODES: dict[str, int] = {
    # success / generic
    "ok": EXIT_OK,
    "internal": EXIT_INTERNAL,
    "invalid_arguments": EXIT_INVALID_ARGUMENTS,
    "invalid_option": EXIT_INVALID_OPTION,
    "dependency_missing": EXIT_DEPENDENCY_MISSING,
    "tool_missing": EXIT_TOOL_MISSING,
    "not_found": EXIT_NOT_FOUND,
    # FA-22: a destination that does not resolve *as an alias* is a lookup
    # failure, not a network failure — "Could not resolve hostname" used to be
    # reported as `network` (102), which points the operator at DNS/firewall
    # instead of at the typo in ~/.ssh/config.
    "alias_not_found": EXIT_NOT_FOUND,
    "tunnel_not_found": EXIT_TUNNEL_NOT_FOUND,
    "already_exists": EXIT_ALREADY_EXISTS,
    "config_missing": EXIT_CONFIG_MISSING,
    "parse_error": EXIT_PARSE_ERROR,
    "permission": EXIT_PERMISSION,
    "verify_failed": EXIT_VERIFY_FAILED,
    "timeout": EXIT_TIMEOUT,
    # exec / session
    "auth": EXIT_AUTH,
    "host_key": EXIT_HOST_KEY,
    "network": EXIT_NETWORK,
    "kex": EXIT_KEX,
    "remote_error": EXIT_REMOTE_ERROR,
    "password_required": EXIT_PASSWORD_REQUIRED,
    "no_tty_for_password": EXIT_NO_TTY,
    "unsupported_target": EXIT_UNSUPPORTED_TARGET,
    "confirmation_required": EXIT_CONFIRMATION_REQUIRED,
    "prompt_timeout": EXIT_PROMPT_TIMEOUT,
    "sudo_required": EXIT_SUDO_REQUIRED,
    "sudo_auth_failed": EXIT_SUDO_AUTH_FAILED,
    # reviews A2 / B1 (1.5.0)
    "askpass_unavailable": EXIT_ASKPASS_UNAVAILABLE,
    "user_required": EXIT_USER_REQUIRED,
    # cluster
    "target_resolution_failed": EXIT_NOT_FOUND,
    "partial_failure": EXIT_INTERNAL,
    "all_hosts_failed": EXIT_INTERNAL,
    "health_check_failed": EXIT_NETWORK,
    "skipped_fail_fast": EXIT_INTERNAL,
    "ssh_execute_missing": EXIT_DEPENDENCY_MISSING,
    "ssh_execute_broken": EXIT_INTERNAL,
    # tunnel
    "posix_only": EXIT_POSIX_ONLY,
    "port_busy": EXIT_PORT_BUSY,
    "port_pool_exhausted": EXIT_PORT_POOL_EXHAUSTED,
    "ssh_connect_failed": EXIT_SSH_CONNECT_FAILED,
    "forward_setup_failed": EXIT_FORWARD_SETUP_FAILED,
    "ambiguous_stop": EXIT_AMBIGUOUS_STOP,
    "stale_state_only": EXIT_STALE_STATE_ONLY,
    "kill_failed": EXIT_KILL_FAILED,
    # deploy_pubkey
    "key_file_missing": EXIT_KEY_FILE_MISSING,
    "ssh_config_unsupported": EXIT_SSH_CONFIG_UNSUPPORTED,
    # transfers
    "local_path_missing": EXIT_LOCAL_PATH_MISSING,
    "local_path_is_directory": EXIT_LOCAL_PATH_IS_DIRECTORY,
    "remote_path_missing": EXIT_REMOTE_PATH_MISSING,
    "remote_path_invalid": EXIT_REMOTE_PATH_INVALID,
    # GAP-003: `ssh_upload` refuses to overwrite an existing remote file
    # unless --overwrite/--resume/--delete was given.
    "remote_path_exists": EXIT_REMOTE_PATH_EXISTS,
    "dst_path_invalid": EXIT_REMOTE_PATH_INVALID,
    "src_path_missing": EXIT_REMOTE_PATH_MISSING,
    "src_is_directory": EXIT_REMOTE_PATH_MISSING,
    "transfer_io": EXIT_TRANSFER_IO,
    "integrity_mismatch": EXIT_TRANSFER_INTEGRITY,
    # FA-16: emitted by lib/sftp_transport on a post-transfer hash mismatch.
    # Without this key `exit_code_for()` fell back to 1 while TransferResult
    # carried 9, so the same failure had two different codes.
    "transfer_verify_failed": EXIT_VERIFY_FAILED,
    # FA-05/FA-44: `--delete`/`--preserve` on a transport that cannot honour
    # them (scp/tar) is refused with this class instead of a fake success.
    "mode_deferred": EXIT_MODE_UNSUPPORTED,
    "network_src": EXIT_NETWORK,
    "network_dst": EXIT_NETWORK,
    "auth_src": EXIT_AUTH,
    "auth_dst": EXIT_AUTH,
    # config manager
    "wildcard_refused": EXIT_WILDCARD_REFUSED,
    "ambiguous_alias": EXIT_AMBIGUOUS_ALIAS,
    # daemon
    "daemon_not_running": EXIT_DAEMON_NOT_RUNNING,
    "daemon_protocol": EXIT_DAEMON_PROTOCOL,
    # audit tooling
    "audit_no_records": EXIT_AUDIT_NO_RECORDS,
    "audit_bad_since": EXIT_AUDIT_BAD_SINCE,
}


def exit_code_for(failure_class: str | None, *, default: int = EXIT_INTERNAL) -> int:
    """Return the registered exit code for ``failure_class``.

    ``None`` means success. Unknown classes fall back to ``default`` (1) so a
    new failure mode can never leak a random number into the contract.
    """
    if failure_class is None:
        return EXIT_OK
    return EXIT_CODES.get(failure_class, default)


#: Classes that are deliberate *aliases* of another class's code. They exist
#: because a script had already shipped that spelling; the test suite allows
#: these to share a code and nothing else.
ALIASES: dict[str, str] = {
    "alias_not_found": "not_found",
    "transfer_verify_failed": "verify_failed",
    "dst_path_invalid": "remote_path_invalid",
    "src_path_missing": "remote_path_missing",
    "src_is_directory": "remote_path_missing",
    "network_src": "network",
    "network_dst": "network",
    "auth_src": "auth",
    "auth_dst": "auth",
    "target_resolution_failed": "not_found",
    "partial_failure": "internal",
    "all_hosts_failed": "internal",
    "skipped_fail_fast": "internal",
    "ssh_execute_broken": "internal",
    "ssh_execute_missing": "dependency_missing",
    "health_check_failed": "network",
}


def describe(exit_code: int) -> str:
    """Reverse lookup for humans: the class(es) registered for ``exit_code``."""
    names = [k for k, v in EXIT_CODES.items() if v == exit_code]
    return ", ".join(sorted(names)) or "unregistered"


__all__ = [
    name
    for name in dir()
    if name.startswith("EXIT_")
    or name in ("ALIASES", "EXIT_CODES", "describe", "exit_code_for")
]
