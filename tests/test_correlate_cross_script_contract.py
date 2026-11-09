# ruff: noqa: PLR2004 — magic exit codes are the contract under test
"""Cross-script consistency: one ``partial_fetch`` rule for all three CLIs.

DEVOPS-BUG-012 reported the same failing input three incompatible ways:

    ====================  =======  =========  ==========================
    script                success  exit_code  data.failure_class
    ====================  =======  =========  ==========================
    tail_multi.py         false    41         fetcher_failed
    grep_across_sources   true     0          partial_fetch
    correlate             true     0          null
    ====================  =======  =========  ==========================

The rule now (``scripts/lib/envelope.py``, documented in ``SKILL.md``):

    Any source failed to fetch -> success=false, exit_code=41,
    data.failure_class="partial_fetch", data.failed_fetchers=[...];
    successfully fetched data still ships in ``data``.

These tests drive the real CLIs as subprocesses with a genuinely failing
source (a ``file://`` path that does not exist) plus a source that works, so
the three envelopes are compared as an orchestration layer would see them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "log-aggregator"
    / "skills"
    / "log-aggregator"
    / "scripts"
)
TAIL_MULTI = SCRIPTS_DIR / "tail_multi.py"
GREP = SCRIPTS_DIR / "grep_across_sources.py"
CORRELATE = SCRIPTS_DIR / "correlate.py"

PARTIAL_FETCH = "partial_fetch"
PARTIAL_FETCH_EXIT = 41


def _file_spec(path: Path) -> str:
    """``file://local/<path>`` for whichever OS the suite runs on."""
    return f"file://local/{path.as_posix()}"


def _run(script: Path, *args: str, timeout: int = 60) -> tuple[int, dict]:
    """Run a CLI with ``--json`` and return its exit code + final envelope."""
    proc = subprocess.run(
        [sys.executable, str(script), *args, "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, (
        f"{script.name} produced no stdout\nstderr={proc.stderr!r}"
    )
    try:
        envelope = json.loads(lines[-1])
    except json.JSONDecodeError as exc:  # pragma: no cover - failure detail
        pytest.fail(
            f"{script.name}: last stdout line is not an envelope: {exc}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    return proc.returncode, envelope


@pytest.fixture()
def sources(tmp_path: Path) -> tuple[str, str]:
    """``(good_spec, missing_spec)`` — one real file, one that cannot be read."""
    good = tmp_path / "app.log"
    good.write_text("INFO boot\nMATCH alpha\n", encoding="utf-8")
    missing = tmp_path / "gone.log"
    return _file_spec(good), _file_spec(missing)


def _one_failed_one_good(good: str, missing: str) -> dict[str, tuple[int, dict]]:
    """The same "one source is unreadable" input through all three CLIs."""
    return {
        "tail_multi": _run(
            TAIL_MULTI,
            "--sources", good, missing,
            "--no-default-filters",
        ),
        "grep_across_sources": _run(
            GREP,
            "--sources", good, missing,
            "--pattern", "MATCH",
        ),
        "correlate": _run(
            CORRELATE,
            "--sources", good, missing,
            "--anchor", "MATCH",
            "--window", "30s",
        ),
    }


def test_all_three_scripts_agree_on_partial_fetch(
    sources: tuple[str, str],
) -> None:
    good, missing = sources
    results = _one_failed_one_good(good, missing)

    for script, (rc, envelope) in results.items():
        assert rc == PARTIAL_FETCH_EXIT, f"{script} exit code: {rc}"
        assert envelope["exit_code"] == PARTIAL_FETCH_EXIT, script
        assert envelope["success"] is False, script
        assert envelope["data"]["failure_class"] == PARTIAL_FETCH, script
        failed = envelope["data"]["failed_fetchers"]
        assert len(failed) == 1, f"{script}: {failed}"
        assert failed[0]["source"].endswith("gone.log"), script
        assert failed[0]["error"], script


def test_partial_results_still_ship_in_data(sources: tuple[str, str]) -> None:
    """``success=false`` must not throw away what was successfully fetched."""
    good, missing = sources
    results = _one_failed_one_good(good, missing)

    _rc, tail_env = results["tail_multi"]
    assert tail_env["data"]["events_emitted"] >= 1

    _rc, grep_env = results["grep_across_sources"]
    assert grep_env["data"]["total_hits"] >= 1
    assert grep_env["data"]["per_source"][str(_good_tag(good))] >= 1

    _rc, corr_env = results["correlate"]
    assert corr_env["data"]["anchor"] is not None
    assert any(
        line["body"] == "MATCH alpha" for line in corr_env["data"]["window_lines"]
    )
    assert corr_env["data"]["summary"]["partial_fetch"] is True


def _good_tag(spec: str) -> str:
    """``file://local/<abs path>`` -> the ``source_tag`` the scripts emit."""
    payload = spec[len("file://local"):]
    return f"local{payload}"


def test_all_three_agree_when_every_source_fails(tmp_path: Path) -> None:
    """Total failure is the same class, not a second spelling."""
    missing_a = _file_spec(tmp_path / "gone-a.log")
    missing_b = _file_spec(tmp_path / "gone-b.log")
    results = {
        "tail_multi": _run(
            TAIL_MULTI, "--sources", missing_a, missing_b, "--no-default-filters"
        ),
        "grep_across_sources": _run(
            GREP, "--sources", missing_a, missing_b, "--pattern", "MATCH"
        ),
        "correlate": _run(
            CORRELATE, "--sources", missing_a, missing_b,
            "--anchor", "MATCH", "--window", "30s",
        ),
    }
    for script, (rc, envelope) in results.items():
        assert rc == PARTIAL_FETCH_EXIT, f"{script} exit code: {rc}"
        assert envelope["success"] is False, script
        assert envelope["data"]["failure_class"] == PARTIAL_FETCH, script
        assert len(envelope["data"]["failed_fetchers"]) == 2, script


def test_clean_input_is_still_a_clean_success(sources: tuple[str, str]) -> None:
    """The rule must not turn an all-good run into a failure."""
    good, _missing = sources
    tail_rc, tail_env = _run(
        TAIL_MULTI, "--sources", good, "--no-default-filters"
    )
    assert (tail_rc, tail_env["success"]) == (0, True)
    assert tail_env["data"]["failure_class"] is None
    assert tail_env["data"]["failed_fetchers"] == []

    grep_rc, grep_env = _run(GREP, "--sources", good, "--pattern", "MATCH")
    assert (grep_rc, grep_env["success"]) == (0, True)
    assert grep_env["data"]["failure_class"] is None

    corr_rc, corr_env = _run(
        CORRELATE, "--sources", good, "--anchor", "MATCH", "--window", "30s"
    )
    assert (corr_rc, corr_env["success"]) == (0, True)
    assert corr_env["data"]["failure_class"] is None


def test_human_mode_partial_fetch_also_exits_41(sources: tuple[str, str]) -> None:
    """summary/raw formats print no envelope; the exit code must carry it."""
    good, missing = sources
    for script, args in (
        (TAIL_MULTI, ("--sources", good, missing, "--no-default-filters")),
        (GREP, ("--sources", good, missing, "--pattern", "MATCH")),
        (CORRELATE, ("--sources", good, missing, "--anchor", "MATCH")),
    ):
        proc = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert proc.returncode == PARTIAL_FETCH_EXIT, (
            f"{script.name} human mode should exit {PARTIAL_FETCH_EXIT}, "
            f"got {proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
