"""Non-interactive privilege escalation for remote commands (DV-14).

Real hosts rarely give every operator root. ``ssh_execute.py`` could run
``systemctl status nginx`` as the login user and nothing else: there was no
way to reach a root-only path, and the alternative — ``ssh ... "sudo -S ..."``
by hand from a shell — is exactly what ``SKILL.md`` forbids, because it either
hangs on a password prompt or leaks the password into argv.

Design (the whole point is that it can never hang):

* Wrap as ``sudo -n [-u USER] -- sh -c '<command>'``. ``-n`` means
  *non-interactive*: sudo fails immediately instead of waiting forever for a
  password on a terminal that does not exist.
* Optional ``--sudo-password-env`` feeds a password through ``sudo -S`` on
  stdin (never argv, never the environment of a logged call). It is opt-in
  because the safe, boring answer is a NOPASSWD sudoers rule.
* Preflight: ``sudo -n true``. A failure is reported as
  ``failure_class: "sudo_required"`` (exit 112) *before* the real command runs,
  so a failed escalation never masquerades as a failed command.
* The wrapped command's own exit status passes through untouched, exactly like
  the non-sudo path.

Helpers here build strings/argv only; the runner lives in ``ssh_execute.py``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

#: Default user when ``--sudo`` is given without a value.
DEFAULT_SUDO_USER = "root"

#: ``failure_class`` used when sudo refuses to escalate non-interactively.
SUDO_REQUIRED = "sudo_required"

#: ``failure_class`` for a wrong sudo password (only with --sudo-password-env).
SUDO_AUTH_FAILED = "sudo_auth_failed"

#: Strings sudo prints when it cannot escalate without a terminal.
NONINTERACTIVE_HINTS: tuple[str, ...] = (
    "a password is required",
    "a terminal is required",
    "no tty present",
    "sudo: sorry, you must have a tty",
    "is not in the sudoers file",
    "not allowed to execute",
    "no askpass program specified",
)

#: Strings sudo prints when the supplied password was wrong.
AUTH_FAILED_HINTS: tuple[str, ...] = (
    "incorrect password",
    "sorry, try again",
    "authentication failure",
)


@dataclass(frozen=True)
class SudoSpec:
    """A resolved ``--sudo`` request."""

    user: str | None = None
    password_env: str | None = None
    preserve_env: bool = False

    @property
    def enabled(self) -> bool:
        return self.user is not None or self.password_env is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "user": self.user or DEFAULT_SUDO_USER,
            "password_from_env": self.password_env,
            "preserve_env": self.preserve_env,
        }


def parse_sudo(value: str | None) -> SudoSpec | None:
    """Turn ``--sudo`` / ``--sudo=USER`` into a :class:`SudoSpec`.

    ``value`` is ``None`` when the flag was not given at all, an empty string
    for a bare ``--sudo`` (⇒ root), or a user name.
    """
    if value is None:
        return None
    user = value.strip() or DEFAULT_SUDO_USER
    return SudoSpec(user=user)


def wrap(
    command: str,
    spec: SudoSpec,
    *,
    non_interactive: bool = True,
    use_stdin_password: bool = False,
) -> str:
    """Return the shell string that runs ``command`` under sudo.

    The command is single-quoted for the *remote* shell, so it reaches
    ``sh -c`` as one argument with no further expansion — the same guarantee
    the non-sudo route gives (no shell metacharacter surprises introduced by
    the wrapper itself).

    ``use_stdin_password`` switches ``-n`` to ``-S`` (read the password from
    stdin); both are mutually exclusive by design: a caller that promised a
    password must supply it, and one that did not must fail fast.
    """
    flags: list[str] = []
    flags.append("-S" if use_stdin_password else ("-n" if non_interactive else "-A"))
    if spec.preserve_env:
        flags.append("-E")
    if spec.user:
        flags += ["-u", shlex.quote(spec.user)]
    return "sudo " + " ".join(flags) + " -- sh -c " + shlex.quote(command)


def probe_command(spec: SudoSpec, *, use_stdin_password: bool = False) -> str:
    """The preflight command: prove escalation works without running anything."""
    return wrap("true", spec, use_stdin_password=use_stdin_password)


def classify(stderr: str) -> str | None:
    """Map sudo's own stderr to a failure class, or ``None`` if unrelated."""
    lowered = (stderr or "").lower()
    if any(hint in lowered for hint in AUTH_FAILED_HINTS):
        return SUDO_AUTH_FAILED
    if any(hint in lowered for hint in NONINTERACTIVE_HINTS):
        return SUDO_REQUIRED
    return None


def remediation(spec: SudoSpec) -> str:
    """One-paragraph, copy-pasteable fix shown next to a sudo failure."""
    user = spec.user or DEFAULT_SUDO_USER
    target = "root" if user == DEFAULT_SUDO_USER else user
    return (
        f"ssh-core: sudo could not escalate to {target} without a terminal. "
        "Either grant NOPASSWD for this command, e.g.\n"
        f"    echo '{_sudoers_user()} ALL=(root) NOPASSWD: /bin/systemctl' "
        "| sudo tee /etc/sudoers.d/ssh-core-readonly\n"
        "    # then validate with: sudo visudo -c\n"
        "or pass the password through an environment variable (never argv):\n"
        "    SSH_SUDO_PASSWORD='...' python scripts/ssh_execute.py <alias> "
        "--sudo --sudo-password-env SSH_SUDO_PASSWORD -- 'systemctl restart nginx'\n"
        "ssh-core never prompts on a tty, so a missing NOPASSWD rule is a hard "
        "failure rather than a hang."
    )


def _sudoers_user() -> str:
    return "%sudo"


__all__ = [
    "AUTH_FAILED_HINTS",
    "DEFAULT_SUDO_USER",
    "NONINTERACTIVE_HINTS",
    "SUDO_AUTH_FAILED",
    "SUDO_REQUIRED",
    "SudoSpec",
    "classify",
    "parse_sudo",
    "probe_command",
    "remediation",
    "wrap",
]
