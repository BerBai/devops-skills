"""Unit tests for ssh-core/scripts/deploy_pubkey.py v1.0.

All subprocess / shutil / Path interactions are mocked via
``monkeypatch.setattr``; these run without any real ssh-copy-id,
network, or filesystem write. The live counterpart lives in
``tests/test_deploy_pubkey_integration.py`` (marker: ``live_ssh``).

Decision tree under test is locked by the task PRD / design:

- prd.md D4   — failure_class taxonomy + exit-code table
- prd.md D5   — 6-flag CLI surface
- prd.md D6   — JSON envelope ``data`` payload
- design.md   — regex set, preflight short-circuit rules, idempotency override
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import deploy_pubkey  # noqa: E402, I001

# Named exit codes (avoids PLR2004 magic-value warnings).
EXIT_OK = 0
EXIT_REMOTE_ERROR = 1
EXIT_TOOL_MISSING = 10
EXIT_KEY_FILE_MISSING = 11
EXIT_NO_TTY = 12
EXIT_AUTH = 13
EXIT_HOST_KEY_UNKNOWN = 14
EXIT_NETWORK = 15
EXIT_TIMEOUT = 124

# Defaults to verify against argparse output.
DEFAULT_CONNECT_TIMEOUT = 8
DEFAULT_TIMEOUT = 180

# A minimal, syntactically valid OpenSSH ed25519 public-key line.
_VALID_PUBKEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIBoZE9k3LfPNiwLOzkk+xfa9OqHV5GLOpXTGy0Wzj8eQ "
    "testuser@host\n"
)


# -----------------------------------------------------------------------------
# helpers / fixtures
# -----------------------------------------------------------------------------


def _completed(rc: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """Stand-in for subprocess.CompletedProcess satisfying attribute access."""
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _args(**overrides: object) -> SimpleNamespace:
    """Build an argparse-like namespace with deploy_pubkey defaults."""
    base: dict[str, object] = {
        "alias": "somehost",
        "key": "~/.ssh/id_ed25519.pub",
        "connect_timeout": DEFAULT_CONNECT_TIMEOUT,
        "timeout": DEFAULT_TIMEOUT,
        "ssh_config": None,
        "accept_new_hostkey": False,
        "json": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def valid_pubkey(tmp_path: Path) -> Path:
    """Write a syntactically-valid ed25519 .pub into tmp_path and return it."""
    p = tmp_path / "id_ed25519.pub"
    p.write_text(_VALID_PUBKEY, encoding="utf-8")
    p.chmod(0o600)
    return p


@pytest.fixture()
def preflight_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make _preflight() return None unconditionally (ssh-copy-id present)."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")


# -----------------------------------------------------------------------------
# build_parser — 4 cases
# -----------------------------------------------------------------------------


def test_parser_required_alias_missing() -> None:
    """argparse exits 2 when the alias positional is omitted."""
    with pytest.raises(SystemExit) as exc:
        deploy_pubkey.build_parser().parse_args([])
    assert exc.value.code == 2  # noqa: PLR2004


def test_parser_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """--help exits 0 and lists every v1.0 flag (D5)."""
    with pytest.raises(SystemExit) as exc:
        deploy_pubkey.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    for flag in (
        "--key",
        "--connect-timeout",
        "--timeout",
        "--ssh-config",
        "--accept-new-hostkey",
        "--json",
    ):
        assert flag in out


def test_parser_rejects_unknown_flag() -> None:
    """Any flag outside D5 surface is rejected (exit 2)."""
    with pytest.raises(SystemExit) as exc:
        deploy_pubkey.build_parser().parse_args(["somehost", "--bogus"])
    assert exc.value.code == 2  # noqa: PLR2004


def test_parser_rejects_v02_strip_password() -> None:
    """The v0.2 stub had --strip-password; v1.0 explicitly removes it (D2)."""
    with pytest.raises(SystemExit) as exc:
        deploy_pubkey.build_parser().parse_args(["somehost", "--strip-password"])
    assert exc.value.code == 2  # noqa: PLR2004


def test_parser_accepts_all_flags() -> None:
    """All flags parse with reasonable defaults / explicit values."""
    args = deploy_pubkey.build_parser().parse_args([
        "myhost",
        "--key", "/tmp/x.pub",
        "--connect-timeout", "5",
        "--timeout", "60",
        "--ssh-config", "/tmp/cfg",
        "--accept-new-hostkey",
        "--json",
    ])
    assert args.alias == "myhost"
    assert args.key == "/tmp/x.pub"
    assert args.connect_timeout == 5  # noqa: PLR2004
    assert args.timeout == 60  # noqa: PLR2004
    assert args.ssh_config == "/tmp/cfg"
    assert args.accept_new_hostkey is True
    assert args.json is True


def test_parser_defaults() -> None:
    """Defaults match D5 (key=~/.ssh/id_ed25519.pub, ct=8, t=180)."""
    args = deploy_pubkey.build_parser().parse_args(["x"])
    assert args.key == "~/.ssh/id_ed25519.pub"
    assert args.connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert args.timeout == DEFAULT_TIMEOUT
    assert args.ssh_config is None
    assert args.accept_new_hostkey is False
    assert args.json is False


# -----------------------------------------------------------------------------
# _resolve_key — 3 cases
# -----------------------------------------------------------------------------


def test_resolve_key_expands_home() -> None:
    """``~/.ssh/...`` expands relative to $HOME."""
    out = deploy_pubkey._resolve_key("~/.ssh/id_ed25519.pub")
    assert str(out).endswith(".ssh/id_ed25519.pub")
    # Path.resolve() yields an absolute path on all platforms.
    assert out.is_absolute()


def test_resolve_key_explicit_absolute(tmp_path: Path) -> None:
    """Absolute path passes through unchanged (modulo .resolve normalization)."""
    p = tmp_path / "k.pub"
    p.write_text("x", encoding="utf-8")
    out = deploy_pubkey._resolve_key(str(p))
    assert out == p.resolve()


def test_resolve_key_relative_becomes_absolute(tmp_path: Path) -> None:
    """Relative path gets resolved against CWD."""
    out = deploy_pubkey._resolve_key("some-rel-path.pub")
    assert out.is_absolute()


# -----------------------------------------------------------------------------
# _looks_like_openssh_pubkey — first-line sanity check
# -----------------------------------------------------------------------------


def test_pubkey_sanity_accepts_ed25519(valid_pubkey: Path) -> None:
    assert deploy_pubkey._looks_like_openssh_pubkey(valid_pubkey) is True


def test_pubkey_sanity_accepts_rsa(tmp_path: Path) -> None:
    p = tmp_path / "k.pub"
    p.write_text("ssh-rsa AAAA... user@h\n", encoding="utf-8")
    assert deploy_pubkey._looks_like_openssh_pubkey(p) is True


def test_pubkey_sanity_accepts_ecdsa(tmp_path: Path) -> None:
    p = tmp_path / "k.pub"
    p.write_text("ecdsa-sha2-nistp256 AAAA... user@h\n", encoding="utf-8")
    assert deploy_pubkey._looks_like_openssh_pubkey(p) is True


def test_pubkey_sanity_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "k.pub"
    p.write_text("garbage not a key\n", encoding="utf-8")
    assert deploy_pubkey._looks_like_openssh_pubkey(p) is False


def test_pubkey_sanity_rejects_private_key(tmp_path: Path) -> None:
    """The risk we care about: a private key disguised as a .pub."""
    p = tmp_path / "k.pub"
    p.write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAA...\n",
        encoding="utf-8",
    )
    assert deploy_pubkey._looks_like_openssh_pubkey(p) is False


def test_pubkey_sanity_unreadable_returns_false(tmp_path: Path) -> None:
    """A nonexistent file → False (OSError caught)."""
    assert deploy_pubkey._looks_like_openssh_pubkey(tmp_path / "no.pub") is False


# -----------------------------------------------------------------------------
# _preflight — 4 cases (happy / tool_missing / key not exists / not a pubkey)
# -----------------------------------------------------------------------------


def test_preflight_happy_returns_none(
    valid_pubkey: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ssh-copy-id is present and the key file looks like a pubkey,
    preflight returns None (caller proceeds to subprocess)."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    args = _args(key=str(valid_pubkey))
    assert deploy_pubkey._preflight(args) is None


def test_preflight_tool_missing(
    valid_pubkey: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shutil.which returns None → exit 10 / tool_missing."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: None)
    args = _args(key=str(valid_pubkey))
    envelope = deploy_pubkey._preflight(args)
    assert envelope is not None
    assert envelope["exit_code"] == EXIT_TOOL_MISSING
    assert envelope["data"]["failure_class"] == "tool_missing"
    # Preflight refusals never spawn subprocess → argv is empty.
    assert envelope["data"]["ssh_copy_id_argv"] == []
    # And the hint suggests an install.
    assert "ssh-copy-id" in envelope["stderr"]


def test_preflight_key_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nonexistent --key → exit 11 / key_file_missing, argv empty."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    args = _args(key=str(tmp_path / "no-such-key.pub"))
    envelope = deploy_pubkey._preflight(args)
    assert envelope is not None
    assert envelope["exit_code"] == EXIT_KEY_FILE_MISSING
    assert envelope["data"]["failure_class"] == "key_file_missing"
    assert envelope["data"]["ssh_copy_id_argv"] == []


def test_preflight_key_not_readable(
    valid_pubkey: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable --key (chmod 000) → exit 11 / key_file_missing.

    Skipped as root since chmod is then a no-op.
    """
    if os.geteuid() == 0:
        pytest.skip("chmod-based unreadable test is meaningless as root")
    valid_pubkey.chmod(0o000)
    try:
        monkeypatch.setattr(
            deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id"
        )
        args = _args(key=str(valid_pubkey))
        envelope = deploy_pubkey._preflight(args)
        assert envelope is not None
        assert envelope["exit_code"] == EXIT_KEY_FILE_MISSING
        assert envelope["data"]["failure_class"] == "key_file_missing"
    finally:
        valid_pubkey.chmod(0o600)


def test_preflight_key_not_a_pubkey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-line content doesn't match an OpenSSH key prefix → key_file_missing.

    This is the catastrophic-misuse guard from design.md "Risks": a user
    pointing --key at a private key would otherwise have that private
    key installed verbatim into the remote authorized_keys.
    """
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    p = tmp_path / "fake.pub"
    p.write_text("not-an-ssh-key just some text\n", encoding="utf-8")
    args = _args(key=str(p))
    envelope = deploy_pubkey._preflight(args)
    assert envelope is not None
    assert envelope["exit_code"] == EXIT_KEY_FILE_MISSING
    assert envelope["data"]["failure_class"] == "key_file_missing"
    assert "doesn't look like an OpenSSH public key" in envelope["stderr"]


# -----------------------------------------------------------------------------
# _build_argv — 6 cases per design.md test strategy
# -----------------------------------------------------------------------------


def test_build_argv_no_optional_flags() -> None:
    args = _args(alias="h1", connect_timeout=8)
    key_path = Path("/tmp/x.pub")
    argv = deploy_pubkey._build_argv(args, key_path)
    assert argv == [
        "ssh-copy-id",
        "-i", "/tmp/x.pub",
        "-o", "ConnectTimeout=8",
        "h1",
    ]


def test_build_argv_connect_timeout_overrides_default() -> None:
    args = _args(connect_timeout=30, alias="h1")
    argv = deploy_pubkey._build_argv(args, Path("/tmp/x.pub"))
    assert "ConnectTimeout=30" in argv


def test_build_argv_with_ssh_config_adds_F_flag() -> None:
    args = _args(ssh_config="/tmp/cfg", alias="h1")
    argv = deploy_pubkey._build_argv(args, Path("/tmp/x.pub"))
    assert "-F" in argv
    assert argv[argv.index("-F") + 1] == "/tmp/cfg"


def test_build_argv_with_accept_new_hostkey() -> None:
    args = _args(accept_new_hostkey=True, alias="h1")
    argv = deploy_pubkey._build_argv(args, Path("/tmp/x.pub"))
    assert "StrictHostKeyChecking=accept-new" in argv


def test_build_argv_all_flags_together() -> None:
    args = _args(
        alias="prod-web-02",
        connect_timeout=12,
        ssh_config="/etc/ssh/config",
        accept_new_hostkey=True,
    )
    argv = deploy_pubkey._build_argv(args, Path("/keys/id_ed25519.pub"))
    # -i is always first immediately after ssh-copy-id
    assert argv[0] == "ssh-copy-id"
    assert argv[1] == "-i"
    assert argv[2] == "/keys/id_ed25519.pub"
    # alias is always last
    assert argv[-1] == "prod-web-02"
    # ConnectTimeout, -F, and StrictHostKeyChecking all appear
    assert "ConnectTimeout=12" in argv
    assert "/etc/ssh/config" in argv
    assert "StrictHostKeyChecking=accept-new" in argv


def test_build_argv_never_includes_batchmode_yes() -> None:
    """BatchMode=yes would defeat the password prompt (design.md note)."""
    args = _args(alias="h1", accept_new_hostkey=True, ssh_config="/tmp/cfg")
    argv = deploy_pubkey._build_argv(args, Path("/tmp/x.pub"))
    joined = " ".join(argv)
    assert "BatchMode=yes" not in joined


def test_build_argv_i_flag_always_first() -> None:
    """-i is always at index 1 (right after the binary name), in every
    flag combination — required for the design.md test row '-i always first'."""
    for kwargs in (
        {},
        {"connect_timeout": 30},
        {"ssh_config": "/tmp/cfg"},
        {"accept_new_hostkey": True},
        {"ssh_config": "/tmp/cfg", "accept_new_hostkey": True},
    ):
        args = _args(**kwargs)
        argv = deploy_pubkey._build_argv(args, Path("/tmp/x.pub"))
        assert argv[0] == "ssh-copy-id"
        assert argv[1] == "-i", f"argv was {argv}"


# -----------------------------------------------------------------------------
# _run_ssh_copy_id — 3 cases
# -----------------------------------------------------------------------------


def test_run_returns_rc_stdout_stderr_elapsed_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc==0 happy path: we forward stdout/stderr verbatim, return non-timed-out."""
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _completed(0, stdout="", stderr="Number of key(s) added: 1\n")

    monkeypatch.setattr(deploy_pubkey.subprocess, "run", fake_run)
    rc, stdout, stderr, elapsed, timed_out = deploy_pubkey._run_ssh_copy_id(
        ["ssh-copy-id", "host"], timeout=60
    )
    assert rc == 0
    assert stderr.endswith("Number of key(s) added: 1\n")
    assert isinstance(elapsed, float)
    assert timed_out is False
    # stdin=None is critical for tty inheritance (design.md).
    assert seen["kwargs"]["stdin"] is None
    # subprocess safety invariant: argv is a list, no shell=True.
    assert isinstance(seen["argv"], list)
    assert seen["kwargs"].get("shell") in (None, False)
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["text"] is True
    assert seen["kwargs"]["timeout"] == 60  # noqa: PLR2004


def test_run_non_zero_returncode_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc != 0: we forward it as-is; classifying is the caller's job."""

    def fake_run(_argv, **_kwargs):
        return _completed(1, stdout="", stderr="Permission denied (publickey,password).")

    monkeypatch.setattr(deploy_pubkey.subprocess, "run", fake_run)
    rc, _stdout, stderr, _elapsed, timed_out = deploy_pubkey._run_ssh_copy_id(
        ["ssh-copy-id", "host"], timeout=60
    )
    assert rc == 1
    assert "Permission denied" in stderr
    assert timed_out is False


def test_run_timeout_returns_sentinel_and_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutExpired → return (-1, "", "", timeout_budget, True)."""

    def raise_timeout(_argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh-copy-id", timeout=kwargs.get("timeout"))

    monkeypatch.setattr(deploy_pubkey.subprocess, "run", raise_timeout)
    rc, stdout, stderr, elapsed, timed_out = deploy_pubkey._run_ssh_copy_id(
        ["ssh-copy-id", "host"], timeout=5
    )
    assert rc == deploy_pubkey._RC_TIMEOUT_SENTINEL
    assert stdout == ""
    assert stderr == ""
    assert elapsed == 5.0  # noqa: PLR2004
    assert timed_out is True


# -----------------------------------------------------------------------------
# _parse_success_outcome — 4+ cases
# -----------------------------------------------------------------------------


def test_parse_success_number_added_one() -> None:
    outcome, n = deploy_pubkey._parse_success_outcome(
        "/usr/bin/ssh-copy-id: Number of key(s) added: 1\n"
    )
    assert outcome == "installed"
    assert n == 1


def test_parse_success_number_added_three() -> None:
    outcome, n = deploy_pubkey._parse_success_outcome(
        "Number of key(s) added: 3\n"
    )
    assert outcome == "installed"
    assert n == 3  # noqa: PLR2004


def test_parse_success_warning_already() -> None:
    """Newer OpenSSH (WARNING flavour)."""
    outcome, n = deploy_pubkey._parse_success_outcome(
        "WARNING: All keys were skipped because they already exist on the remote system.\n"
    )
    assert outcome == "already_installed"
    assert n == 0


def test_parse_success_error_already() -> None:
    """Older OpenSSH (ERROR flavour)."""
    outcome, n = deploy_pubkey._parse_success_outcome(
        "ERROR: All keys were skipped because they already exist on the remote system.\n"
    )
    assert outcome == "already_installed"
    assert n == 0


def test_parse_success_unparseable_falls_back() -> None:
    """If wording shifts and nothing matches, return (installed, None) — null
    keys_added rather than misreport."""
    outcome, n = deploy_pubkey._parse_success_outcome("(some unrecognised banner)\n")
    assert outcome == "installed"
    assert n is None


# -----------------------------------------------------------------------------
# _classify_failure — 8 cases (one per failure_class)
# -----------------------------------------------------------------------------


def test_classify_timeout() -> None:
    assert (
        deploy_pubkey._classify_failure(-1, "", is_tty=True, timed_out=True)
        == "timeout"
    )


def test_classify_no_tty_for_password() -> None:
    """stdin not a TTY + a password-prompt-shaped stderr → no_tty_for_password."""
    stderr = "Permission denied, please try again."
    assert (
        deploy_pubkey._classify_failure(1, stderr, is_tty=False, timed_out=False)
        == "no_tty_for_password"
    )


def test_classify_auth() -> None:
    stderr = "Permission denied (publickey,password)."
    assert (
        deploy_pubkey._classify_failure(1, stderr, is_tty=True, timed_out=False)
        == "auth"
    )


def test_classify_host_key_unknown() -> None:
    stderr = "Host key verification failed."
    assert (
        deploy_pubkey._classify_failure(1, stderr, is_tty=True, timed_out=False)
        == "host_key_unknown"
    )


def test_classify_network_dns() -> None:
    stderr = "ssh: Could not resolve hostname foo.example.com: ..."
    assert (
        deploy_pubkey._classify_failure(1, stderr, is_tty=True, timed_out=False)
        == "network"
    )


def test_classify_network_connection_refused() -> None:
    stderr = "ssh: connect to host: Connection refused"
    assert (
        deploy_pubkey._classify_failure(1, stderr, is_tty=True, timed_out=False)
        == "network"
    )


def test_classify_remote_error_catchall() -> None:
    """Anything that doesn't match a more specific pattern → remote_error."""
    assert (
        deploy_pubkey._classify_failure(
            5, "some unknown failure mode", is_tty=True, timed_out=False
        )
        == "remote_error"
    )


def test_classify_timeout_beats_other_signals() -> None:
    """When timed_out=True, classification is timeout regardless of stderr."""
    stderr = "Permission denied (publickey)."  # would otherwise match auth
    assert (
        deploy_pubkey._classify_failure(-1, stderr, is_tty=True, timed_out=True)
        == "timeout"
    )


def test_classify_no_tty_precedes_auth() -> None:
    """When is_tty=False AND stderr matches a prompt-fail pattern, the
    no_tty path wins even though `Permission denied` is in stderr."""
    stderr = "Permission denied, please try again."
    assert (
        deploy_pubkey._classify_failure(1, stderr, is_tty=False, timed_out=False)
        == "no_tty_for_password"
    )


# -----------------------------------------------------------------------------
# Idempotency override (rc != 0 + "All keys were skipped" → success)
# -----------------------------------------------------------------------------


def test_idempotency_override_in_main(
    valid_pubkey: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Older ssh-copy-id builds exit non-zero when all keys are already
    installed; main() reclassifies that to success/already_installed."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            1,
            stdout="",
            stderr=(
                "INFO: Source of key(s) ...\n"
                "WARNING: All keys were skipped because they already exist on the remote system.\n"
            ),
        ),
    )
    rc = deploy_pubkey.main([
        "somehost", "--key", str(valid_pubkey), "--json"
    ])
    assert rc == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["exit_code"] == EXIT_OK
    assert payload["data"]["outcome"] == "already_installed"
    assert payload["data"]["failure_class"] is None
    # And we did NOT pollute success with a stale failure hint.
    assert payload["data"]["keys_added"] == 0


def test_idempotency_does_not_override_on_timeout(
    valid_pubkey: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If TimeoutExpired fired, we never look at stderr — must remain timeout."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")

    def raise_timeout(_argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh-copy-id", timeout=kwargs.get("timeout"))

    monkeypatch.setattr(deploy_pubkey.subprocess, "run", raise_timeout)
    rc = deploy_pubkey.main([
        "somehost", "--key", str(valid_pubkey), "--timeout", "5", "--json"
    ])
    assert rc == EXIT_TIMEOUT

    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["failure_class"] == "timeout"


# -----------------------------------------------------------------------------
# Envelope shape (PRD D6)
# -----------------------------------------------------------------------------


def test_success_envelope_shape(valid_pubkey: Path) -> None:
    """Verify D6 success-envelope `data` payload keys + types."""
    args = _args(key=str(valid_pubkey), alias="prod-web-02")
    env = deploy_pubkey._success_envelope(
        args,
        key_path=Path("/tmp/x.pub"),
        outcome="installed",
        keys_added=1,
        argv=["ssh-copy-id", "-i", "/tmp/x.pub", "prod-web-02"],
        elapsed=0.42,
        raw_stderr="Number of key(s) added: 1\n",
    )
    assert env["success"] is True
    assert env["exit_code"] == 0
    assert env["stderr"] == ""
    # stdout carries a one-line human summary even in success path.
    assert "installed key on prod-web-02" in env["stdout"]
    # data payload
    d = env["data"]
    assert d["alias"] == "prod-web-02"
    assert d["key_path"] == "/tmp/x.pub"
    assert d["outcome"] == "installed"
    assert d["keys_added"] == 1
    assert d["failure_class"] is None
    assert d["ssh_copy_id_argv"] == ["ssh-copy-id", "-i", "/tmp/x.pub", "prod-web-02"]
    assert d["elapsed_s"] == pytest.approx(0.42)
    assert d["raw_stderr"] == "Number of key(s) added: 1\n"


def test_already_installed_envelope_human_text() -> None:
    args = _args(alias="prod-web-02")
    env = deploy_pubkey._success_envelope(
        args,
        key_path=Path("/tmp/x.pub"),
        outcome="already_installed",
        keys_added=0,
        argv=["ssh-copy-id"],
        elapsed=0.1,
        raw_stderr="",
    )
    assert "key already on prod-web-02" in env["stdout"]


def test_success_envelope_installed_unknown_count() -> None:
    """When _parse_success_outcome returned (installed, None) — wording
    shift fallback — the human summary omits the count gracefully."""
    args = _args(alias="prod-web-02")
    env = deploy_pubkey._success_envelope(
        args,
        key_path=Path("/tmp/x.pub"),
        outcome="installed",
        keys_added=None,
        argv=["ssh-copy-id"],
        elapsed=0.1,
        raw_stderr="(some unrecognised banner)\n",
    )
    assert "installed key on prod-web-02" in env["stdout"]
    # No "keys_added=N" suffix when we have no count to report.
    assert "keys_added" not in env["stdout"]
    assert env["data"]["keys_added"] is None


def test_failure_envelope_shape() -> None:
    """Verify failure-envelope wires data.failure_class + the right exit_code."""
    args = _args(alias="bad-host")
    env = deploy_pubkey._failure_envelope(
        args,
        key_path=Path("/tmp/x.pub"),
        fclass="network",
        argv=["ssh-copy-id", "bad-host"],
        elapsed=0.2,
        raw_stderr="ssh: Could not resolve hostname bad-host: ...",
    )
    assert env["success"] is False
    assert env["exit_code"] == EXIT_NETWORK
    assert env["data"]["failure_class"] == "network"
    assert env["data"]["ssh_copy_id_argv"] == ["ssh-copy-id", "bad-host"]
    assert env["data"]["outcome"] is None
    assert env["data"]["keys_added"] is None
    # Hint substituted the alias.
    assert "bad-host" in env["stderr"]


# -----------------------------------------------------------------------------
# main() integration — each failure_class wired correctly
# -----------------------------------------------------------------------------


def _run_main_capture_json(
    capsys: pytest.CaptureFixture[str], cli_args: list[str]
) -> tuple[int, dict]:
    rc = deploy_pubkey.main(cli_args)
    out = capsys.readouterr().out
    payload = json.loads(out) if out.strip() else {}
    return rc, payload


def test_main_tool_missing(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: None)
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--json"]
    )
    assert rc == EXIT_TOOL_MISSING
    assert payload["data"]["failure_class"] == "tool_missing"
    assert payload["data"]["ssh_copy_id_argv"] == []


def test_main_key_file_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(tmp_path / "no.pub"), "--json"]
    )
    assert rc == EXIT_KEY_FILE_MISSING
    assert payload["data"]["failure_class"] == "key_file_missing"
    assert payload["data"]["ssh_copy_id_argv"] == []


def test_main_success_installed(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            0, stdout="", stderr="Number of key(s) added: 1\n"
        ),
    )
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--json"]
    )
    assert rc == EXIT_OK
    assert payload["data"]["outcome"] == "installed"
    assert payload["data"]["keys_added"] == 1
    assert payload["data"]["failure_class"] is None
    # argv list is populated (we DID spawn the subprocess).
    assert payload["data"]["ssh_copy_id_argv"][0] == "ssh-copy-id"


def test_main_success_already_installed_rc_zero(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """rc==0 + "All keys were skipped" → already_installed."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            0,
            stdout="",
            stderr=(
                "WARNING: All keys were skipped because they already exist "
                "on the remote system.\n"
            ),
        ),
    )
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--json"]
    )
    assert rc == EXIT_OK
    assert payload["data"]["outcome"] == "already_installed"
    assert payload["data"]["keys_added"] == 0


def test_main_auth_failure(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            1, stdout="", stderr="Permission denied (publickey,password)."
        ),
    )
    # Force is_tty=True so we don't pick up no_tty path.
    monkeypatch.setattr(deploy_pubkey.sys.stdin, "isatty", lambda: True)
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--json"]
    )
    assert rc == EXIT_AUTH
    assert payload["data"]["failure_class"] == "auth"


def test_main_host_key_unknown(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            1, stdout="", stderr="Host key verification failed."
        ),
    )
    monkeypatch.setattr(deploy_pubkey.sys.stdin, "isatty", lambda: True)
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--json"]
    )
    assert rc == EXIT_HOST_KEY_UNKNOWN
    assert payload["data"]["failure_class"] == "host_key_unknown"
    assert "--accept-new-hostkey" in payload["stderr"]


def test_main_network_failure(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            1, stdout="", stderr="ssh: Could not resolve hostname xyz: ..."
        ),
    )
    monkeypatch.setattr(deploy_pubkey.sys.stdin, "isatty", lambda: True)
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--json"]
    )
    assert rc == EXIT_NETWORK
    assert payload["data"]["failure_class"] == "network"


def test_main_timeout(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")

    def raise_timeout(_argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh-copy-id", timeout=kwargs.get("timeout"))

    monkeypatch.setattr(deploy_pubkey.subprocess, "run", raise_timeout)
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--timeout", "5", "--json"]
    )
    assert rc == EXIT_TIMEOUT
    assert payload["data"]["failure_class"] == "timeout"
    assert payload["data"]["elapsed_s"] == 5.0  # noqa: PLR2004


def test_main_remote_error_catchall(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown ssh-copy-id failure mode falls to remote_error / exit 1."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(2, stdout="", stderr="some unknown error"),
    )
    monkeypatch.setattr(deploy_pubkey.sys.stdin, "isatty", lambda: True)
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--json"]
    )
    assert rc == EXIT_REMOTE_ERROR
    assert payload["data"]["failure_class"] == "remote_error"


def test_main_no_tty_for_password(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stdin not a TTY + password-prompt-fail stderr → no_tty_for_password."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            1, stdout="", stderr="Permission denied, please try again."
        ),
    )
    monkeypatch.setattr(deploy_pubkey.sys.stdin, "isatty", lambda: False)
    rc, payload = _run_main_capture_json(
        capsys, ["somehost", "--key", str(valid_pubkey), "--json"]
    )
    assert rc == EXIT_NO_TTY
    assert payload["data"]["failure_class"] == "no_tty_for_password"
    assert "interactive terminal" in payload["stderr"]


def test_main_returns_exit_code_from_envelope(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() exit code matches the envelope's exit_code (so the shell sees it)."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            1, stdout="", stderr="Host key verification failed."
        ),
    )
    monkeypatch.setattr(deploy_pubkey.sys.stdin, "isatty", lambda: True)
    rc = deploy_pubkey.main(["somehost", "--key", str(valid_pubkey), "--json"])
    capsys.readouterr()
    assert rc == EXIT_HOST_KEY_UNKNOWN


# -----------------------------------------------------------------------------
# Human-mode output (no --json)
# -----------------------------------------------------------------------------


def test_human_mode_failure_writes_hint_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --json: stderr is the short actionable hint."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    rc = deploy_pubkey.main(["somehost", "--key", str(tmp_path / "no.pub")])
    captured = capsys.readouterr()
    assert rc == EXIT_KEY_FILE_MISSING
    # Without --json: stdout is empty, stderr carries the hint.
    assert captured.out == ""
    assert "public key not found" in captured.err or "doesn't look like" in captured.err


def test_human_mode_success_writes_summary_to_stdout(
    valid_pubkey: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --json: success summary lands on stdout."""
    monkeypatch.setattr(deploy_pubkey.shutil, "which", lambda _x: "/usr/bin/ssh-copy-id")
    monkeypatch.setattr(
        deploy_pubkey.subprocess,
        "run",
        lambda *_a, **_k: _completed(
            0, stdout="", stderr="Number of key(s) added: 1\n"
        ),
    )
    rc = deploy_pubkey.main(["prod-web-01", "--key", str(valid_pubkey)])
    captured = capsys.readouterr()
    assert rc == EXIT_OK
    assert "installed key on prod-web-01" in captured.out


# -----------------------------------------------------------------------------
# Regex sanity smoke (catches the most likely future regression: someone
# tweaks a pattern and accidentally narrows it)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Number of key(s) added: 1\n", "installed"),
        ("Number of key(s) added: 12\n", "installed"),
        ("WARNING: All keys were skipped because they already exist...", "already_installed"),
        ("ERROR: All keys were skipped because they already exist", "already_installed"),
    ],
)
def test_parse_success_regex_smoke(stderr: str, expected: str) -> None:
    outcome, _ = deploy_pubkey._parse_success_outcome(stderr)
    assert outcome == expected


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Could not resolve hostname x", "network"),
        ("Connection refused", "network"),
        ("Connection timed out", "network"),
        ("Network is unreachable", "network"),
        ("No route to host", "network"),
        ("Host key verification failed.", "host_key_unknown"),
        ("No matching host key type found.", "host_key_unknown"),
        ("Permission denied (publickey,password).", "auth"),
    ],
)
def test_classify_failure_regex_smoke(stderr: str, expected: str) -> None:
    assert (
        deploy_pubkey._classify_failure(1, stderr, is_tty=True, timed_out=False)
        == expected
    )


def test_module_level_regexes_compile() -> None:
    """Smoke: all module-level regexes are actually compiled re.Pattern."""
    for name in (
        "_INSTALLED_RE",
        "_ALREADY_RE",
        "_AUTH_RE",
        "_HOST_KEY_RE",
        "_NETWORK_RE",
        "_PROMPT_FAIL_RE",
    ):
        assert isinstance(getattr(deploy_pubkey, name), re.Pattern), name


# -----------------------------------------------------------------------------
# Exit-code dict sanity
# -----------------------------------------------------------------------------


def test_exit_codes_match_d4_pin() -> None:
    """Pin: codes 10-15 + 124 + 1 per PRD D4."""
    assert deploy_pubkey._EXIT_CODES["tool_missing"] == EXIT_TOOL_MISSING
    assert deploy_pubkey._EXIT_CODES["key_file_missing"] == EXIT_KEY_FILE_MISSING
    assert deploy_pubkey._EXIT_CODES["no_tty_for_password"] == EXIT_NO_TTY
    assert deploy_pubkey._EXIT_CODES["auth"] == EXIT_AUTH
    assert deploy_pubkey._EXIT_CODES["host_key_unknown"] == EXIT_HOST_KEY_UNKNOWN
    assert deploy_pubkey._EXIT_CODES["network"] == EXIT_NETWORK
    assert deploy_pubkey._EXIT_CODES["timeout"] == EXIT_TIMEOUT
    assert deploy_pubkey._EXIT_CODES["remote_error"] == EXIT_REMOTE_ERROR


def test_exit_codes_unique() -> None:
    """No two failure_class strings share an exit_code."""
    codes = list(deploy_pubkey._EXIT_CODES.values())
    assert len(codes) == len(set(codes))


def test_human_hints_cover_all_failure_classes() -> None:
    """Every failure_class in _EXIT_CODES must have a matching hint."""
    assert set(deploy_pubkey._HUMAN_HINTS.keys()) == set(deploy_pubkey._EXIT_CODES.keys())
