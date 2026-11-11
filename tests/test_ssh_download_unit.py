"""Local unit tests for ``ssh_download.py`` guards and envelope contract.

The 1.4.0 sync deleted ``test_ssh_download_unit.py`` (17 cases). These are the
host-free ones: argument guards, ``--delete`` confirmations, the
``--transport sftp`` dependency check, and the "scp cannot do this" refusals.

The resume/integrity state machine needs a real SFTP peer and stays with
``test_ssh_download_integration.py``; the lib-level pieces are covered by
``test_ssh_core_transfers.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
DOWNLOAD = SCRIPTS / "ssh_download.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib import exit_codes  # noqa: E402

from tests.ssh_core.helpers import load_script  # noqa: E402

EXIT_OK = 0
EXIT_INVALID_ARGUMENTS = exit_codes.EXIT_INVALID_ARGUMENTS
EXIT_DEPENDENCY_MISSING = exit_codes.EXIT_DEPENDENCY_MISSING
EXIT_MODE_UNSUPPORTED = exit_codes.EXIT_MODE_UNSUPPORTED


def _run(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DOWNLOAD), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _envelope(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


def test_help_exits_zero() -> None:
    proc = _run("--help")
    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()
    for flag in ("--resume", "--recursive", "--bwlimit", "--transport"):
        assert flag in proc.stdout


def test_local_directory_without_recursive_is_refused(tmp_path: Path) -> None:
    proc = _run("pai", "/remote/x", str(tmp_path), "--json")
    assert proc.returncode == exit_codes.exit_code_for("local_path_is_directory")
    env = _envelope(proc)
    assert env["data"]["failure_class"] == "local_path_is_directory"
    assert "--recursive" in env["stderr"]


def test_delete_without_yes_is_refused(tmp_path: Path) -> None:
    proc = _run("pai", "/remote/x", str(tmp_path / "out"), "--delete", "--recursive", "--json")
    assert proc.returncode == exit_codes.exit_code_for("confirmation_required")
    env = _envelope(proc)
    assert env["data"]["failure_class"] == "confirmation_required"
    assert env["data"]["requires"] == "--yes"
    assert env["data"]["mirror"] is True


def test_delete_without_recursive_is_invalid_arguments(tmp_path: Path) -> None:
    proc = _run("pai", "/remote/x", str(tmp_path / "out"), "--delete", "--yes", "--json")
    assert proc.returncode == EXIT_INVALID_ARGUMENTS
    assert _envelope(proc)["data"]["failure_class"] == "invalid_arguments"


def test_explicit_sftp_without_paramiko_is_a_dependency_error() -> None:
    """``--transport sftp`` on a host without paramiko must say so by name."""
    try:
        import paramiko  # noqa: F401
    except Exception:
        pass
    else:
        pytest.skip("paramiko is installed here, so this refusal cannot trigger")

    proc = _run("pai", "/remote/x", "/tmp/out", "--transport", "sftp", "--json")
    assert proc.returncode == EXIT_DEPENDENCY_MISSING
    env = _envelope(proc)
    assert env["data"]["failure_class"] == "dependency_missing"
    assert "paramiko" in env["stderr"]


def test_openssh_backend_refuses_sftp_only_flags(tmp_path: Path) -> None:
    """The OpenSSH backend cannot resume or throttle; it must not pretend to."""
    proc = _run(
        "pai", "/remote/x", str(tmp_path / "out.bin"),
        "--transport", "openssh", "--resume", "--json",
    )
    env = _envelope(proc)
    assert env["success"] is False
    assert env["exit_code"] == EXIT_MODE_UNSUPPORTED
    assert env["data"]["failure_class"] == "mode_deferred"
    assert env["data"]["deferred_flags"] == ["--resume"]
    assert env["data"]["deferred_transport"] == "sftp"


def test_openssh_backend_refuses_bwlimit(tmp_path: Path) -> None:
    proc = _run(
        "pai", "/remote/x", str(tmp_path / "out.bin"),
        "--transport", "openssh", "--bwlimit", "50", "--json",
    )
    env = _envelope(proc)
    assert env["exit_code"] == EXIT_MODE_UNSUPPORTED
    assert env["data"]["deferred_flags"] == ["--bwlimit"]


def test_bad_argv_emits_json_not_a_traceback() -> None:
    proc = _run("pai", "--json")
    assert proc.returncode == EXIT_INVALID_ARGUMENTS
    env = _envelope(proc)
    assert env["data"]["failure_class"] == "invalid_arguments"
    assert "usage" in env["data"]


# -----------------------------------------------------------------------------
# BUG-006: the tar fallback's layout and its counts
#
# The end-to-end layout is asserted by the live suite; here the argv and the
# counter are pinned directly, so a regression is caught with no host at all.
# -----------------------------------------------------------------------------


def test_tar_extract_strips_the_leaf_level_when_recursive(
    tmp_path: Path,
) -> None:
    """``--strip-components=1`` is what removes the extra ``out/<leaf>/``."""
    download = load_script("ssh_download")
    argv = download._tar_extract_argv(tmp_path, strip=True)
    assert argv[0] == "tar"
    assert "--strip-components=1" in argv
    # `-C <dir>` must still come before the flag for both GNU tar and bsdtar.
    assert argv.index("-C") < argv.index("--strip-components=1")


def test_tar_extract_keeps_the_entry_name_for_a_single_file(
    tmp_path: Path,
) -> None:
    """Regression constraint: the single-file layout must not change."""
    download = load_script("ssh_download")
    assert "--strip-components=1" not in download._tar_extract_argv(
        tmp_path, strip=False
    )


def test_tar_counter_counts_files_and_payload_bytes(tmp_path: Path) -> None:
    """The numbers in the envelope come from the archive, not from a stub."""
    import io
    import tarfile

    download = load_script("ssh_download")
    payload = {"a.txt": b"AAA\n", "sub/b.txt": b"BBB\n"}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in payload.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        directory = tarfile.TarInfo("sub")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
    blob = buffer.getvalue()

    counter = download.TarEntryCounter()
    for offset in range(0, len(blob), 137):  # odd chunks: incremental parsing
        counter.feed(blob[offset:offset + 137])
    assert counter.files == 2
    assert counter.bytes == sum(len(data) for data in payload.values())
    assert counter.stopped is False


def test_tar_counter_never_raises_on_a_non_archive_stream() -> None:
    download = load_script("ssh_download")
    counter = download.TarEntryCounter()
    counter.feed(b"not a tar archive at all" * 100)
    assert counter.files == 0
    assert counter.stopped is True
