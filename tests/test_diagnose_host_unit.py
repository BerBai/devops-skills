"""Unit tests for remote-debug/scripts/diagnose_host.py v1.0.

The script is exercised through its in-process API (``_run_probe``,
``_score``, ``_parse_*``, ``main``) with ``subprocess.run`` mocked at the
module level. The integration counterpart lives in
``tests/test_diagnose_host_integration.py`` (marker: ``live_ssh``).

The architectural claim under test is CONTRIBUTING.md line 29 -- that
remote-debug invokes ssh-core via ``subprocess`` and never via a Python
import. The mocked subprocess.run stand-in lets us assert the argv
shape *and* feed back synthetic JSON envelopes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "remote-debug" / "skills" / "remote-debug" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import diagnose_host  # noqa: E402, I001

# Magic-value constants (avoids PLR2004 in assertions).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_TIMEOUT = 124
EXIT_SSH_FAIL = 255
CORES_REF = 4
LOAD_WARN_REF = 5.0
LOAD_CRIT_REF = 9.0
DISK_WARN_PCT_REF = 85
DISK_CRIT_PCT_REF = 96
ZOMBIES_WARN_REF = 7

# Reference parser inputs (named so PLR2004 doesn't fire on each digit).
LOAD_1M_SAMPLE = 0.12
LOAD_5M_SAMPLE = 0.34
LOAD_15M_SAMPLE = 0.56
THREADS_SAMPLE = 234
CORES_8 = 8
LOAD_1M_8CORE = 0.01
MEM_TOTAL_REF = 16384
MEM_USED_REF = 8192
MEM_FREE_REF = 2048
TWO = 2


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _envelope_json(
    success: bool = True,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    data: dict | None = None,
) -> str:
    """Build the JSON string ssh_execute.py would print on --json."""
    return json.dumps(
        {
            "success": success,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "data": data or {},
        }
    )


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# -----------------------------------------------------------------------------
# ssh_execute.py discovery
# -----------------------------------------------------------------------------


def test_ssh_execute_path_discovery_finds_file() -> None:
    """The relative path computed from __file__ must point at the real
    ssh_execute.py in this repo's checkout."""
    path = diagnose_host._ssh_execute_path()
    assert path.name == "ssh_execute.py"
    assert path.exists(), f"expected ssh_execute.py at {path}"
    # Sanity: same content as the actual ssh-core script.
    expected = (
        REPO_ROOT
        / "plugins"
        / "ssh-core"
        / "skills"
        / "ssh-core"
        / "scripts"
        / "ssh_execute.py"
    ).resolve()
    assert path.resolve() == expected


def test_ssh_execute_path_discovery_raises_clear_error_when_absent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If ssh_execute.py is missing on disk, main() must emit a
    precondition envelope rather than raise."""
    fake_path = Path("/nonexistent/path/to/ssh_execute.py")
    monkeypatch.setattr(diagnose_host, "_ssh_execute_path", lambda: fake_path)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called when ssh_execute is missing")

    monkeypatch.setattr(diagnose_host.subprocess, "run", fail_if_called)

    rc = diagnose_host.main(["pai", "--json"])

    assert rc == EXIT_FAIL
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["success"] is False
    assert parsed["exit_code"] == EXIT_FAIL
    assert parsed["data"]["failure_class"] == "precondition"
    assert "ssh_execute.py" in parsed["stderr"]
    assert "ssh-core" in parsed["stderr"]


# -----------------------------------------------------------------------------
# _run_probe: argv shape, JSON envelope passthrough, error mapping
# -----------------------------------------------------------------------------


def test_run_probe_parses_ssh_execute_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal ssh_execute envelope round-trips through _run_probe."""
    captured: dict = {}
    payload = _envelope_json(
        success=True,
        exit_code=0,
        stdout="13:00:00 up 1 day,  load average: 0.10, 0.05, 0.01\n",
        stderr="",
        data={"route": "remote", "failure_class": None},
    )

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(diagnose_host.subprocess, "run", fake_run)

    ssh_exec = Path("/fake/ssh_execute.py")
    env = diagnose_host._run_probe(ssh_exec, "pai", "uptime")

    # Envelope made it through verbatim.
    assert env["success"] is True
    assert env["exit_code"] == EXIT_OK
    assert "load average" in env["stdout"]
    assert env["data"]["failure_class"] is None

    # Shell-out contract: python -> ssh_execute.py -> host -> command -> --json.
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][1] == str(ssh_exec)
    assert captured["argv"][2] == "pai"
    assert captured["argv"][3] == "uptime"
    assert "--json" in captured["argv"]
    # ADR-001 D1 invariants: argv list, no shell.
    assert isinstance(captured["argv"], list)
    assert captured["kwargs"].get("shell") in (None, False)
    assert captured["kwargs"]["capture_output"] is True


def test_run_probe_handles_non_json_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ssh_execute prints non-JSON on stdout, _run_probe synthesises a
    failure envelope rather than crashing."""

    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="not actually json", stderr="something broke")

    monkeypatch.setattr(diagnose_host.subprocess, "run", fake_run)

    env = diagnose_host._run_probe(Path("/fake/ssh_execute.py"), "pai", "uptime")

    assert env["success"] is False
    assert env["data"]["failure_class"] == "ssh_execute_broken"
    assert "non-JSON" in env["stderr"]
    assert env["stdout"] == "not actually json"


def test_run_probe_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess.TimeoutExpired in the ssh_execute call -> 124 envelope."""

    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(diagnose_host.subprocess, "run", raise_timeout)
    env = diagnose_host._run_probe(Path("/fake/ssh_execute.py"), "pai", "uptime")
    assert env["exit_code"] == EXIT_TIMEOUT
    assert env["data"]["failure_class"] == "timeout"


# -----------------------------------------------------------------------------
# _score severity thresholds
# -----------------------------------------------------------------------------


def _probe_block(parsed: dict, success: bool = True) -> dict:
    """Minimal probe dict that satisfies _score's reads."""
    return {
        "success": success,
        "exit_code": 0 if success else 1,
        "stdout": "",
        "stderr": "",
        "failure_class": None,
        "parsed": parsed,
    }


def test_score_ok_when_all_probes_clean() -> None:
    probes = {
        "load": _probe_block({"cores": CORES_REF, "load_1m": 0.1}),
        "disk": _probe_block({"use_pct": 10}),
        "zombie": _probe_block({"count": 0}),
    }
    assert diagnose_host._score(probes) == "ok"


def test_score_warn_on_load_above_cores() -> None:
    probes = {
        "load": _probe_block({"cores": CORES_REF, "load_1m": LOAD_WARN_REF}),
        "disk": _probe_block({"use_pct": 10}),
        "zombie": _probe_block({"count": 0}),
    }
    assert diagnose_host._score(probes) == "warn"


def test_score_crit_on_load_2x_cores() -> None:
    probes = {
        "load": _probe_block({"cores": CORES_REF, "load_1m": LOAD_CRIT_REF}),
        "disk": _probe_block({"use_pct": 10}),
        "zombie": _probe_block({"count": 0}),
    }
    assert diagnose_host._score(probes) == "crit"


def test_score_warn_on_disk_80pct() -> None:
    probes = {
        "load": _probe_block({"cores": CORES_REF, "load_1m": 0.1}),
        "disk": _probe_block({"use_pct": DISK_WARN_PCT_REF}),
        "zombie": _probe_block({"count": 0}),
    }
    assert diagnose_host._score(probes) == "warn"


def test_score_crit_on_disk_95pct() -> None:
    probes = {
        "load": _probe_block({"cores": CORES_REF, "load_1m": 0.1}),
        "disk": _probe_block({"use_pct": DISK_CRIT_PCT_REF}),
        "zombie": _probe_block({"count": 0}),
    }
    assert diagnose_host._score(probes) == "crit"


def test_score_warn_on_zombies_above_5() -> None:
    probes = {
        "load": _probe_block({"cores": CORES_REF, "load_1m": 0.1}),
        "disk": _probe_block({"use_pct": 10}),
        "zombie": _probe_block({"count": ZOMBIES_WARN_REF}),
    }
    assert diagnose_host._score(probes) == "warn"


def test_score_crit_does_not_downgrade_to_warn() -> None:
    """A crit signal must dominate later warn signals."""
    probes = {
        "load": _probe_block({"cores": CORES_REF, "load_1m": LOAD_CRIT_REF}),
        "disk": _probe_block({"use_pct": DISK_WARN_PCT_REF}),
        "zombie": _probe_block({"count": ZOMBIES_WARN_REF}),
    }
    assert diagnose_host._score(probes) == "crit"


def test_score_missing_parsed_fields_is_ok() -> None:
    """If parsers couldn't extract values, severity stays ok (not a crash)."""
    probes = {
        "load": _probe_block({}),
        "disk": _probe_block({}),
        "zombie": _probe_block({}),
    }
    assert diagnose_host._score(probes) == "ok"


# -----------------------------------------------------------------------------
# Parsers
# -----------------------------------------------------------------------------


def test_parse_load_handles_loadavg_format() -> None:
    parsed = diagnose_host._parse_load("4\n0.12 0.34 0.56 1/234 5678\n")
    assert parsed["cores"] == CORES_REF
    assert parsed["load_1m"] == LOAD_1M_SAMPLE
    assert parsed["load_5m"] == LOAD_5M_SAMPLE
    assert parsed["load_15m"] == LOAD_15M_SAMPLE
    assert parsed["running"] == 1
    assert parsed["threads"] == THREADS_SAMPLE


def test_parse_load_handles_missing_running_field() -> None:
    """Some kernels emit only the three load floats; running/threads optional."""
    parsed = diagnose_host._parse_load("8\n0.01 0.02 0.03\n")
    assert parsed["cores"] == CORES_8
    assert parsed["load_1m"] == LOAD_1M_8CORE
    assert "running" not in parsed


def test_parse_load_handles_garbage_input() -> None:
    """Garbage input must not crash; parser returns {} or partial dict."""
    parsed = diagnose_host._parse_load("garbage line 1\nmore garbage\n")
    # Nothing parseable -> empty dict (no 'cores', no 'load_1m').
    assert "cores" not in parsed
    assert "load_1m" not in parsed


def test_parse_disk_handles_df_format() -> None:
    line = "/dev/sda1   1000000  500000  500000  47% /\n"
    assert diagnose_host._parse_disk(line) == {"use_pct": 47}


def test_parse_disk_handles_garbage_input() -> None:
    assert diagnose_host._parse_disk("no percent here") == {}


def test_parse_mem_handles_free_format() -> None:
    line = "Mem:   16384  8192  2048   100   6144   8000\n"
    parsed = diagnose_host._parse_mem(line)
    assert parsed["total_mb"] == MEM_TOTAL_REF
    assert parsed["used_mb"] == MEM_USED_REF
    assert parsed["free_mb"] == MEM_FREE_REF


def test_parse_mem_handles_garbage_input() -> None:
    assert diagnose_host._parse_mem("not a free output") == {}


def test_parse_zombie_handles_count() -> None:
    assert diagnose_host._parse_zombie("0\n") == {"count": 0}
    assert diagnose_host._parse_zombie("3\n") == {"count": 3}


def test_parse_zombie_handles_garbage_input() -> None:
    assert diagnose_host._parse_zombie("not a number") == {}


def test_parse_uptime_returns_empty() -> None:
    """uptime is free-form text; we keep the raw stdout and parse nothing."""
    assert diagnose_host._parse_uptime("anything here") == {}


# -----------------------------------------------------------------------------
# --check CSV filter
# -----------------------------------------------------------------------------


def test_check_filter_runs_only_listed_probes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--check load,disk must invoke ssh_execute exactly twice with the
    matching probe commands."""
    monkeypatch.setattr(
        diagnose_host, "_ssh_execute_path", lambda: Path(__file__)
    )  # any existing file
    captured: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        captured.append(list(argv))
        return _completed(
            0,
            stdout=_envelope_json(
                success=True,
                exit_code=0,
                stdout="4\n0.1 0.1 0.1 1/1 1\n" if "loadavg" in argv[3] else "/d 1 1 1 5% /\n",
                stderr="",
            ),
        )

    monkeypatch.setattr(diagnose_host.subprocess, "run", fake_run)

    rc = diagnose_host.main(["pai", "--check", "load,disk", "--json"])
    assert rc == EXIT_OK

    # Exactly two ssh_execute calls, in declared order.
    assert len(captured) == TWO
    probe_cmds = [argv[3] for argv in captured]
    assert probe_cmds == [diagnose_host.PROBES["load"], diagnose_host.PROBES["disk"]]

    # checks_run reflects the filter.
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["checks_run"] == ["load", "disk"]
    assert set(out["data"]["probes"].keys()) == {"load", "disk"}


def test_check_filter_rejects_unknown_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--check with a name not in PROBES must short-circuit with a
    precondition envelope before any ssh_execute call."""
    monkeypatch.setattr(diagnose_host, "_ssh_execute_path", lambda: Path(__file__))

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not run when --check is invalid")

    monkeypatch.setattr(diagnose_host.subprocess, "run", fail_if_called)

    rc = diagnose_host.main(["pai", "--check", "bogus_probe", "--json"])
    assert rc == EXIT_FAIL
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is False
    assert out["data"]["failure_class"] == "precondition"
    assert "bogus_probe" in out["stderr"]


# -----------------------------------------------------------------------------
# CLI surface
# -----------------------------------------------------------------------------


def test_help_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse --help exits 0 and prints 'usage:' (contract preserved)."""
    with pytest.raises(SystemExit) as exc_info:
        diagnose_host.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "usage:" in out
    assert "host" in out
    assert "--check" in out
    assert "--json" in out


def test_main_aggregates_envelope_with_all_probes_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end main() with all probes returning healthy envelopes."""
    monkeypatch.setattr(diagnose_host, "_ssh_execute_path", lambda: Path(__file__))

    healthy: dict[str, str] = {
        "uptime": " 13:00:00 up 1 day,  load average: 0.10, 0.05, 0.01\n",
        "load": "4\n0.10 0.05 0.01 1/100 9999\n",
        "disk": "/dev/sda1  1000  100  900  10% /\n",
        "mem": "Mem:  8000  1000  6000  100  1000  7000\n",
        "zombie": "0\n",
    }

    def fake_run(argv, **_kwargs):
        cmd = argv[3]
        for name, probe_cmd in diagnose_host.PROBES.items():
            if probe_cmd == cmd:
                return _completed(0, stdout=_envelope_json(stdout=healthy[name]))
        raise AssertionError(f"unexpected probe command: {cmd}")

    monkeypatch.setattr(diagnose_host.subprocess, "run", fake_run)

    rc = diagnose_host.main(["pai", "--json"])
    assert rc == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is True
    assert out["data"]["severity"] == "ok"
    assert set(out["data"]["probes"].keys()) == {
        "uptime", "load", "disk", "mem", "zombie",
    }
    for name, probe in out["data"]["probes"].items():
        assert probe["success"] is True, f"{name} failed: {probe}"
    # Top-level summary text mentions severity.
    assert "severity=ok" in out["stdout"]


def test_main_failure_when_probe_returns_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If a probe envelope carries failure_class=network, overall success
    is False and the failure_class surfaces on the per-probe record."""
    monkeypatch.setattr(diagnose_host, "_ssh_execute_path", lambda: Path(__file__))

    def fake_run(*_args, **_kwargs):
        return _completed(
            0,
            stdout=_envelope_json(
                success=False,
                exit_code=EXIT_SSH_FAIL,
                stdout="",
                stderr="ssh: Could not resolve hostname x: ...",
                data={"route": "remote", "failure_class": "network"},
            ),
        )

    monkeypatch.setattr(diagnose_host.subprocess, "run", fake_run)

    rc = diagnose_host.main(["nope", "--json"])
    assert rc == EXIT_FAIL
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is False
    # Every probe captured its failure_class verbatim from ssh_execute.
    for probe in out["data"]["probes"].values():
        assert probe["success"] is False
        assert probe["failure_class"] == "network"
        assert probe["exit_code"] == EXIT_SSH_FAIL


def test_main_failure_when_severity_crit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even with all probes returning exit 0, severity=crit forces success=False."""
    monkeypatch.setattr(diagnose_host, "_ssh_execute_path", lambda: Path(__file__))

    # Tuned outputs: huge load + high disk usage to drive both into crit.
    canned: dict[str, str] = {
        "uptime": " up 1 day\n",
        "load": "4\n20.0 18.0 12.0 1/100 9999\n",  # 20 >> 2*4 -> crit
        "disk": "/dev/sda1  1000  990  10  99% /\n",  # 99 >= 95 -> crit
        "mem": "Mem:  8000  4000  3000  100  1000  3500\n",
        "zombie": "0\n",
    }

    def fake_run(argv, **_kwargs):
        cmd = argv[3]
        for name, probe_cmd in diagnose_host.PROBES.items():
            if probe_cmd == cmd:
                return _completed(0, stdout=_envelope_json(stdout=canned[name]))
        raise AssertionError(f"unexpected probe command: {cmd}")

    monkeypatch.setattr(diagnose_host.subprocess, "run", fake_run)

    rc = diagnose_host.main(["pai", "--json"])
    assert rc == EXIT_FAIL
    out = json.loads(capsys.readouterr().out)
    assert out["success"] is False
    assert out["data"]["severity"] == "crit"
