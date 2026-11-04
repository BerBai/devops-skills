"""One place that decides how a transfer authenticates (SSHCORE-026 follow-up).

``ssh_execute`` / ``ssh_session`` / ``ssh_cluster`` / ``ssh_tunnel`` /
``ssh_daemon`` all route password authentication through
``lib.askpass.askpass_env``. The three transfer scripts never did: their scp,
tar and rsync backends hardcoded ``-o BatchMode=yes``, so on a password-only
host the "no paramiko needed, scp is enough" fallback answered
``Permission denied (publickey...)`` and the *only* remaining transport was the
paramiko SFTP path — which is exactly what a host without paramiko cannot have.

This module gives them the same three-way decision, so ``--auth auto`` can read
``# auth: password`` from ``~/.ssh/config`` (the metadata
``ssh_config_manager.py`` writes) and the backends can ask ``askpass`` for the
secret instead of failing.

Nothing here ever puts the password in argv, the envelope or the audit log: the
callers only receive the string and hand it to ``askpass_env``.
"""

from __future__ import annotations

import os

from . import read_ssh_config_metadata

#: Environment variable consulted when ``--password-env`` is not given.
DEFAULT_PASSWORD_ENV = "SSH_PASSWORD"

AUTH_CHOICES = ("auto", "key", "password")


def resolve(
    alias: str,
    requested: str,
    ssh_config: str | None,
    password_env: str = DEFAULT_PASSWORD_ENV,
) -> tuple[str, str | None]:
    """Return ``(auth_method, password_or_None)`` for one transfer.

    * ``key``      → ``("key", None)``: BatchMode publickey, as before.
    * ``password`` → ``("password", secret)``; ``secret`` is ``None`` when the
      environment variable is unset, which callers turn into
      ``password_required`` (exit 105) *before* spawning anything.
    * ``auto``     → ``password`` when ``~/.ssh/config`` says
      ``# auth: password`` (or ``# device-type: network``), otherwise ``key``.
    """
    method = requested
    if method == "auto":
        meta = read_ssh_config_metadata(alias, ssh_config)
        if (
            meta.get("auth", "").lower() == "password"
            or meta.get("device-type", "").lower() == "network"
        ):
            method = "password"
        else:
            method = "key"
    if method == "password":
        return "password", (os.environ.get(password_env) or None)
    return "key", None


def auth_options(password_auth: bool) -> list[str]:
    """The ``-o`` pairs that pin an auth profile for ssh/scp/rsync.

    Mirrors ``ssh_execute._build_remote_argv`` and
    ``ssh_tunnel._tunnel_argv``: ``BatchMode=yes`` on the key route (a prompt
    means "wrong host/config", and hanging is worse than failing), the explicit
    password methods on the other.
    """
    if password_auth:
        return [
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "UpdateHostKeys=no",
        ]
    return ["-o", "BatchMode=yes"]


__all__ = ["AUTH_CHOICES", "DEFAULT_PASSWORD_ENV", "auth_options", "resolve"]
