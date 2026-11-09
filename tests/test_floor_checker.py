"""Self-test for tools/check_python_floor.py.

The checker is the thing that is supposed to catch a floor violation, so it
needs its own test: a rule that silently stops firing is worse than no rule.
Each case writes a tiny module and asserts what the checker reports.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "tools" / "check_python_floor.py"

BAD_CASES = {
    "removeprefix": 'print("abc".removeprefix("a"))\n',
    "removesuffix": 'print("abc".removesuffix("c"))\n',
    "zip_strict": "print(list(zip([1], [2], strict=True)))\n",
    "dataclass_slots": (
        "from dataclasses import dataclass\n"
        "@dataclass(slots=True)\n"
        "class A:\n"
        "    x: int\n"
    ),
    "tempdir_ignore_cleanup": (
        "import tempfile\n"
        "with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:\n"
        "    print(d)\n"
    ),
    "write_text_newline": 'from pathlib import Path\nPath("x").write_text("y", newline="")\n',
}

GOOD_CASES = {
    "dataclass_plain": (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class A:\n"
        "    x: int\n"
    ),
    "literal_in_docstring": (
        '"""We avoid Path.write_text(newline=) because it is 3.10+."""\n'
        "X = 1\n"
    ),
    "literal_in_comment": (
        "# zip(strict=True) is 3.10+, so we do not use it.\n"
        "X = 1\n"
    ),
    "floor_ok_marker_same_line": (
        "import tempfile\n"
        "tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # floor-ok\n"
    ),
    "floor_ok_marker_above": (
        "import tempfile\n"
        "# floor-ok: guarded by sys.version_info\n"
        "tempfile.TemporaryDirectory(\n"
        "    ignore_cleanup_errors=True\n"
        ")\n"
    ),
    "write_text_without_newline": (
        'from pathlib import Path\nPath("x").write_text("y", encoding="utf-8")\n'
    ),
}


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root), "--quiet"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _write(tmp_path: Path, name: str, body: str) -> Path:
    target = tmp_path / f"{name}.py"
    target.write_text(body, encoding="utf-8")
    return target


def test_checker_flags_every_forbidden_construct(tmp_path: Path) -> None:
    for name, body in BAD_CASES.items():
        _write(tmp_path, name, body)
    result = _run(tmp_path)
    assert result.returncode == 1
    for name in BAD_CASES:
        assert f"{name}.py" in result.stderr, (
            f"{name} was not reported; stderr={result.stderr!r}"
        )


def test_checker_accepts_the_allowed_forms(tmp_path: Path) -> None:
    for name, body in GOOD_CASES.items():
        _write(tmp_path, name, body)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_checker_catches_a_38_syntax_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "match_stmt",
        "match 1:\n"
        "    case 1:\n"
        "        pass\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "syntax needs >3.8" in result.stderr


def test_checker_is_clean_on_the_real_tree() -> None:
    """The repository must satisfy its own floor claim.

    The default root set includes ``scripts/`` and ``tools/``: those were
    outside the scan until ``scripts/sync_vendored.py`` shipped a 3.10-only
    ``Path.write_text(newline=)`` that neither CI nor this test could see.
    """
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr


def test_default_scan_covers_scripts_and_tools() -> None:
    """A floor violation outside the plugin tree must still fail the gate."""
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--quiet"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.stderr == "", result.stderr
    # The default run must have scanned more than the plugin tree alone.
    summary = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    scanned = int(summary.stdout.split()[1])
    plugins_only = subprocess.run(
        [sys.executable, str(CHECKER), "--root", "plugins"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    plugins_scanned = int(plugins_only.stdout.split()[1])
    assert scanned > plugins_scanned


def test_missing_root_is_an_argument_error(tmp_path: Path) -> None:
    result = _run(tmp_path / "nope")
    assert result.returncode == 2
