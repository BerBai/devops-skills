"""Unit tests for ssh-guarded ``lib/redact.py`` — 8 cases per design.md D12."""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

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
from lib.redact import _DEFAULT_PATTERNS as _DEFAULTS  # noqa: E402, I001
from lib.redact import Redactor as _Redactor  # noqa: E402

Redactor = _Redactor
DEFAULT_PATTERN_COUNT = len(_DEFAULTS)

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))

# --- default catalog ------------------------------------------------------


def test_default_catalog_loaded_when_path_none() -> None:
    r = Redactor(None)
    # Every documented default pattern compiles and survives init.
    assert len(r._compiled) == DEFAULT_PATTERN_COUNT  # noqa: SLF001


def test_default_catalog_used_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    r = Redactor(missing)
    assert len(r._compiled) == DEFAULT_PATTERN_COUNT  # noqa: SLF001


def test_corrupt_json_falls_back_to_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "redact.json"
    bad.write_text("not json {{{")
    r = Redactor(bad)
    assert len(r._compiled) == DEFAULT_PATTERN_COUNT  # noqa: SLF001
    # Warn lands on stderr (logging-guidelines.md).
    assert "not valid JSON" in capsys.readouterr().err


def test_custom_catalog_overrides_default(tmp_path: Path) -> None:
    cat = tmp_path / "redact.json"
    cat.write_text(
        json.dumps(
            {
                "version": 1,
                "patterns": [
                    {
                        "kind": "internal_api_key",
                        "regex": r"MYCO-[A-Z0-9]{32}",
                        "replace_with": "[REDACTED-INTERNAL]",
                    }
                ],
            }
        )
    )
    r = Redactor(cat)
    # Custom catalog wholly replaces defaults (PRD/spec doesn't require merging).
    assert len(r._compiled) == 1  # noqa: SLF001
    text = "secret MYCO-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 here"
    out, hits = r.apply(text)
    assert "[REDACTED-INTERNAL]" in out
    assert len(hits) == 1
    assert hits[0]["kind"] == "internal_api_key"


def test_invalid_regex_skipped_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    cat = tmp_path / "redact.json"
    cat.write_text(
        json.dumps(
            {
                "version": 1,
                "patterns": [
                    {"kind": "bad", "regex": "[unclosed", "replace_with": "X"},
                    {
                        "kind": "good",
                        "regex": r"AKIA[0-9A-Z]{16}",
                        "replace_with": "[REDACTED-AWS-ACCESS-KEY]",
                    },
                ],
            }
        )
    )
    r = Redactor(cat)
    assert len(r._compiled) == 1  # noqa: SLF001
    assert "skipping bad pattern" in capsys.readouterr().err


# --- per-pattern matches --------------------------------------------------


def test_apply_redacts_aws_access_key() -> None:
    r = Redactor(None)
    out, hits = r.apply("token=AKIAABCDEFGHIJKLMNOP done")
    assert "[REDACTED-AWS-ACCESS-KEY]" in out
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert hits and hits[0]["kind"] == "aws_access_key"


def test_apply_redacts_jwt() -> None:
    r = Redactor(None)
    jwt = "eyJhbGciOiJIUzI1.eyJzdWIiOjEyMzQ1.signaturepayload"
    out, hits = r.apply(f"Authorization: Bearer {jwt}")
    assert "[REDACTED-JWT]" in out
    assert hits[0]["kind"] == "jwt"


def test_apply_redacts_private_key_header() -> None:
    """A truncated transcript (header only) still gets redacted."""
    r = Redactor(None)
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb64...\n"
    out, hits = r.apply(text)
    assert "[REDACTED-PRIVATE-KEY-HEADER]" in out
    assert hits and hits[0]["kind"] == "private_key_header"


def test_apply_redacts_a_whole_private_key_block() -> None:
    """The key body must not survive: only the header used to be replaced."""
    r = Redactor(None)
    text = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "SUPERSECRETBODYMATERIAL\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    out, hits = r.apply(text)
    assert out == "[REDACTED-PRIVATE-KEY-BLOCK]\n"
    assert "SUPERSECRETBODYMATERIAL" not in out
    assert hits and hits[0]["kind"] == "private_key_block"


def test_apply_empty_text_is_pass_through() -> None:
    r = Redactor(None)
    out, hits = r.apply("")
    assert out == ""
    assert hits == []


def test_apply_no_match_is_pass_through() -> None:
    r = Redactor(None)
    out, hits = r.apply("nothing secret here, just text")
    assert out == "nothing secret here, just text"
    assert hits == []
