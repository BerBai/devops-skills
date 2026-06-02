"""Unit tests for ssh-guarded/scripts/redact_check.py v1.0."""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
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
SCRIPT_PATH = SCRIPTS_DIR / "redact_check.py"

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import redact_check  # noqa: E402, I001

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


def _run(
    argv: list[str],
    stdin: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        rc = redact_check.main(argv)
        captured = capsys.readouterr()
    finally:
        sys.stdin = old_stdin
    return rc, captured.out, captured.err


def _catalog(tmp_path: Path, patterns: list[dict[str, str]], *, version: Any = 1) -> Path:
    path = tmp_path / "redact.json"
    path.write_text(json.dumps({"version": version, "patterns": patterns}), encoding="utf-8")
    return path


def _one_pattern_catalog(tmp_path: Path) -> Path:
    return _catalog(
        tmp_path,
        [
            {
                "kind": "internal_api_key",
                "regex": r"MYCO-[A-Z0-9]{8}",
                "replace_with": "[REDACTED-INTERNAL]",
            }
        ],
    )


def test_help_surface_lists_four_flags() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "--catalog" in proc.stdout
    assert "--report-only" in proc.stdout
    assert "--json" in proc.stdout
    assert "--emit-redacted" in proc.stdout


@pytest.mark.parametrize(
    ("secret", "replacement", "kind"),
    [
        ("AKIAABCDEFGHIJKLMNOP", "[REDACTED-AWS-ACCESS-KEY]", "aws_access_key"),
        ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij", "[REDACTED-GH-TOKEN]", "github_token"),
        (
            "eyJhbGciOiJIUzI1.eyJzdWIiOjEyMzQ1.signaturepayload",
            "[REDACTED-JWT]",
            "jwt",
        ),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "[REDACTED-PRIVATE-KEY-HEADER]", "rsa_private"),
    ],
)
def test_builtin_patterns_redact(
    secret: str,
    replacement: str,
    kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out, err = _run([], f"value={secret}\n", capsys)
    assert rc == 0
    assert replacement in out
    assert secret not in out
    assert f"{kind}=1" in err


def test_builtin_no_match_passthrough(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = _run([], "nothing secret here\n", capsys)
    assert rc == 0
    assert out == "nothing secret here\n"
    assert err == "redact_check: 0 hits\n"


def test_catalog_override_replaces_builtin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = _one_pattern_catalog(tmp_path)
    rc, out, err = _run(
        ["--catalog", str(catalog)],
        "AKIAABCDEFGHIJKLMNOP MYCO-ABCDEFGH\n",
        capsys,
    )
    assert rc == 0
    assert "AKIAABCDEFGHIJKLMNOP" in out
    assert "[REDACTED-INTERNAL]" in out
    assert "internal_api_key=1" in err


def test_catalog_missing_returns_70(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = _run(["--catalog", "/no/such/redact.json"], "x\n", capsys)
    assert rc == 70
    assert out == ""
    assert "failure_class=catalog_load_failed" in err


def test_catalog_bad_json_returns_71(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = tmp_path / "redact.json"
    catalog.write_text("not json {{{", encoding="utf-8")
    rc, out, err = _run(["--catalog", str(catalog)], "x\n", capsys)
    assert rc == 71
    assert out == ""
    assert "failure_class=catalog_schema_invalid" in err


def test_catalog_bad_schema_returns_71(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = _catalog(tmp_path, [], version=2)
    rc, out, err = _run(["--catalog", str(catalog)], "x\n", capsys)
    assert rc == 71
    assert out == ""
    assert "catalog_schema_invalid" in err


def test_catalog_entry_missing_field_returns_71(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = tmp_path / "redact.json"
    catalog.write_text(
        json.dumps({"version": 1, "patterns": [{"kind": "x", "regex": "secret"}]}),
        encoding="utf-8",
    )
    rc, _, err = _run(["--catalog", str(catalog)], "secret\n", capsys)
    assert rc == 71
    assert "replace_with" in err


def test_catalog_bad_regex_returns_72_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = _catalog(
        tmp_path,
        [{"kind": "bad", "regex": "[unclosed", "replace_with": "X"}],
    )
    rc, out, err = _run(["--catalog", str(catalog), "--json"], "x\n", capsys)
    env = json.loads(out)
    assert rc == 72
    assert err == ""
    assert env["exit_code"] == 72
    assert env["data"]["failure_class"] == "regex_compile_failed"
    assert env["data"]["offending_kind"] == "bad"


def test_json_success_omits_redacted_text_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out, err = _run(["--json"], "token=AKIAABCDEFGHIJKLMNOP\n", capsys)
    env = json.loads(out)
    assert rc == 0
    assert err == ""
    assert env["success"] is True
    assert env["data"]["total_hits"] == 1
    assert env["data"]["hits"][0]["line"] == 1
    assert env["data"]["hits"][0]["match_len"] == 20
    assert "redacted_text" not in env["data"]


def test_json_emit_redacted_includes_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, _ = _run(["--json", "--emit-redacted"], "token=AKIAABCDEFGHIJKLMNOP\n", capsys)
    env = json.loads(out)
    assert rc == 0
    assert "[REDACTED-AWS-ACCESS-KEY]" in env["data"]["redacted_text"]


def test_report_only_suppresses_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = _run(["--report-only"], "token=AKIAABCDEFGHIJKLMNOP\n", capsys)
    assert rc == 0
    assert out == ""
    assert "redact_check: 1 hits" in err


def test_multiline_hits_have_one_indexed_lines(capsys: pytest.CaptureFixture[str]) -> None:
    text = "\n".join(
        [
            "line 1",
            "AKIAABCDEFGHIJKLMNOP",
            "line 3",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
        ]
    )
    rc, out, _ = _run(["--json"], text + "\n", capsys)
    env = json.loads(out)
    assert rc == 0
    assert [hit["line"] for hit in env["data"]["hits"]] == [2, 4]


def test_empty_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = _run([], "", capsys)
    assert rc == 0
    assert out == ""
    assert err == "redact_check: 0 hits\n"


def test_custom_catalog_json_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    catalog = _one_pattern_catalog(tmp_path)
    rc, out, _ = _run(
        ["--catalog", str(catalog), "--json"],
        "MYCO-ABCDEFGH MYCO-IJKLMNOP\n",
        capsys,
    )
    env = json.loads(out)
    assert rc == 0
    assert env["data"]["summary"] == {"internal_api_key": 2}
    assert env["data"]["total_hits"] == 2


def test_binary_stdin_returns_generic() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--json"],
        input=b"\xff\xfe\xfd",
        capture_output=True,
        check=False,
    )
    env = json.loads(proc.stdout.decode("utf-8"))
    assert proc.returncode == 1
    assert env["data"]["failure_class"] == "generic"
