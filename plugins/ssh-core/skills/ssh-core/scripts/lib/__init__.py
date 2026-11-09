"""Shared helpers for ssh-core scripts.

Stable surface:
    json_result(success, exit_code, stdout, stderr, data) -> dict
    emit(result, as_json: bool, redactor=None) -> None  # prints to stdout
    msys_safe_env() -> dict                          # adds MSYS_NO_PATHCONV=1 on Windows
    alias_state_path(alias, kind) -> Path
    filter_ssh_noise(stderr) -> tuple[str, list[str]]  # split known-hosts noise out
    split_login_banner(stderr, command_output=...) -> tuple[str, list[str]]
        # peel sshd's pre-auth banner off stderr (SSHCORE-017)
    classify_failure(returncode, stderr, *, command_output="") -> str | None
        # None|network|auth|host_key|kex|remote_error; the disconnect wording a
        # device uses after a *successful* command only counts as `auth` when
        # the transcript shows no command output at all (BUG-008)
    device_output_text(text) -> str
        # strip ssh's own closure chatter — whole lines and the suffix glued to
        # the last command line — from a merged (-tt / pty) transcript
    strip_ssh_farewell(text) -> (str, int)
        # same filter, plus how many farewell lines went away
    greeting_prefix(text) -> str
        # the leading run of pre-login greeting lines of a merged transcript
        # (BUG-008b: the boundary the login driver records is a race)
    stream_capture(stream, cap=MAX_STREAM_BYTES) -> BoundedCapture
        # drains a *binary* stream into a bounded sink (see BoundedCapture.text)
    normalized_exit_code(returncode) -> int
        # signals become 128+N, Windows' 4294967295 is masked to 0-255
    validate_ssh_option(option) -> str | None        # None if safe, else reason
    ssh_config_path_problem(path) -> str | None      # None if usable, else reason
    write_text_lf(path, text) -> None                # newline="" without 3.10
    audit_dir() -> Path
    audit_log_path(alias) -> Path
    append_audit(alias, record, max_bytes=..., keep=...) -> Path | None
    audit_policy(max_bytes=None, keep=None) -> (bytes, generations)
    audit_enabled(default=False) -> bool             # SSH_CORE_AUDIT env switch
    merged_ssh_config(ssh_config) -> str | None
        # -F path that keeps the system crypto policy
    is_windows() / split_command() / detach_kwargs() / is_alive() / terminate()
        # platform abstraction (see lib/platform.py)

Companion modules (added in 1.1 to kill the per-script duplication that let
the same bug live in nine files at once):

    lib.exit_codes   one failure_class → exit_code table (SSH-19)
    lib.cli          argparse subclass that always emits JSON (SSH-18) + --version
    lib.redact       drops HostName/User/IdentityFile from envelopes (SSH-07)
    lib.configfile   locked atomic config writes, unique backups, restore (SSH-02..05)
    lib.session      pty-free interactive driver shared by ssh_session/ssh_execute
    lib.transfer     scp/rsync fallbacks when paramiko is absent (SSH-11/14)
    lib.retry        bounded retry with backoff for network/timeout only (DV-17)
    lib.probe        TCP/port liveness probes that do not need ssh (DV-15)
    lib.sudo         non-interactive privilege escalation wrapper (DV-14)

Everything else is an implementation detail and may move between versions.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager  # noqa: F401  (part of the public surface)
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from . import platform  # noqa: E402
from .platform import (  # noqa: E402, F401
    PTY_UNSUPPORTED_HINT,
    can_detach,
    detach_kwargs,
    is_alive,
    is_windows,
    split_command,
    supports_pty_session,
    terminate,
)


def json_result(
    success: bool,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": data or {},
    }


def _utf8_stdio() -> None:
    """Force UTF-8 on the interpreter's own stdout/stderr.

    Windows consoles default to the ANSI code page (cp936 on a Chinese
    install, cp1252 elsewhere), and ``json.dump(..., ensure_ascii=False)``
    then raises ``UnicodeEncodeError`` for characters that code page cannot
    represent - *after* half the envelope was already written, so the caller
    gets unparseable JSON plus a traceback. Reconfiguring to UTF-8 with
    ``errors="replace"`` keeps the envelope parseable everywhere.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def emit(
    result: dict[str, Any],
    as_json: bool,
    *,
    redactor: Any = None,
    streamed: bool = False,
) -> None:
    """Print ``result`` on stdout/stderr according to ``as_json``.

    ``redactor`` (a :class:`lib.redact.Redactor`) scrubs host identifiers from
    ``stderr``, ``stdout`` and the well-known ``data`` keys before anything is
    written (SSH-07). Pass it from every script that touches a real host.

    ``streamed`` says the caller already relayed the command's stdout/stderr to
    the terminal while it ran (SSHCORE-025). In that case the bytes must not be
    written a second time — the transcript has been seen, and re-printing it
    would duplicate every line *and* reorder stdout behind stderr.
    """
    if redactor is not None:
        result = _apply_redaction(result, redactor)
    _utf8_stdio()
    if as_json:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    # Human mode: the command's own bytes go out untouched (SSHCORE-025).
    # Adding a newline here used to turn `printf abc` into 4 bytes on a
    # pipeline, contradicting the "byte-clean" promise for successes.
    if not streamed:
        if result.get("stdout"):
            sys.stdout.write(result["stdout"])
        if result.get("stderr"):
            sys.stderr.write(result["stderr"])
    # SSHCORE-007: a failed command that produced no output must never be
    # silent — the caller (a human or an agent) can only see exit codes via
    # the shell, and "no output" reads as success. Emit a one-line trailer
    # on stderr for failures only; successes stay clean for pipelines.
    if not result.get("success"):
        code = result.get("exit_code")
        fclass = (result.get("data") or {}).get("failure_class")
        suffix = f", failure_class={fclass}" if fclass else ""
        if not result.get("stdout") and not result.get("stderr"):
            sys.stderr.write(f"ssh-core: command failed (exit_code={code}{suffix})\n")
        else:
            sys.stderr.write(f"ssh-core: exit_code={code}{suffix}\n")
        sys.stderr.flush()
    sys.stdout.flush()


def _apply_redaction(result: dict[str, Any], redactor: Any) -> dict[str, Any]:
    """Return a shallow copy of ``result`` with host identifiers redacted."""
    data = dict(result.get("data") or {})
    for key, value in list(data.items()):
        if isinstance(value, str) and key in {
            "raw_stderr", "raw_stdout", "stderr_tail", "detail", "message",
        }:
            data[key] = redactor.text(value)
        elif isinstance(value, list) and key in ("ssh_argv", "ssh_copy_id_argv"):
            data[key] = redactor.argv(value)
    redacted = dict(result)
    redacted["stdout"] = redactor.text(result.get("stdout") or "")
    redacted["stderr"] = redactor.text(result.get("stderr") or "")
    redacted["data"] = data
    return redacted


def msys_safe_env() -> dict[str, str]:
    """Return a copy of os.environ with MSYS_NO_PATHCONV=1 forced on.

    Harmless on non-Windows; required on Windows MSYS bash to stop POSIX
    paths from being rewritten into Windows drive paths in argv.
    """
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    return env


def alias_state_path(alias: str, kind: str = "ssh_daemon") -> Path:
    """Where to put per-alias state.

    Uses md5(alias) as the basename so casual `ls /tmp` does not leak host
    names. The directory mode is 0o700; callers should set 0o600 on the file.
    """
    base = Path(tempfile.gettempdir()) / kind
    base.mkdir(mode=0o700, exist_ok=True)
    digest = hashlib.md5(alias.encode("utf-8")).hexdigest()
    return base / f"{digest}.json"


# Lines emitted by the ssh client itself that we want to keep out of the
# top-level `stderr` field. Add patterns sparingly — when in doubt, leave a
# line in real_stderr (fail-open). See ADR-001 D4 for the policy.
SSH_NOISE_PATTERNS = (
    re.compile(r"^Warning: Permanently added .* to the list of known hosts\.$"),
)


# Regex sets used by classify_failure. All are deliberately scoped to
# returncode == 255 by the classifier so they cannot false-positive on
# remote commands that emit "Permission denied" in their own stderr.
# (255 is ssh's own conventional exit code for client-side failure.)
SSH_CLIENT_FAILURE_EXIT_CODE = 255

NETWORK_PATTERN = re.compile(
    r"Could not resolve hostname|Network is unreachable|"
    r"No route to host|Connection refused|Connection timed out|"
    r"Connection reset by peer|Broken pipe|Operation timed out|"
    r"ssh_exchange_identification",
    re.IGNORECASE,
)
#: A name ssh could not resolve (FA-22). Split out of NETWORK_PATTERN because
#: "Could not resolve hostname no-such-host-xyz" is almost always a typo in
#: ``~/.ssh/config``, and reporting it as 102 (network) sends the operator to
#: DNS/firewall instead of to the alias list. A dotted / colon-y / all-digit
#: name is treated as a real address, so a genuine DNS outage still says
#: ``network``.
UNRESOLVED_HOST_RE = re.compile(
    r"Could not resolve hostname ([^\s:]+)", re.IGNORECASE
)
#: Unambiguous credential rejections. These mean "auth" whenever ssh exits 255,
#: whatever else the transcript contains.
AUTH_PATTERN = re.compile(
    r"Permission denied \(|"
    r"Permission denied, please try again|"
    r"Too many authentication failures|"
    r"Authentication failed|"
    r"password authentication failed|"
    r"No more authentication methods",
    re.IGNORECASE,
)
#: Default prompt heuristic, shared by ``lib.session`` (which re-exports it as
#: ``DEFAULT_PROMPT_RE``) so "did the device re-print its prompt?" has exactly
#: one definition: ``<Switch>``, ``[Switch-GigabitEthernet0/0/1]``,
#: ``admin@host:~$``, ``#``, ``>`` at the end of a line.
PROMPT_LINE_RE = re.compile(
    r"(?:<[^<>\r\n]{1,80}>|\[[^\[\]\r\n]{1,120}\]|[\w.@:/~-]{0,64}[>$#%])\s*$"
)

#: Device-side credential rejections (BUG-008). A VRP / Comware / IOS CLI
#: answers a bad password on its **own** login prompt ("Error: Local
#: authentication is rejected.") instead of sshd's ``Permission denied``, and it
#: re-displays the banner and prompt afterwards. Two consequences:
#:
#: * "a prompt came back" is not proof that a command ran
#:   (``lib.session.prompt_returned`` vetoes on this pattern too);
#: * a wrong password on a device must still report ``failure_class: "auth"``
#:   / exit 100, which is an explicit acceptance criterion of the issue.
#:
#: Deliberately tiny and device-specific: a broad word like "login failed" would
#: also match the *output of a remote command* (``grep login /var/log/auth``
#: followed by exit 255), and turning that into ``auth`` is the exact
#: wrong-direction conclusion this issue is about.
DEVICE_AUTH_REJECT_PATTERN = re.compile(
    r"(?i)(authentication is rejected|bad login name or password|"
    r"authentication rejected|invalid username or password)"
)

#: The *ambiguous* disconnect wording (BUG-008). Huawei VRP / Comware close the
#: channel themselves once the command has run, and OpenSSH reports that with
#: the same sentence sshd uses when it rejects a client mid-handshake:
#:
#:     Received disconnect from 1.2.3.4 port 22:2: The connection is closed by
#:     SSH server
#:
#: Sending an operator to "check the password" for a command that already
#: produced its output is worse than saying "remote_error", so this pattern only
#: classifies as ``auth`` when the transcript shows no command output — no
#: result and, crucially, no prompt echo (a real rejection never echoes a prompt
#: first). See :func:`classify_failure`'s ``command_output`` argument.
AUTH_DISCONNECT_PATTERN = re.compile(
    r"Received disconnect .*The connection is closed by SSH server",
    re.IGNORECASE,
)

#: ssh's own closure chatter on a merged (``-tt`` / pty) stream. Everything
#: matching this is transport noise, not something the device command printed:
#: every network-device session ends with one or two of these lines, so "did the
#: command produce output?" cannot be answered on the raw text without removing
#: them first (BUG-008).
#:
#: ``client_loop: send disconnect: Connection reset`` (BUG-008b) is the shape a
#: VRP box produces when it drops the TCP connection instead of closing the SSH
#: channel politely. It used to be glued to the last command line
#: (``...on line is 0.client_loop: ...``), so the line-shaped filter could not
#: see it; :func:`strip_ssh_farewell` cuts that suffix off before this regex
#: runs. The reset wording carries no verdict either way — it is the *client*
#: reporting a dead socket, and the verdict comes from the transcript.
TRANSPORT_NOISE_RE = re.compile(
    r"(?i)^(?:"
    r"Connection to .* closed\.?|Shared connection to .* closed\.?|"
    r"Received disconnect from .*|Disconnected from .*|"
    r"Connection closed by .*|client_loop: send disconnect: .*|"
    r"Warning: Permanently added .*|"
    r"ssh: .*|ssh_exchange_identification: .*"
    r")\s*$"
)

#: ssh's farewell as a *suffix* of a line (BUG-008b). On a merged ``-tt`` stream
#: the last line is ``<command output>Received disconnect ...`` or
#: ``...is 0.client_loop: send disconnect: Connection reset`` — ssh never added
#: the newline the device did not send, so "starts with a farewell" is false and
#: a line-anchored filter keeps the whole thing as command output.
#:
#: Only ssh's own wording is listed. A device line that legitimately contains
#: one of these phrases with output *after* it stays untouched (the pattern is
#: anchored at the end of the line).
SSH_FAREWELL_SUFFIX_RE = re.compile(
    r"(?i)(?:"
    r"Connection to \S+ closed\.|Shared connection to \S+ closed\.|"
    r"client_loop: send disconnect: .*|"
    r"Received disconnect from \S+ port \S+:\d+(?::\s.*)?|"
    r"Disconnected from (?:[\w.:-]+|\[[^\]]+\]) port \d+|"
    r"Connection closed by (?:[\w.:-]+|\[[^\]]+\]) port \d+|"
    r"Connection reset by (?:[\w.:-]+|\[[^\]]+\]) port \d+"
    r")\s*$"
)


#: Lines that belong to a **pre-login greeting** and never to command output
#: (BUG-008b). On a merged ``-tt`` stream the greeting and the answer arrive in
#: the same buffer, and the login driver's snapshot boundary is a race: on a fast
#: device the answer is already there when the first poll happens, so "everything
#: recorded before authentication finished" can include the command's result.
#: Cutting that whole prefix deleted the result and the prompt with it, and the
#: leftovers classified as ``auth``.
#:
#: These markers are the wording devices actually greet with: Huawei VRP's VTY
#: notice and login time, Comware/IOS's "Press ENTER/RETURN to get started",
#: Linux's last-login line and MOTD greeting. The list is deliberately about the
#: *greeting*, never about results — a device that prints none of these keeps its
#: greeting in ``stdout`` (a nuisance) instead of risking a deleted answer.
#:
#: ``Copyright (C) ...`` is deliberately **not** listed even though many login
#: banners carry it: ``display version`` prints the same line, so matching it
#: would make the cut eat a line of the command's own answer.
LOGIN_GREETING_LINE_RE = re.compile(
    r"(?i)(?:"
    r"\bVTY users\b|current login time|"
    r"press (?:ENTER|RETURN) to (?:get started|continue)|"
    r"unauthorized access|authorized users only|"
    r"last (?:successful )?login\b|user last login|"
    r"welcome to\b|"
    r"^\s*(?:username|login|user name)\s*:\s*$"
    r")"
)


def greeting_prefix(text: str) -> str:
    """Return the leading run of greeting lines of ``text``, endings intact.

    The run stops at the first line that is not a known greeting marker, so a
    snapshot that ran into the command's answer yields the greeting only
    (BUG-008b). An empty result means "this transcript does not start with a
    greeting we recognise", and callers must then cut nothing.
    """
    if not text:
        return ""
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        if not LOGIN_GREETING_LINE_RE.search(line):
            break
        kept.append(line)
    return "".join(kept)


def strip_ssh_farewell(text: str) -> tuple[str, int]:
    """Drop ssh's trailing closure chatter, including the glued form (BUG-008b).

    Returns ``(text_without_farewell, removed_line_count)``.

    Two shapes exist on a merged (``-tt`` / pty) stream and only the first was
    handled before BUG-008b:

    * whole lines — ``Received disconnect from <host> port 22:2: ...`` then
      ``Disconnected from <host> port 22``. Every network-device session ends
      with one or both, and they are ssh talking, not the command;
    * a **suffix glued to the command's last line** — a VRP/Comware box answers
      ``display clock`` without a trailing newline, so the transcript reads
      ``...on line is 0.Received disconnect from ...`` or
      ``...on line is 0.client_loop: send disconnect: Connection reset``.

    The second shape is what made "did the command print anything?" answer
    "nothing" on the runs where the device dropped the TCP connection: the raw
    line did not *start* with a farewell, so it survived the line filter and
    ``stdout`` kept ssh's own trailer. Cutting the suffix first is what makes
    the device verdict independent of *how* the box hung up.

    The count is returned so a caller can report how much transport noise was
    removed instead of silently shrinking the transcript.
    """
    if not text:
        return "", 0
    removed = 0
    kept: list[str] = []
    for line in text.split("\n"):
        body = line.rstrip("\r")
        if not body.strip():
            kept.append(line)
            continue
        if TRANSPORT_NOISE_RE.match(body.strip()):
            removed += 1
            continue
        trimmed = SSH_FAREWELL_SUFFIX_RE.sub("", body)
        if trimmed != body:
            removed += 1
            if trimmed.strip():
                kept.append(trimmed)
            continue
        kept.append(line)
    return "\n".join(kept), removed


def device_output_text(text: str) -> str:
    """The part of a merged transcript the *device* produced (BUG-008).

    ``ssh -tt`` merges the device CLI's output with ssh's own diagnostics, so
    "did the command print anything?" cannot be answered from the raw text:
    every device session ends with ``Received disconnect ...`` /
    ``Disconnected from ...``, which is ssh talking, not the command.

    Removing those lines is what lets :func:`classify_failure` keep calling a
    bare disconnect an ``auth`` rejection while a transcript that ends with a
    prompt and a result is not one. This is :func:`strip_ssh_farewell` without
    the count, so the line filter and the glued-suffix filter can never drift
    apart; anything unrecognised stays (fail-open, the same rule as
    :func:`filter_ssh_noise`).
    """
    return strip_ssh_farewell(text)[0]


def credentials_rejected(text: str) -> bool:
    """True when a merged transcript carries an unambiguous credential failure.

    Two sources, both narrow on purpose:

    * ssh's own wording (``Permission denied (``, ``Authentication failed`` ...),
      which is what a Linux host or a device with sshd-style auth prints;
    * the device CLI's own wording (``Error: Local authentication is rejected.``).

    This is the veto both success tests consult, so a rejected login can never
    be reported as a completed command just because the device re-printed its
    banner and prompt afterwards (BUG-008 acceptance criterion: a wrong password
    is still ``failure_class: "auth"`` / exit 100).
    """
    if not text:
        return False
    device_text = device_output_text(text)
    return bool(
        AUTH_PATTERN.search(device_text)
        or DEVICE_AUTH_REJECT_PATTERN.search(device_text)
    )


HOST_KEY_PATTERN = re.compile(
    r"Host key verification failed|"
    r"REMOTE HOST IDENTIFICATION HAS CHANGED|"
    r"no matching host key type found|"
    r"Host key for .* has changed",
    re.IGNORECASE,
)
KEX_PATTERN = re.compile(
    r"no matching key exchange method found|"
    r"no matching cipher found|"
    r"no matching MAC found|"
    r"key exchange failed|"
    r"Unable to negotiate with .*: no matching",
    re.IGNORECASE,
)

# Metadata comment keys understood by ssh_config_manager (SSHCORE-013). Kept
# here so ssh_execute / ssh_session can read the same metadata without
# importing the config-manager script.
SSH_CONFIG_METADATA_RE = re.compile(
    r"^\s*#\s*(description|environment|tags|location|auth|device-type)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)


#: ssh's own diagnostics. Anything *before* the first of these in stderr is
#: the server's pre-auth banner, relayed verbatim by the client (SSHCORE-017).
SSH_CLIENT_DIAGNOSTIC_RE = re.compile(
    r"^(?:\S+@[^\s:]+: (?:Permission denied|Connection closed|Connection reset)|"
    r"ssh: |ssh_exchange_identification|"
    r"(?:Unable to negotiate|No \S+ host key is known|Host key verification failed|"
    r"REMOTE HOST IDENTIFICATION HAS CHANGED|Warning: )|"
    r"banner exchange: |Received disconnect from |Client_loop: )"
)


def split_login_banner(stderr: str, *, command_output: str = "") -> tuple[str, list[str]]:
    """Peel sshd's pre-auth banner off ``stderr`` (SSHCORE-017).

    OpenSSH relays the server's pre-auth banner (``sshd_config`` ``Banner``) to
    the *client's* stderr, on the same channel as the remote command's own
    stderr. On any host that greets with "Authorized users only..." every
    successful call reported ``stderr != ""``, which breaks any consumer whose
    rule is "non-empty stderr ⇒ alert".

    Why the banner is removed here instead of with ``-o LogLevel=ERROR``: that
    option also silences diagnostics ``failure_class`` is derived from.
    Measured against this lab's hardware:

    ==============================  =============  ==============
    message                         OpenSSH 9.5p1  OpenSSH 8.2p1
                                    (Windows)      (Kylin 3)
    ==============================  =============  ==============
    ``Permission denied (...)``     kept           kept
    ``Host key verification...``    kept           kept
    ``Could not resolve hostname``  kept           kept
    ``Connection refused``          **lost**       kept
    ``Unable to negotiate ...``     **lost**       **lost**
    ==============================  =============  ==============

    Losing the last two would push ``network``/``kex`` back into
    ``remote_error`` — i.e. re-open SSHCORE-003 — so the client keeps its
    default level and the banner is removed from the captured text instead.

    The banner's shape is what we key on: OpenSSH writes a blank line first and
    then the banner, so a stream that does *not* start with a blank line is not
    a banner. With that leading blank line present, the first line is dropped
    unless it is itself an ssh diagnostic. A multi-line banner followed by the
    command's own stderr keeps everything after its first line, because eating
    a real error is far worse than echoing banner text.

    Returns ``(real_stderr, banner_lines)``.
    """
    if not stderr or not stderr.strip():
        return stderr, []
    match = re.match(r"^(?:[ \t]*\r?\n)+", stderr)
    if match is None:
        # Banner hosts always start with a blank line; anything else is the
        # command's own stderr (or a diagnostic) and must be left alone.
        return stderr, []
    body = stderr[match.end():]
    if not body:
        return "", [line.strip() for line in stderr.splitlines() if line.strip()]

    first, _newline, rest = body.partition("\n")
    stripped = first.strip()
    if not stripped:
        return stderr, []
    if SSH_CLIENT_DIAGNOSTIC_RE.match(stripped):
        # Not a banner — ssh is reporting a problem, keep every byte.
        return stderr, []
    if rest.strip() and command_output and stripped in command_output:
        # The "banner" is the command's own first line.
        return stderr, []
    # Exactly one line goes: a multi-line banner would then leave its tail
    # behind, which is the deliberate trade-off — one leaked banner line is a
    # nuisance, one eaten error line is a wrong answer.
    if rest and stderr.endswith("\n") and not rest.endswith("\n"):
        rest += "\n"
    return rest, [stripped]


def filter_ssh_noise(stderr: str) -> tuple[str, list[str]]:
    """Split captured ssh stderr into (real_stderr, noise_lines).

    Fail-open: any line that does NOT match a pattern in SSH_NOISE_PATTERNS
    stays in real_stderr. The library of patterns is intentionally small;
    missing a real error is worse than emitting an extra ssh warning to
    the user.

    The server's login banner is handled structurally by
    :func:`split_login_banner` (SSHCORE-017), which the exec route calls
    first; this function only drops line-shaped warnings that are always
    transport noise (``Warning: Permanently added ...``).

    Preserves a trailing newline iff the input had one and at least one
    real line survived (fixes the spike's dropped-newline behavior).
    """
    real: list[str] = []
    noise: list[str] = []
    for line in stderr.splitlines():
        if any(p.match(line) for p in SSH_NOISE_PATTERNS):
            noise.append(line)
        else:
            real.append(line)
    out = "\n".join(real)
    if real and stderr.endswith("\n"):
        out += "\n"
    return out, noise


def _unresolved_is_alias(stderr: str) -> bool:
    """True when an unresolved name looks like an alias, not an address."""
    match = UNRESOLVED_HOST_RE.search(stderr)
    if match is None:
        return False
    name = match.group(1).strip().strip("'\"")
    return bool(name) and "." not in name and ":" not in name and not name.isdigit()


def classify_failure(
    returncode: int,
    stderr: str,
    *,
    command_output: str = "",
) -> str | None:
    """Map (returncode, stderr) to a failure class.

    Returns one of ``None`` / ``network`` / ``alias_not_found`` / ``auth`` /
    ``host_key`` / ``kex`` / ``remote_error``. ``None`` means success
    (`returncode == 0`).

    For non-zero codes, the ssh-client regexes only apply when
    ``returncode == 255`` (ssh's own exit code for client-side problems);
    everything else is bucketed as ``remote_error``.

    Classification order (SSHCORE-003) is host_key → kex → auth → alias →
    network: a host-key change or algorithm-negotiation failure mentions
    auth-shaped words, but the remediation (known_hosts / KexAlgorithms) is
    entirely different from fixing credentials.

    ``command_output`` (BUG-008) is whatever the *command* produced, when the
    caller has it (the interactive/network routes work on one merged stream, so
    the transcript doubles as both). It gates the one ambiguous pattern: a
    device that says ``Received disconnect ... The connection is closed by SSH
    server`` after it answered the command is closing the session normally, not
    rejecting credentials, so with output present that wording classifies as
    ``remote_error``. Callers that pass nothing keep the old, conservative
    behaviour (see :data:`AUTH_DISCONNECT_PATTERN`).

    Note: this function does NOT return "timeout". The caller is the only
    code path that knows whether `subprocess.TimeoutExpired` was raised,
    so timeout classification belongs there, not here.
    """
    if returncode == 0:
        return None
    if returncode == SSH_CLIENT_FAILURE_EXIT_CODE:
        if HOST_KEY_PATTERN.search(stderr):
            return "host_key"
        if KEX_PATTERN.search(stderr):
            return "kex"
        if AUTH_PATTERN.search(stderr):
            return "auth"
        if DEVICE_AUTH_REJECT_PATTERN.search(stderr):
            # A device CLI rejects a bad password on its own login prompt
            # ("Error: Local authentication is rejected."), which ssh reports
            # with exit 255 like any other client failure (BUG-008).
            return "auth"
        if AUTH_DISCONNECT_PATTERN.search(stderr):
            # Only a real rejection when nothing was produced first: a device
            # that echoes a prompt and then hangs up executed the command.
            if not command_output.strip():
                return "auth"
            return "remote_error"
        if _unresolved_is_alias(stderr):
            return "alias_not_found"
        if NETWORK_PATTERN.search(stderr):
            return "network"
    return "remote_error"


# --- Bounded output capture (SSHCORE-005 / 015 / 019 / 021 / 022) -----------

# Documented cap in SKILL.md + references/safety.md. Per stream, in *bytes* of
# UTF-8 text. Keep the constant here so every route (exec / session) shares it.
MAX_STREAM_BYTES = 10 * 1024 * 1024
TRUNCATION_MARKER = "\n... [truncated, {n} bytes elided]\n"

_READ_CHUNK = 65536

# Popen/run kwargs for every child whose stdout/stderr we read as text.
#
# ``text=True`` alone decodes with the *locale* encoding - cp936 on a Chinese
# Windows, ANSI_X3.4-1968 (ASCII) under ``LANG=C`` on a Kylin/RHEL host - while
# ssh and every POSIX tool emit UTF-8. A mismatch raises UnicodeDecodeError
# inside the reader thread, the output silently vanishes, and the call still
# looks successful. Pin UTF-8 and turn undecodable bytes into U+FFFD instead of
# dropping the stream.
#
# Note: ``subprocess`` does not accept ``newline=``, and text mode always
# applies universal-newline translation (``\r\n`` / lone ``\r`` → ``\n``).
# Any route that must preserve the remote bytes **verbatim** therefore reads a
# *binary* pipe and decodes once at the end — see ``ssh_execute._capture_process``
# and ``BoundedCapture`` (SSHCORE-015/019). The text-mode callers below only
# parse diagnostics, where newline folding is harmless.
CHILD_TEXT_KWARGS: dict[str, str] = {
    "encoding": "utf-8",
    "errors": "replace",
}

#: Popen/run kwargs for children whose bytes we keep as bytes (exec route).
#: ``newline=""`` is meaningless without ``text=True``, so the transport uses
#: ``text=False`` and the envelope decodes once, at the very end.
CHILD_BYTES_KWARGS: dict[str, Any] = {"encoding": None}  # documentation only


def decode_utf8(data: bytes) -> tuple[str, int]:
    """Decode ``data`` as UTF-8, replacing invalid sequences.

    Returns ``(text, replaced)`` where ``replaced`` counts the U+FFFD
    characters that were substituted for undecodable bytes — the number the
    envelope reports as ``decode_replaced`` so a caller can tell "no output"
    from "output that could not be represented" (SSHCORE-015).
    """
    if not data:
        return "", 0
    text = data.decode("utf-8", errors="replace")
    replaced = text.count("\ufffd") - data.count(
        "\ufffd".encode("utf-8")
    )
    return text, max(0, replaced)


class BoundedCapture:
    """Accumulates a **byte** stream up to ``cap`` bytes, discarding the rest.

    Used by the exec paths: the underlying process keeps running to
    completion (no early kill — SIGPIPE would change remote behavior), but
    bytes past the cap are dropped and counted so the envelope can say
    exactly how much was elided.

    Since 1.4 the buffer holds ``bytes``, not ``str`` (SSHCORE-015/019/022):

    * no decode happens on the read path, so an undecodable byte can never
      abort the reader thread or truncate the envelope to nothing;
    * the cap is enforced on byte boundaries, so ``kept + elided == received``
      exactly, whatever the encoding of the payload;
    * ``\r\n`` / lone ``\r`` survive because nothing translates newlines.

    Decoding happens once, lazily, in :meth:`text`.
    """

    def __init__(self, cap: int = MAX_STREAM_BYTES) -> None:
        self.cap = cap
        self._parts: list[bytes] = []
        self._kept = 0
        self._elided = 0
        self._decode_errors = 0
        self._lock = threading.Lock()

    def note_decode_error(self) -> None:
        """Record that a read chunk could not be decoded."""
        with self._lock:
            self._decode_errors += 1

    def note_elided(self, count: int) -> None:
        """Record bytes a *producer* already dropped before us.

        The interactive session buffer keeps the tail and counts what it threw
        away; the exec pipeline has to report that loss through the same
        ``bytes_elided`` / ``truncated`` fields as its own cap, or a 10 MB
        transcript arrives looking complete (SSHCORE-022).
        """
        if count > 0:
            with self._lock:
                self._elided += int(count)

    @property
    def decode_errors(self) -> int:
        """How many read chunks failed to decode (0 == clean run)."""
        return self._decode_errors

    def feed(self, chunk: bytes | str) -> None:
        """Append one chunk. ``str`` is tolerated (UTF-8 encoded) for callers
        that already decoded, but bytes are the native currency."""
        if not chunk:
            return
        data = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else chunk
        size = len(data)
        with self._lock:
            room = self.cap - self._kept
            if room <= 0:
                self._elided += size
                return
            if size <= room:
                self._parts.append(data)
                self._kept += size
                return
            self._parts.append(data[:room])
            self._kept += room
            self._elided += size - room

    @property
    def truncated(self) -> bool:
        return self._elided > 0

    @property
    def bytes_elided(self) -> int:
        return self._elided

    @property
    def bytes_kept(self) -> int:
        """Bytes actually retained (``min(received, cap)``)."""
        return self._kept

    @property
    def bytes_received(self) -> int:
        """Bytes read from the stream before capping."""
        return self._kept + self._elided

    def raw(self) -> bytes:
        """Kept prefix as bytes, without the truncation marker."""
        with self._lock:
            return b"".join(self._parts)

    def text(self) -> str:
        """Kept prefix (at most ``cap`` bytes) plus a truncation marker.

        The marker is appended *outside* the cap, matching the wording in
        ``references/safety.md``: "we truncate and append ... [truncated,
        N bytes elided]". ``bytes_elided`` is exactly the number of bytes
        dropped, so ``bytes_kept + bytes_elided == bytes_received`` and
        ``len(text.encode()) == cap + len(marker)`` when truncated.
        """
        body, _replaced = decode_utf8(self.raw())
        if not self._elided:
            return body
        return body + TRUNCATION_MARKER.format(n=self._elided)

    def decoded(self) -> tuple[str, int]:
        """``(text_with_marker, replaced_characters)`` in one pass."""
        body, replaced = decode_utf8(self.raw())
        if not self._elided:
            return body, replaced
        return body + TRUNCATION_MARKER.format(n=self._elided), replaced


def drain_stream(
    stream: IO[bytes] | IO[str] | None,
    sink: BoundedCapture,
    tee: Any = None,
) -> None:
    """Read a stream block-by-block into ``sink`` until EOF.

    Reads whatever the pipe yields: bytes on a binary pipe, ``str`` on a text
    one (``sink.feed`` accepts both).

    ``tee`` — an optional binary stream (``sys.stdout.buffer``) that receives
    every chunk as it arrives, so human mode can show output *while* the
    command runs instead of after it finished (SSHCORE-025). A tee that fails
    (closed pipe, ``BrokenPipeError``) is dropped silently: the caller's
    ``| head`` must not turn into a remote failure.

    ``UnicodeDecodeError`` is a ``ValueError`` subclass, so a decode failure
    used to be swallowed here as if it were a closed pipe: the reader thread
    ended, ``stdout`` came back empty, and the envelope still said
    ``success: true`` (SSHCORE-015). Callers now spawn with
    :data:`CHILD_TEXT_KWARGS` — and the exec route with a *binary* pipe — so
    this handler is the last-resort net, and it records the loss on the sink
    instead of hiding it.
    """
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            sink.feed(chunk)
            if tee is not None:
                try:
                    tee.write(chunk)
                    tee.flush()
                except (OSError, ValueError, AttributeError):
                    tee = None
    except UnicodeDecodeError:
        sink.note_decode_error()
    except (OSError, ValueError):
        # A closed pipe at process exit is normal; EOF is EOF.
        return
    finally:
        try:
            stream.close()
        except OSError:
            pass


def stream_capture(
    stream: IO[bytes] | IO[str] | None, cap: int = MAX_STREAM_BYTES
) -> BoundedCapture:
    """Convenience: drain ``stream`` into a fresh :class:`BoundedCapture`."""
    sink = BoundedCapture(cap)
    drain_stream(stream, sink)
    return sink


# --- exit-code normalisation (SSHCORE-023) -----------------------------------

_SIGNAL_EXIT_BASE = 128


def normalized_exit_code(returncode: int | None) -> tuple[int, int | None]:
    """Map a raw ``Popen`` returncode onto the shell's ``0-255`` convention.

    ``subprocess`` reports "killed by signal N" as ``-N``; every shell (and
    every CI/ops check that does ``if rc > 128``) uses ``128 + N`` instead, so
    a raw negative leaked a number the caller could not interpret. Windows
    reports the same event as an unsigned DWORD (``4294967295``), which used
    to appear verbatim in the envelope.

    Returns ``(exit_code, terminated_by_signal)``. ``terminated_by_signal`` is
    the positive signal number, or ``None`` for a normal exit — it is what the
    envelope reports as ``data.terminated_by_signal``.
    """
    if returncode is None:
        return 1, None
    if returncode < 0:
        return _SIGNAL_EXIT_BASE + (-returncode), -returncode
    if returncode > 255:
        masked = returncode & 0xFF
        return masked, None
    return returncode, None


# --- ssh -o option validation (SSHCORE-009) ---------------------------------

# Directives that execute local commands would turn `-o Foo=bar` into code
# execution with the caller's privileges. OpenSSH itself warns about
# ProxyCommand; we refuse the whole family, plus anything that rewrites the
# connection in a way we cannot audit.
_REFUSED_SSH_OPTIONS = frozenset(
    {
        "proxycommand",
        "localcommand",
        "permitlocalcommand",
        "match",
        "hostname",
        "knownhostscommand",
        "remotecommand",
        "identityagent",
    }
)
# `ssh -o` accepts arbitrary directives; keys are case-insensitive.
_SSH_OPTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*=[^\r\n]*$")


def validate_ssh_option(option: str) -> str | None:
    """Return None when ``option`` is a safe ``KEY=VALUE``; else a reason.

    Rules (SSHCORE-009):
    * must be ``KEY=VALUE`` on a single line (no CR/LF — argv injection);
    * KEY must be alphanumeric and start with a letter;
    * refuses directives that spawn local commands (ProxyCommand,
      LocalCommand, KnownHostsCommand, RemoteCommand, ...) and `Match`,
      which can silently re-scope the whole config.
    """
    if not _SSH_OPTION_RE.match(option):
        return (
            f"ssh option {option!r} must be a single-line KEY=VALUE "
            "with no spaces before '='"
        )
    key = option.split("=", 1)[0].strip().lower()
    if key in _REFUSED_SSH_OPTIONS:
        return (
            f"ssh option {key!r} is refused: it can execute local commands "
            "or re-scope the connection"
        )
    return None


# --- Encodings --------------------------------------------------------------


def read_text_auto(path: str | os.PathLike[str]) -> str:
    """Read a text file without assuming the caller's encoding.

    Command lists and configs are routinely authored by PowerShell 5.1
    (``Out-File`` → UTF-16 or cp936), Notepad (BOM) or a POSIX editor (UTF-8).
    Order: UTF-16 with BOM, UTF-8 with/without BOM, then the locale's ANSI code
    page with ``replace`` so a mixed file still yields something usable instead
    of a traceback (a GBK command file used to crash
    ``ssh_execute --commands-file``).

    Raises ``OSError`` (including ``FileNotFoundError``) when the file cannot be
    read at all — callers turn that into an ``invalid_arguments`` envelope
    (FA-03) instead of letting a traceback escape.
    """
    p = Path(path).expanduser()
    raw = p.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        with contextlib.suppress(UnicodeDecodeError):
            return raw.decode("utf-16")
    with contextlib.suppress(UnicodeDecodeError):
        return raw.decode("utf-8-sig")
    import locale

    with contextlib.suppress(LookupError):
        return raw.decode(locale.getpreferredencoding(False), errors="replace")
    return raw.decode("latin-1", errors="replace")


def write_text_lf(
    path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8"
) -> None:
    r"""Write ``text`` with ``\n`` line endings, on any supported Python.

    ``Path.write_text(..., newline="")`` is **3.10+** only (FA-08): on a
    Kylin 3 / CentOS 7 host running 3.8 it raises ``TypeError: write_text()
    got an unexpected keyword argument 'newline'`` — silently breaking every
    ``~/.ssh/config`` write while reads kept working. Opening the file
    explicitly has the same effect and works from 3.6 upwards.
    """
    with open(path, "w", encoding=encoding, newline="") as handle:
        handle.write(text)


# --- Audit trail (SSHCORE-014, SSH-25) --------------------------------------

AUDIT_DIR_NAME = "ssh-core"
AUDIT_DIR_MODE = 0o700
AUDIT_FILE_MODE = 0o600
#: Rotate a per-alias JSONL file once it passes this size (bytes).
#: Override per run with ``SSH_CORE_AUDIT_MAX_BYTES`` (DV-13).
AUDIT_MAX_BYTES = 10 * 1024 * 1024
#: How many rotated generations to keep (``.1`` newest … ``.N`` oldest).
#: Override per run with ``SSH_CORE_AUDIT_KEEP`` (DV-13).
AUDIT_KEEP = 3
#: ``SSH_CORE_AUDIT=off`` disables auditing globally; ``=on`` audits every
#: script even when the caller did not pass ``--audit-log``.
AUDIT_ENV = "SSH_CORE_AUDIT"
AUDIT_MAX_BYTES_ENV = "SSH_CORE_AUDIT_MAX_BYTES"
AUDIT_KEEP_ENV = "SSH_CORE_AUDIT_KEEP"


def audit_dir() -> Path:
    """The directory that holds every per-alias JSONL audit file.

    ``$TMPDIR/ssh-core`` — created with mode 0o700 on first use. Kept in one
    place so ``scripts/audit.py`` (summarize / prune) and
    :func:`audit_log_path` can never disagree about where the records live.
    """
    base = Path(tempfile.gettempdir()) / AUDIT_DIR_NAME
    # parents=True: TMPDIR itself may not exist yet (e.g. a test sandbox).
    base.mkdir(mode=AUDIT_DIR_MODE, parents=True, exist_ok=True)
    try:
        base.chmod(AUDIT_DIR_MODE)
    except OSError:
        pass
    return base


def audit_log_path(alias: str) -> Path:
    """Per-alias JSONL audit file: ``$TMPDIR/ssh-core/<md5(alias)>.jsonl``.

    Directory 0o700 / file 0o600 — the content may include device config
    and must not be world-readable. Mirrors :func:`alias_state_path`.
    """
    digest = hashlib.md5(alias.encode("utf-8")).hexdigest()
    return audit_dir() / f"{digest}.jsonl"


def audit_policy(
    max_bytes: int | None = None, keep: int | None = None
) -> tuple[int, int]:
    """Resolve the rotation policy: explicit arg > env override > default.

    ``SSH_CORE_AUDIT_MAX_BYTES`` and ``SSH_CORE_AUDIT_KEEP`` let an operator
    size the trail per host without editing code; a malformed value is
    ignored rather than raising (auditing must never break work).
    """
    def _int_env(name: str, fallback: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return fallback
        try:
            return max(1, int(raw))
        except ValueError:
            return fallback

    resolved_bytes = (
        max_bytes
        if max_bytes is not None
        else _int_env(AUDIT_MAX_BYTES_ENV, AUDIT_MAX_BYTES)
    )
    resolved_keep = (
        keep if keep is not None else _int_env(AUDIT_KEEP_ENV, AUDIT_KEEP)
    )
    return resolved_bytes, resolved_keep


def audit_enabled(explicit: bool | None = None, *, default: bool = False) -> bool:
    """Decide whether this invocation writes audit records.

    Precedence: explicit flag (``--audit-log`` / ``--no-audit``) > environment
    (``SSH_CORE_AUDIT=on|off``) > per-script default.
    """
    if explicit is not None:
        return explicit
    env = os.environ.get(AUDIT_ENV, "").strip().lower()
    if env in ("1", "on", "true", "yes"):
        return True
    if env in ("0", "off", "false", "no"):
        return False
    return default


def _rotate_audit(path: Path, *, max_bytes: int, keep: int) -> None:
    """Rename ``path`` → ``.1`` (shifting older generations) when too big.

    Renames are retried briefly: on Windows a reader (``audit.py``, a tail, an
    antivirus scanner) holding the file open makes ``os.replace`` fail with
    ``PermissionError``, and swallowing that silently loses generations — the
    file keeps growing while the trail claims to be rotated. The retry is
    bounded and still never raises, because auditing must not break real work.
    """
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    def _replace(source: Path, target: Path) -> bool:
        for attempt in range(3):
            try:
                source.replace(target)
                return True
            except OSError:
                if attempt == 2:
                    return False
                time.sleep(0.05)
        return False

    for index in range(keep, 0, -1):
        current = path.with_name(f"{path.name}.{index}")
        if index == keep:
            with contextlib.suppress(OSError):
                current.unlink()
            continue
        if current.exists():
            _replace(current, path.with_name(f"{path.name}.{index + 1}"))
    _replace(path, path.with_name(f"{path.name}.1"))


def append_audit(
    alias: str,
    record: dict[str, Any],
    *,
    max_bytes: int | None = None,
    keep: int | None = None,
) -> Path | None:
    """Append one JSONL record. Never raises — auditing must not break work.

    Callers are responsible for omitting credentials; this helper only
    stamps ``ts`` when absent, enforces the file mode, and rotates the file
    once it grows past ``max_bytes`` (keeping ``keep`` generations). Both
    default to :data:`AUDIT_MAX_BYTES` / :data:`AUDIT_KEEP`, overridable with
    ``SSH_CORE_AUDIT_MAX_BYTES`` / ``SSH_CORE_AUDIT_KEEP`` (DV-13).
    """
    try:
        resolved_bytes, resolved_keep = audit_policy(max_bytes, keep)
        path = audit_log_path(alias)
        _rotate_audit(path, max_bytes=resolved_bytes, keep=resolved_keep)
        payload = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        # O_APPEND keeps concurrent writers from interleaving partial lines
        # for small writes; create with 0o600 from the start.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, AUDIT_FILE_MODE)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            path.chmod(AUDIT_FILE_MODE)
        except OSError:
            pass
        return path
    except OSError:
        return None


def audit_cli_flags(parser: Any) -> None:
    """Add the shared ``--audit-log`` / ``--no-audit`` pair to ``parser``."""
    parser.add_argument(
        "--audit-log",
        dest="audit",
        action="store_true",
        default=None,
        help=(
            "Append a JSONL audit record under $TMPDIR/ssh-core/ "
            "(default: on when SSH_CORE_AUDIT=on)"
        ),
    )
    parser.add_argument(
        "--no-audit",
        dest="audit",
        action="store_false",
        help="Do not write audit records",
    )


SYSTEM_SSH_CONFIG = Path("/etc/ssh/ssh_config")
_MERGED_CONFIG_CACHE: dict[str, str] = {}


def merged_ssh_config(
    ssh_config: str | None,
    *,
    system_config: str | os.PathLike[str] | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return the ``-F`` path that keeps the system crypto policy.

    ``ssh -F <file>`` *replaces* the system config rather than layering on
    it. On RHEL / Kylin that file carries
    ``Include /etc/crypto-policies/back-ends/openssh.config``; dropping it
    silently narrows the algorithm list so legacy devices fail with
    ``no matching key exchange method found`` even though plain ``ssh``
    connects (measured against 192.168.30.188 from a Kylin 3 client).

    So a caller-supplied config is wrapped in a small file that includes
    the user config first (first match wins, so user intent is preserved)
    and the system config second. The wrapper is cached per source file
    under ``$TMPDIR/ssh-core/`` so argv stays stable across calls.

    Returns ``None`` when ``ssh_config`` is None — ssh then reads the
    system config itself and nothing needs doing.
    """
    if ssh_config is None:
        return None
    system = Path(system_config) if system_config is not None else SYSTEM_SSH_CONFIG
    user = Path(ssh_config).expanduser()
    if not user.is_file() or not system.is_file():
        return ssh_config
    key = str(user.resolve())
    cached = _MERGED_CONFIG_CACHE.get(key)
    if cached and Path(cached).is_file():
        return cached
    base = (
        Path(cache_dir)
        if cache_dir is not None
        else Path(tempfile.gettempdir()) / AUDIT_DIR_NAME
    )
    base.mkdir(mode=AUDIT_DIR_MODE, parents=True, exist_ok=True)
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    path = base / f"cfg-{digest}.conf"
    try:
        path.write_text(
            f"Include {key}\nInclude {system}\n", encoding="utf-8"
        )
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    except OSError:
        return ssh_config
    _MERGED_CONFIG_CACHE[key] = str(path)
    return str(path)


def _host_patterns(line: str) -> list[str]:
    """Split a ``Host`` directive line into its patterns (may be empty)."""
    return line.split(None, 1)[1].split() if len(line.split(None, 1)) > 1 else []


def alias_matches(pattern: str, alias: str) -> bool:
    """OpenSSH-style ``Host`` matching: case-insensitive, ``*`` / ``?`` globs.

    ``Host *`` matches everything; ``Host web-*`` matches ``WEB-01``. Native
    ssh is case-insensitive and glob-aware, so ssh-core must be too, or
    ``find`` / ``delete`` disagree with what a connection actually resolves to
    (SSH-20 / SSH-21).
    """
    import fnmatch

    return fnmatch.fnmatchcase(alias.casefold(), pattern.casefold())


def is_wildcard_alias(alias: str) -> bool:
    """True when ``alias`` contains any ``*?!`` glob metacharacter."""
    return any(ch in alias for ch in "*?!")


def ssh_config_path_problem(ssh_config: str | None) -> str | None:
    """Return a reason when ``--ssh-config`` cannot be used, else ``None``.

    ``ssh -F <missing>`` fails with ``Can't open user config file`` and ssh's
    own exit 255, which ssh-core used to classify as ``remote_error`` (104) —
    pointing at the remote host when the problem is a local path (FA-21).
    Checking before the spawn turns that into ``config_missing`` (6).
    """
    if not ssh_config:
        return None
    path = Path(ssh_config).expanduser()
    if not path.exists():
        return f"ssh config file does not exist: {path}"
    if not path.is_file():
        return f"ssh config path is not a regular file: {path}"
    return None


def find_host_block(text: str, alias: str) -> dict[str, str] | None:
    """Return the metadata of the *first* Host block matching ``alias``.

    Implements OpenSSH's precedence: the first matching ``Host`` block wins,
    and matching is case-insensitive with glob support.
    """
    pending: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower == "host" or lower.startswith(("host ", "host\t")):
            patterns = _host_patterns(stripped)
            if any(alias_matches(p, alias) for p in patterns):
                meta: dict[str, str] = {}
                for comment in pending:
                    m = SSH_CONFIG_METADATA_RE.match(comment)
                    if m:
                        meta[m.group(1).lower()] = m.group(2).strip()
                return meta
            pending = []
        elif stripped.startswith("#"):
            pending.append(stripped)
        elif not stripped:
            continue
        else:
            pending = []
    return None


def read_ssh_config_metadata(alias: str, ssh_config: str | None = None) -> dict[str, str]:
    """Read ssh-core metadata comment lines for ``alias`` from ``~/.ssh/config``.

    Returns a dict with any of ``description`` / ``environment`` / ``tags``
    / ``location`` / ``auth`` / ``device-type``. Empty dict when the alias
    (or the config file) is absent. Comment metadata is written by
    ``ssh_config_manager.py``; native OpenSSH ignores it.

    Matching follows OpenSSH (SSH-21): case-insensitive, first match wins.
    """
    path = Path(ssh_config or "~/.ssh/config").expanduser()
    if not path.is_file():
        return {}
    try:
        # utf-8-sig: Notepad and PowerShell 5.1 write a BOM, which would
        # otherwise become U+FEFF on the first line and hide the metadata
        # block that precedes the first Host directive.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    return find_host_block(text, alias) or {}


def iter_ssh_config_hosts(
    ssh_config: str | None = None,
    *,
    include_wildcards: bool = False,
) -> list[dict[str, str]]:
    """List every ``Host`` alias with its ssh-core metadata.

    Returns records shaped ``{"alias": ..., "environment": ...,
    "tags": "a,b", "auth": ..., "device-type": ...}`` — metadata values are
    raw strings; callers split ``tags`` themselves. Wildcard Host patterns
    (``Host *`` / ``Host web-*``) are skipped unless ``include_wildcards`` is
    set, in which case they carry ``"wildcard": "true"`` (SSH-20): they are
    not concrete targets for a cluster broadcast, but ``list`` must still
    show them rather than pretending the config only has one entry.
    """
    path = Path(ssh_config or "~/.ssh/config").expanduser()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []

    hosts: list[dict[str, str]] = []
    pending: list[str] = []
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower == "host" or lower.startswith(("host ", "host\t")):
            patterns = _host_patterns(stripped)
            concrete = [p for p in patterns if not is_wildcard_alias(p)]
            wild = [p for p in patterns if is_wildcard_alias(p)]
            meta: dict[str, str] = {}
            for comment in pending:
                m = SSH_CONFIG_METADATA_RE.match(comment)
                if m:
                    meta[m.group(1).lower()] = m.group(2).strip()
            for alias in concrete:
                hosts.append({"alias": alias, **meta})
            if include_wildcards:
                for alias in wild:
                    hosts.append({"alias": alias, "wildcard": "true", **meta})
            pending = []
        elif stripped.startswith("#"):
            pending.append(stripped)
        elif not stripped:
            continue
        else:
            pending = []
    return hosts
