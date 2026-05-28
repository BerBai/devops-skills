"""Unit tests for ssh-guarded ``lib/request_artifact.py`` — 16+ cases per D12.

Covers:
- load: valid, bad version, missing field, corrupt JSON
- save: atomic write + mode 0o600 + no leftover tmp
- validate: happy x 4 ops, expired, server mismatch, sandbox refusal, denylist refusal
- compute_risk: 4 ops
- allocate_request_id: happy + collision retry exhaustion
- parse_expires_in: defaults + floor + cap + bad format
- save_result: with + without redactor
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "scripts"
)

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
from lib._errors import ArtifactError as _ArtifactError  # noqa: E402, I001
from lib.redact import Redactor as _Redactor  # noqa: E402, I001
from lib.request_artifact import (  # noqa: E402, I001
    RequestArtifact as _RequestArtifact,
)
from lib.request_artifact import (  # noqa: E402, I001
    RiskSummary as _RiskSummary,
)
from lib.request_artifact import (  # noqa: E402, I001
    allocate_request_id as _allocate_request_id,
)
from lib.request_artifact import (  # noqa: E402, I001
    compute_risk as _compute_risk,
)
from lib.request_artifact import (  # noqa: E402, I001
    iso_utc_now as _iso_utc_now,
)
from lib.request_artifact import (  # noqa: E402, I001
    iso_utc_offset as _iso_utc_offset,
)
from lib.request_artifact import (  # noqa: E402, I001
    parse_expires_in as _parse_expires_in,
)
from lib.request_artifact import (  # noqa: E402, I001
    save_result as _save_result,
)
import lib.request_artifact as _ra_mod  # noqa: E402, I001

ArtifactError = _ArtifactError
Redactor = _Redactor
RequestArtifact = _RequestArtifact
RiskSummary = _RiskSummary
allocate_request_id = _allocate_request_id
compute_risk = _compute_risk
iso_utc_now = _iso_utc_now
iso_utc_offset = _iso_utc_offset
parse_expires_in = _parse_expires_in
save_result = _save_result
ra = _ra_mod

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))

PROJECT_ID = "myproj"
SANDBOX_ROOT = f"~/workspace/{PROJECT_ID}"

EXPECTED_ARTIFACT_MODE = 0o600
DEFAULT_EXPIRES_SECONDS = 3600
SEVEN_DAYS_SECONDS = 7 * 24 * 3600
THREE_RETRIES = 3


def _now_plus(seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_command_art(**overrides: Any) -> RequestArtifact:
    payload = {"command": "ls", "workdir": f"{SANDBOX_ROOT}/scratch"}
    return RequestArtifact(
        version=1,
        request_id="command-abcd1234",
        operation=overrides.get("operation", "command"),
        server=overrides.get("server", "pai"),
        reason=overrides.get("reason", "test"),
        created_at=iso_utc_now(),
        expires_at=overrides.get("expires_at", _now_plus(3600)),
        payload=overrides.get("payload", payload),
        risk_summary=overrides.get(
            "risk_summary",
            RiskSummary(
                irreversible=False, scope="single-file", blast_radius="testing"
            ),
        ),
    )


# --- parse_expires_in ------------------------------------------------------


def test_parse_expires_in_default_is_1h() -> None:
    assert parse_expires_in(None) == DEFAULT_EXPIRES_SECONDS


def test_parse_expires_in_30m_is_1800() -> None:
    assert parse_expires_in("30m") == 30 * 60


def test_parse_expires_in_2h_and_1d() -> None:
    assert parse_expires_in("2h") == 2 * 3600
    assert parse_expires_in("1d") == 86400


def test_parse_expires_in_above_7d_is_rejected() -> None:
    with pytest.raises(ArtifactError) as exc_info:
        parse_expires_in("8d")
    assert exc_info.value.exit_code == 34
    assert exc_info.value.failure_class == "expiry_invalid"


def test_parse_expires_in_below_60s_is_rejected() -> None:
    with pytest.raises(ArtifactError):
        parse_expires_in("30s")  # 's' unit not allowed


def test_parse_expires_in_bad_format_rejected() -> None:
    with pytest.raises(ArtifactError):
        parse_expires_in("abc")


def test_parse_expires_in_at_7d_cap_allowed() -> None:
    assert parse_expires_in("7d") == SEVEN_DAYS_SECONDS


# --- save / load roundtrip -------------------------------------------------


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    art = _make_command_art()
    p = tmp_path / "command-abcd1234.json"
    art.save(p)
    loaded = RequestArtifact.load(p)
    assert loaded.operation == "command"
    assert loaded.server == art.server
    assert loaded.payload == art.payload
    assert loaded.risk_summary == art.risk_summary


def test_save_writes_mode_0o600(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("file mode not meaningful on Windows")
    art = _make_command_art()
    p = tmp_path / "command-mode.json"
    art.save(p)
    mode = p.stat().st_mode & 0o777
    assert mode == EXPECTED_ARTIFACT_MODE


def test_save_leaves_no_tmp_file_after_success(tmp_path: Path) -> None:
    art = _make_command_art()
    p = tmp_path / "command-tmp.json"
    art.save(p)
    leftover = [child.name for child in tmp_path.iterdir() if ".tmp" in child.name]
    assert leftover == []


def test_load_rejects_bad_version(tmp_path: Path) -> None:
    p = tmp_path / "command-vbad.json"
    art_dict = _make_command_art().to_dict()
    art_dict["version"] = 99
    p.write_text(json.dumps(art_dict))
    with pytest.raises(ArtifactError) as exc_info:
        RequestArtifact.load(p)
    assert exc_info.value.exit_code == 53
    assert exc_info.value.failure_class == "artifact_invalid"


def test_load_rejects_missing_field(tmp_path: Path) -> None:
    p = tmp_path / "command-miss.json"
    art_dict = _make_command_art().to_dict()
    del art_dict["server"]
    p.write_text(json.dumps(art_dict))
    with pytest.raises(ArtifactError) as exc_info:
        RequestArtifact.load(p)
    assert "missing fields" in exc_info.value.message


def test_load_rejects_corrupt_json(tmp_path: Path) -> None:
    p = tmp_path / "command-corr.json"
    p.write_text("{not json")
    with pytest.raises(ArtifactError):
        RequestArtifact.load(p)


def test_load_rejects_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "ghost.json"
    with pytest.raises(ArtifactError):
        RequestArtifact.load(p)


# --- validate --------------------------------------------------------------


def test_validate_happy_command() -> None:
    _make_command_art().validate(project_id=PROJECT_ID)


def test_validate_happy_upload() -> None:
    art = _make_command_art(
        operation="upload",
        payload={
            "local_path": "/tmp/x",
            "local_sha256": "00" * 32,
            "local_size": 1,
            "remote_path": f"{SANDBOX_ROOT}/x",
            "overwrite": False,
            "sensitive_local": False,
        },
        risk_summary=RiskSummary(
            irreversible=False, scope="single-file", blast_radius="up"
        ),
    )
    art.validate(project_id=PROJECT_ID)


def test_validate_happy_mkdir() -> None:
    art = _make_command_art(
        operation="mkdir",
        payload={
            "remote_path": f"{SANDBOX_ROOT}/staging",
            "mode": "0755",
            "parents": True,
        },
        risk_summary=RiskSummary(
            irreversible=False, scope="single-file", blast_radius="mkd"
        ),
    )
    art.validate(project_id=PROJECT_ID)


def test_validate_happy_delete() -> None:
    art = _make_command_art(
        operation="delete",
        payload={
            "remote_path": f"{SANDBOX_ROOT}/stale.log",
            "recursive": False,
            "is_directory_pre_check": False,
        },
        risk_summary=RiskSummary(
            irreversible=True, scope="single-file", blast_radius="del"
        ),
    )
    art.validate(project_id=PROJECT_ID)


def test_validate_expired_rejected() -> None:
    art = _make_command_art(expires_at=_now_plus(-60))
    with pytest.raises(ArtifactError) as exc_info:
        art.validate(project_id=PROJECT_ID)
    assert exc_info.value.failure_class == "artifact_expired"
    assert exc_info.value.exit_code == 53


def test_validate_server_mismatch_rejected() -> None:
    art = _make_command_art(server="other-alias")
    with pytest.raises(ArtifactError) as exc_info:
        art.validate(project_id=PROJECT_ID, expected_server="pai")
    assert "server mismatch" in exc_info.value.message
    assert exc_info.value.exit_code == 53


def test_validate_sandbox_refusal() -> None:
    art = _make_command_art(payload={"command": "ls", "workdir": "/etc"})
    with pytest.raises(ArtifactError) as exc_info:
        art.validate(project_id=PROJECT_ID)
    assert exc_info.value.failure_class == "workdir_outside_sandbox"
    assert exc_info.value.exit_code == 50


def test_validate_denylist_refusal() -> None:
    art = _make_command_art(
        operation="delete",
        payload={
            "remote_path": f"{SANDBOX_ROOT}/.env",
            "recursive": False,
            "is_directory_pre_check": False,
        },
        risk_summary=RiskSummary(
            irreversible=True, scope="single-file", blast_radius="del"
        ),
    )
    with pytest.raises(ArtifactError) as exc_info:
        art.validate(project_id=PROJECT_ID)
    assert exc_info.value.failure_class == "path_in_denylist"
    assert exc_info.value.exit_code == 51


# --- compute_risk ---------------------------------------------------------


def test_compute_risk_command_with_rm_is_irreversible() -> None:
    rs = compute_risk("command", {"command": "rm -rf /tmp/x"}, "pai")
    assert rs.irreversible
    assert rs.scope == "tree"


def test_compute_risk_command_with_sudo_is_host_scope() -> None:
    rs = compute_risk("command", {"command": "sudo systemctl restart x"}, "pai")
    assert rs.irreversible
    assert rs.scope == "host"


def test_compute_risk_command_with_systemctl_is_service() -> None:
    rs = compute_risk("command", {"command": "systemctl status nginx"}, "pai")
    assert rs.irreversible
    assert rs.scope == "service"


def test_compute_risk_command_with_redirect_is_irreversible() -> None:
    rs = compute_risk("command", {"command": "cat foo > bar"}, "pai")
    assert rs.irreversible


def test_compute_risk_command_safe_is_single_file_and_reversible() -> None:
    rs = compute_risk("command", {"command": "ls -la"}, "pai")
    assert not rs.irreversible
    assert rs.scope == "single-file"


def test_compute_risk_upload_overwrite_true_is_irreversible() -> None:
    rs = compute_risk(
        "upload",
        {"remote_path": "~/workspace/x/file", "overwrite": True, "local_size": 42},
        "pai",
    )
    assert rs.irreversible


def test_compute_risk_upload_overwrite_false_is_reversible() -> None:
    rs = compute_risk(
        "upload",
        {"remote_path": "~/workspace/x/file", "overwrite": False, "local_size": 42},
        "pai",
    )
    assert not rs.irreversible


def test_compute_risk_mkdir_is_always_reversible() -> None:
    rs = compute_risk(
        "mkdir", {"remote_path": "~/workspace/x/staging", "mode": "0755"}, "pai"
    )
    assert not rs.irreversible
    assert rs.scope == "single-file"


def test_compute_risk_delete_is_always_irreversible() -> None:
    rs = compute_risk(
        "delete", {"remote_path": "~/workspace/x/file", "recursive": False}, "pai"
    )
    assert rs.irreversible
    assert rs.scope == "single-file"


def test_compute_risk_delete_recursive_is_tree() -> None:
    rs = compute_risk(
        "delete", {"remote_path": "~/workspace/x/dir", "recursive": True}, "pai"
    )
    assert rs.irreversible
    assert rs.scope == "tree"


# --- allocate_request_id --------------------------------------------------


def test_allocate_request_id_creates_placeholder(tmp_path: Path) -> None:
    rid = allocate_request_id("command", tmp_path)
    assert rid.startswith("command-")
    assert (tmp_path / f"{rid}.json").exists()


def test_allocate_request_id_unknown_operation_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError):
        allocate_request_id("frobnicate", tmp_path)


def test_allocate_request_id_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_hex = "deadbeef"

    class _FixedUUID:
        hex = fixed_hex + "00000000000000000000000000"

    monkeypatch.setattr(uuid, "uuid4", _FixedUUID)
    # First allocation succeeds; second collides; ... after 3 → ArtifactError
    rid1 = allocate_request_id("command", tmp_path)
    assert rid1 == f"command-{fixed_hex}"
    with pytest.raises(ArtifactError) as exc_info:
        allocate_request_id("command", tmp_path)
    assert exc_info.value.exit_code == 1


# --- save_result ----------------------------------------------------------


def test_save_result_applies_redactor(tmp_path: Path) -> None:
    art = _make_command_art()
    payload = {
        "executed_at": iso_utc_now(),
        "exit_code": 0,
        "duration_ms": 12,
        "stdout_redacted": "secret AKIAABCDEFGHIJKLMNOP done",
        "stderr_redacted": "",
        "transport": "ssh_execute",
        "transport_exit_code": 0,
    }
    result_path = save_result(art, payload, Redactor(None), out_dir=tmp_path)
    written = json.loads(result_path.read_text())
    assert "[REDACTED-AWS-ACCESS-KEY]" in written["stdout_redacted"]
    assert written["request_id"] == art.request_id
    assert written["version"] == 1


def test_save_result_skips_redaction_when_none(tmp_path: Path) -> None:
    art = _make_command_art()
    payload = {
        "executed_at": iso_utc_now(),
        "exit_code": 0,
        "duration_ms": 12,
        "stdout_redacted": "raw secret AKIAABCDEFGHIJKLMNOP",
        "stderr_redacted": "",
        "transport": "ssh_execute",
        "transport_exit_code": 0,
    }
    result_path = save_result(art, payload, redactor=None, out_dir=tmp_path)
    written = json.loads(result_path.read_text())
    assert "AKIAABCDEFGHIJKLMNOP" in written["stdout_redacted"]


# --- iso helpers ----------------------------------------------------------


def test_iso_utc_offset_advances_time() -> None:
    a = iso_utc_now()
    b = iso_utc_offset(60)
    assert a <= b
