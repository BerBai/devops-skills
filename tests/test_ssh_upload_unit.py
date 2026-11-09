"""Local unit tests for ``ssh_upload.py`` guards and envelope contract.

The 1.4.0 sync deleted ``test_ssh_upload_unit.py`` (22 cases). These are the
cases that do not need a host: argument guards, the ``--json`` envelope shape,
the exit-code taxonomy, and the ``--delete``/``--chmod`` refusals that run
before any transport is chosen.

The transport itself is covered by ``test_ssh_upload_integration.py``
(marker ``live_ssh``) and, at the lib level, by
``test_ssh_core_transfers.py``.

Every case here is subprocess-level, so nothing is monkeypatched: the guards
must fire before ssh/scp is ever spawned, which is exactly what is asserted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
UPLOAD = SCRIPTS / "ssh_upload.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.ssh_core.helpers import load_script  # noqa: E402

import lib.transfer as transfer  # noqa: E402
from lib import exit_codes  # noqa: E402

EXIT_OK = 0
EXIT_INVALID_ARGUMENTS = exit_codes.EXIT_INVALID_ARGUMENTS
EXIT_LOCAL_PATH_MISSING = exit_codes.EXIT_LOCAL_PATH_MISSING
EXIT_LOCAL_PATH_IS_DIRECTORY = exit_codes.EXIT_LOCAL_PATH_IS_DIRECTORY


def _run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(UPLOAD), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _envelope(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


# -----------------------------------------------------------------------------
# argument guards (no host contact)
# -----------------------------------------------------------------------------


def test_help_exits_zero() -> None:
    proc = _run("--help")
    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()
    for flag in ("--resume", "--recursive", "--chmod", "--bwlimit", "--transport"):
        assert flag in proc.stdout


def test_help_documents_the_overwrite_policy() -> None:
    """GAP-003: the default (refuse to clobber) has to be discoverable."""
    text = _run("--help").stdout
    assert "--overwrite" in text
    assert "remote_path_exists" in text
    assert "--resume and --delete" in text
    assert "168" in text


def test_version_flag_reports_the_shared_version() -> None:
    upload = load_script("ssh_upload")
    from lib.version import __version__

    proc = _run("--version")
    assert proc.returncode == 0
    assert f"ssh-core {__version__}" in proc.stdout
    assert upload.__name__  # module loaded, no import side effects


def test_missing_local_path_is_reported_not_raised(tmp_path: Path) -> None:
    proc = _run("pai", str(tmp_path / "nope.bin"), "/tmp/x", "--json")
    assert proc.returncode == EXIT_LOCAL_PATH_MISSING
    env = _envelope(proc)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "local_path_missing"
    assert "local path not found" in env["stderr"]


def test_directory_without_recursive_is_refused(tmp_path: Path) -> None:
    proc = _run("pai", str(tmp_path), "/tmp/x", "--json")
    assert proc.returncode == EXIT_LOCAL_PATH_IS_DIRECTORY
    env = _envelope(proc)
    assert env["data"]["failure_class"] == "local_path_is_directory"
    assert "--recursive" in env["stderr"]


def test_invalid_chmod_octal_is_refused(tmp_path: Path) -> None:
    payload = tmp_path / "f.bin"
    payload.write_bytes(b"x")
    proc = _run("pai", str(payload), "/tmp/x", "--chmod", "not-octal", "--json")
    assert proc.returncode == EXIT_INVALID_ARGUMENTS
    assert _envelope(proc)["data"]["failure_class"] == "invalid_arguments"


def test_delete_without_yes_is_refused(tmp_path: Path) -> None:
    """--delete is destructive; it may not run without an explicit --yes."""
    payload = tmp_path / "f.bin"
    payload.write_bytes(b"x")
    proc = _run("pai", str(payload), "/tmp/x", "--delete", "--json")
    assert proc.returncode == exit_codes.exit_code_for("confirmation_required")
    env = _envelope(proc)
    assert env["data"]["failure_class"] == "confirmation_required"
    assert "--yes" in env["stderr"]


def test_delete_without_recursive_is_refused(tmp_path: Path) -> None:
    """Mirroring is a tree operation; a single file cannot be pruned."""
    payload = tmp_path / "f.bin"
    payload.write_bytes(b"x")
    proc = _run("pai", str(payload), "/tmp/x", "--delete", "--yes", "--json")
    env = _envelope(proc)
    assert env["success"] is False
    assert env["data"]["failure_class"] in {
        "confirmation_required",
        "invalid_arguments",
        "mode_deferred",
    }


def test_bad_argv_emits_json_not_a_traceback(tmp_path: Path) -> None:
    proc = _run("pai", str(tmp_path / "nope"), "--json")
    assert proc.returncode == EXIT_INVALID_ARGUMENTS
    env = _envelope(proc)
    assert env["data"]["failure_class"] == "invalid_arguments"
    assert "usage" in env["data"]


def test_unknown_flag_is_an_argument_error() -> None:
    proc = _run("pai", "a", "b", "--not-a-flag", "--json")
    assert proc.returncode == EXIT_INVALID_ARGUMENTS
    assert _envelope(proc)["data"]["failure_class"] == "invalid_arguments"


# -----------------------------------------------------------------------------
# transport selection is deferred, never silently downgraded
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--resume", "--bwlimit"])
def test_sftp_only_flags_are_refused_on_the_scp_backend(
    tmp_path: Path, flag: str
) -> None:
    """On the openssh backend these must fail loudly, not be ignored.

    1.4.0 returns ``mode_deferred`` (166) with ``data.deferred_flags`` carrying
    the flag names, so a caller can see *which* request could not be honoured.
    """
    payload = tmp_path / "f.bin"
    payload.write_bytes(b"x")
    extra = [flag, "10"] if flag == "--bwlimit" else [flag]
    proc = _run(
        "pai", str(payload), "/tmp/x",
        "--transport", "openssh", *extra, "--json",
        timeout=120,
    )
    env = _envelope(proc)
    assert env["success"] is False
    if env["exit_code"] == exit_codes.EXIT_MODE_UNSUPPORTED:
        assert env["data"]["failure_class"] == "mode_deferred"
        assert env["data"]["deferred_flags"] == [flag]
    else:  # paramiko present: the flag is honoured, the transfer just fails later
        assert env["exit_code"] in {
            exit_codes.EXIT_NETWORK,
            exit_codes.EXIT_AUTH,
            exit_codes.EXIT_NOT_FOUND,
            exit_codes.EXIT_REMOTE_ERROR,
            exit_codes.EXIT_PASSWORD_REQUIRED,
            exit_codes.EXIT_HOST_KEY,
        }


# -----------------------------------------------------------------------------
# GAP-003: --overwrite / refuse-to-clobber
#
# The real probe is driven end to end in the live suite; these cases pin the
# contract without a host by stubbing the probe and calling the guard directly.
# -----------------------------------------------------------------------------


def _guard_args(**overrides) -> object:
    """A Namespace shaped like the parser output the guard consumes."""
    import argparse

    base = dict(
        alias="pai", local_path="/tmp/f.bin", remote_path="/tmp/dest.bin",
        recursive=False, resume=False, delete=False, overwrite=False,
        transport="scp", timeout=30, ssh_config=None, accept_new_hostkey=False,
        port=None, identity=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_existing_target_without_overwrite_is_168_and_writes_nothing() -> None:
    """The refusal is the whole point: nothing may be spawned after it."""
    upload = load_script("ssh_upload")
    args = _guard_args()

    def probe(_path: str):
        return transfer.REMOTE_PATH_FILE, 0, ""

    env = upload._overwrite_guard(args, probe, transport="scp")
    assert env is not None
    assert env["success"] is False
    assert env["exit_code"] == 168
    assert env["data"]["failure_class"] == "remote_path_exists"
    assert env["data"]["remote_path_state"] == transfer.REMOTE_PATH_FILE
    assert env["data"]["requires"] == "--overwrite"
    assert env["data"]["bytes_transferred"] == 0
    # Still the transfer family's 160s block.
    assert 160 <= env["exit_code"] < 170


def test_overwrite_flag_allows_the_clobber() -> None:
    upload = load_script("ssh_upload")
    args = _guard_args(overwrite=True)

    def probe(_path: str):  # pragma: no cover - must not be called
        raise AssertionError("the probe must be skipped when --overwrite is given")

    assert upload._overwrite_guard(args, probe, transport="scp") is None


@pytest.mark.parametrize("flag", ["resume", "delete"])
def test_resume_and_delete_imply_overwrite(flag: str) -> None:
    upload = load_script("ssh_upload")
    args = _guard_args(**{flag: True, "recursive": flag == "delete"})

    def probe(_path: str):  # pragma: no cover - must not be called
        raise AssertionError("--resume/--delete imply overwrite")

    assert upload._overwrite_guard(args, probe, transport="sftp") is None


def test_recursive_upload_never_probes_the_destination() -> None:
    """An existing remote *directory* is normal for --recursive (GAP-003)."""
    upload = load_script("ssh_upload")
    args = _guard_args(recursive=True)

    def probe(_path: str):  # pragma: no cover - must not be called
        raise AssertionError("recursive uploads copy into the directory")

    assert upload._overwrite_guard(args, probe, transport="scp") is None


def test_absent_target_is_allowed() -> None:
    upload = load_script("ssh_upload")
    args = _guard_args()

    def probe(_path: str):
        return transfer.REMOTE_PATH_ABSENT, 0, ""

    assert upload._overwrite_guard(args, probe, transport="scp") is None


def test_unanswerable_probe_fails_closed() -> None:
    """Cannot tell -> do not write. The alternative is the silent clobber."""
    upload = load_script("ssh_upload")
    args = _guard_args()

    def probe(_path: str):
        return transfer.REMOTE_PATH_UNKNOWN, 0, ""

    env = upload._overwrite_guard(args, probe, transport="scp")
    assert env is not None
    assert env["success"] is False
    assert env["data"]["failure_class"] == "transfer_io"
    assert env["data"]["remote_path_state"] == "unknown"
    assert "--overwrite" in env["stderr"]


def test_unreachable_host_is_not_blamed_on_the_destination() -> None:
    """An ssh client failure keeps its own class instead of claiming 168."""
    upload = load_script("ssh_upload")
    args = _guard_args()

    def probe(_path: str):
        return (
            transfer.REMOTE_PATH_UNKNOWN, 255,
            "ssh: connect to host pai port 22: Connection refused",
        )

    env = upload._overwrite_guard(args, probe, transport="scp")
    assert env is not None
    assert env["data"]["failure_class"] == "network"
    assert env["exit_code"] == exit_codes.EXIT_NETWORK
