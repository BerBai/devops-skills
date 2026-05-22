"""Unit tests for docker-quick/scripts/image_audit.py v1.0.

Mocks ``subprocess.run`` and exercises parsers, waste pattern scanning,
severity scoring, and main()'s end-to-end flow across local/remote
routes. Integration counterpart lives in
``tests/test_image_audit_integration.py`` (marker: ``live_ssh``).
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
    REPO_ROOT / "plugins" / "docker-quick" / "skills" / "docker-quick" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import image_audit as ia  # noqa: E402, I001

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope_json(
    success: bool = True,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    data: dict | None = None,
) -> str:
    return json.dumps({
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": data or {},
    })


def _history_row(
    *,
    rid: str = "<missing>",
    size: str = "10MB",
    created_by: str = "/bin/sh -c #(nop)  CMD [\"/bin/sh\"]",
    created_at: str = "2024-01-15T10:23:45Z",
    comment: str = "",
) -> dict:
    return {
        "ID": rid,
        "Size": size,
        "CreatedBy": created_by,
        "CreatedAt": created_at,
        "Comment": comment,
    }


def _history_stdout(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows)


def _inspect_blob(*, user: str = "1000:1000") -> dict:
    return {
        "Id": "sha256:fakeid",
        "Config": {"User": user},
        "Architecture": "amd64",
        "Size": 12345678,
    }


# -----------------------------------------------------------------------------
# _envelope / _emit
# -----------------------------------------------------------------------------


def test_envelope_defaults() -> None:
    env = ia._envelope(True, 0)
    assert env == {
        "success": True,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "data": {},
    }


def test_emit_json_path(capsys: pytest.CaptureFixture[str]) -> None:
    ia._emit({"success": True, "exit_code": 0, "stdout": "", "stderr": "",
              "data": {"x": 1}}, True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"] == {"x": 1}


# -----------------------------------------------------------------------------
# _parse_size: cover each unit + edge cases
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected", [
        ("12B", 12),
        ("0B", 0),
        ("1KB", 1024),
        ("12.3MB", int(12.3 * 1024 * 1024)),
        ("1GB", 1024 ** 3),
        ("2TB", 2 * 1024 ** 4),
        ("", 0),
        ("garbage", 0),
        ("12.3", 12),       # numeric without unit -> bytes-like fallback
    ],
)
def test_parse_size_known_units(raw: str, expected: int) -> None:
    assert ia._parse_size(raw) == expected


def test_parse_size_strips_whitespace() -> None:
    assert ia._parse_size("  500KB  ") == 500 * 1024


def test_parse_size_case_insensitive_suffix() -> None:
    assert ia._parse_size("10mb") == 10 * 1024 * 1024


def test_parse_size_none_input_treated_as_zero() -> None:
    assert ia._parse_size("") == 0


def test_parse_size_floats_with_unit() -> None:
    assert ia._parse_size("1.5GB") == int(1.5 * 1024 ** 3)


# -----------------------------------------------------------------------------
# _parse_history_output: shapes the JSON Lines parsing
# -----------------------------------------------------------------------------


def test_parse_history_multi_layer() -> None:
    rows = [_history_row(size="10MB"), _history_row(size="20MB"),
            _history_row(size="0B")]
    layers, err = ia._parse_history_output(_history_stdout(rows))
    assert err is None
    assert layers is not None
    assert len(layers) == 3  # noqa: PLR2004
    assert layers[0]["size_bytes"] == 10 * 1024 * 1024
    assert layers[1]["index"] == 1
    assert layers[2]["size_bytes"] == 0


def test_parse_history_single_layer() -> None:
    layers, err = ia._parse_history_output(json.dumps(_history_row()))
    assert err is None
    assert len(layers) == 1


def test_parse_history_empty_stdout_is_error() -> None:
    layers, err = ia._parse_history_output("")
    assert layers is None
    assert err is not None


def test_parse_history_non_json_line() -> None:
    text = json.dumps(_history_row()) + "\nNOT JSON\n"
    layers, err = ia._parse_history_output(text)
    assert layers is None
    assert err is not None
    assert "line 2" in err


def test_parse_history_blank_lines_are_skipped() -> None:
    text = "\n" + json.dumps(_history_row()) + "\n\n"
    layers, err = ia._parse_history_output(text)
    assert err is None
    assert len(layers) == 1


# -----------------------------------------------------------------------------
# _parse_inspect_output
# -----------------------------------------------------------------------------


def test_parse_inspect_happy() -> None:
    blob, err = ia._parse_inspect_output(json.dumps(_inspect_blob()))
    assert err is None
    assert blob["Config"]["User"] == "1000:1000"


def test_parse_inspect_non_dict_input() -> None:
    blob, err = ia._parse_inspect_output(json.dumps([1, 2, 3]))
    assert blob is None
    assert err is not None
    assert "expected object" in err


def test_parse_inspect_invalid_json() -> None:
    blob, err = ia._parse_inspect_output("NOPE")
    assert blob is None
    assert err is not None


# -----------------------------------------------------------------------------
# _score_layers
# -----------------------------------------------------------------------------


def test_score_layers_all_ok_under_threshold() -> None:
    layers, _ = ia._parse_history_output(_history_stdout([
        _history_row(size="10MB"),
        _history_row(size="50MB"),
    ]))
    state, findings = ia._score_layers(layers, threshold_bytes=200 * 1024 * 1024)
    assert state == "ok"
    assert findings == []


def test_score_layers_large_layer_warns() -> None:
    layers, _ = ia._parse_history_output(_history_stdout([
        _history_row(size="250MB"),
    ]))
    state, findings = ia._score_layers(layers, threshold_bytes=200 * 1024 * 1024)
    assert state == "warn"
    f = next(f for f in findings if f["kind"] == "large_layer")
    assert f["value"]["index"] == 0


def test_score_layers_apt_cache_left_warns() -> None:
    layers, _ = ia._parse_history_output(_history_stdout([
        _history_row(created_by="/bin/sh -c apt-get update && apt-get install -y curl"),
    ]))
    state, findings = ia._score_layers(layers, threshold_bytes=1024 ** 4)
    assert state == "warn"
    assert any(f["kind"] == "apt_cache_left" for f in findings)


def test_score_layers_apt_cache_cleared_no_finding() -> None:
    layers, _ = ia._parse_history_output(_history_stdout([
        _history_row(created_by="apt-get install -y curl && rm -rf /var/lib/apt/lists"),
    ]))
    state, findings = ia._score_layers(layers, threshold_bytes=1024 ** 4)
    assert state == "ok"
    assert not any(f["kind"] == "apt_cache_left" for f in findings)


def test_score_layers_pip_cache_left_warns() -> None:
    layers, _ = ia._parse_history_output(_history_stdout([
        _history_row(created_by="pip install -r requirements.txt"),
    ]))
    state, findings = ia._score_layers(layers, threshold_bytes=1024 ** 4)
    assert state == "warn"
    assert any(f["kind"] == "pip_cache_left" for f in findings)


def test_score_layers_pip_no_cache_dir_no_finding() -> None:
    layers, _ = ia._parse_history_output(_history_stdout([
        _history_row(created_by="pip install --no-cache-dir -r requirements.txt"),
    ]))
    state, findings = ia._score_layers(layers, threshold_bytes=1024 ** 4)
    assert state == "ok"


def test_score_layers_npm_cache_left_warns() -> None:
    layers, _ = ia._parse_history_output(_history_stdout([
        _history_row(created_by="npm install --prefix /app"),
    ]))
    state, findings = ia._score_layers(layers, threshold_bytes=1024 ** 4)
    assert state == "warn"
    assert any(f["kind"] == "npm_cache_left" for f in findings)


def test_score_layers_multiple_findings_same_layer() -> None:
    layers, _ = ia._parse_history_output(_history_stdout([
        _history_row(size="500MB",
                     created_by="apt-get install -y curl && pip install requests"),
    ]))
    state, findings = ia._score_layers(layers, threshold_bytes=200 * 1024 * 1024)
    kinds = {f["kind"] for f in findings}
    assert "large_layer" in kinds
    assert "apt_cache_left" in kinds
    assert "pip_cache_left" in kinds
    assert state == "warn"


# -----------------------------------------------------------------------------
# _score_user (info-only)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("user", ["", "root", "0", "0:0"])
def test_score_user_root_emits_info(user: str) -> None:
    findings = ia._score_user(_inspect_blob(user=user))
    assert len(findings) == 1
    assert findings[0]["severity"] == "info"
    assert findings[0]["kind"] == "running_as_root"


def test_score_user_non_root_no_finding() -> None:
    findings = ia._score_user(_inspect_blob(user="1000:1000"))
    assert findings == []


def test_score_user_none_blob_returns_empty() -> None:
    assert ia._score_user(None) == []


# -----------------------------------------------------------------------------
# _history_image / _inspect_image — local and remote routes
# -----------------------------------------------------------------------------


def test_history_image_local_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _completed(0, stdout=_history_stdout([_history_row()]))

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    env = ia._history_image("local", "busybox:latest", "docker", connect_timeout=8)
    assert env["success"]
    assert captured["argv"] == [
        "docker", "history", "--no-trunc", "--format", "{{json .}}",
        "busybox:latest",
    ]


def test_history_image_local_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _completed(0, stdout=_history_stdout([_history_row()]))

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    ia._history_image("local", "busybox:latest", "podman", connect_timeout=8)
    assert captured["argv"][0] == "podman"


def test_history_image_remote_shlex_quotes_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _completed(0, stdout=_envelope_json(
            stdout=_history_stdout([_history_row()])
        ))

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    ia._history_image(
        "pai", "weird image name", "docker", connect_timeout=8,
        ssh_exec_override=fake_ssh,
    )
    cmd_str = captured["argv"][3]
    assert "'weird image name'" in cmd_str
    assert "docker history --no-trunc" in cmd_str


def test_history_image_remote_missing_ssh_execute_precondition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        ia, "_ssh_execute_path", lambda: tmp_path / "missing.py",
    )
    env = ia._history_image("pai", "img", "docker", connect_timeout=8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "precondition"


def test_history_image_remote_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def raise_to(*args, **kw):
        raise subprocess.TimeoutExpired("ssh", 30)

    monkeypatch.setattr(ia.subprocess, "run", raise_to)
    env = ia._history_image(
        "pai", "img", "docker", connect_timeout=8,
        ssh_exec_override=fake_ssh,
    )
    assert env["data"]["failure_class"] == "timeout"
    assert env["exit_code"] == EXIT_TIMEOUT


def test_history_image_remote_non_json_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def fake_run(argv, **kw):
        return _completed(1, stdout="BROKEN")

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    env = ia._history_image(
        "pai", "img", "docker", connect_timeout=8,
        ssh_exec_override=fake_ssh,
    )
    assert env["data"]["failure_class"] == "ssh_execute_broken"


def test_inspect_image_local_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _completed(0, stdout=json.dumps(_inspect_blob()))

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    env = ia._inspect_image("local", "busybox:latest", "docker", connect_timeout=8)
    assert env["success"]
    assert captured["argv"] == [
        "docker", "inspect", "--format", "{{json .}}", "busybox:latest",
    ]


def test_inspect_image_remote_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def fake_run(argv, **kw):
        return _completed(0, stdout=_envelope_json(
            stdout=json.dumps(_inspect_blob())
        ))

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    env = ia._inspect_image(
        "pai", "img", "docker", connect_timeout=8,
        ssh_exec_override=fake_ssh,
    )
    assert env["success"]


# -----------------------------------------------------------------------------
# main() end-to-end
# -----------------------------------------------------------------------------


def test_main_argparse_missing_host_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        ia.main([])
    assert exc.value.code == EXIT_ARGS


def test_main_argparse_missing_image_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        ia.main(["local"])
    assert exc.value.code == EXIT_ARGS


def _mock_subprocess(monkeypatch: pytest.MonkeyPatch, plan: list[dict]) -> list[dict]:
    calls = []
    idx = [0]

    def fake_run(argv, **kw):
        i = idx[0]
        idx[0] += 1
        calls.append({"argv": argv, "kw": kw})
        step = plan[i] if i < len(plan) else plan[-1]
        return _completed(
            step.get("returncode", 0),
            stdout=step.get("stdout", ""),
            stderr=step.get("stderr", ""),
        )

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    return calls


def test_main_happy_clean_image(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_history_row(size="5MB"), _history_row(size="10MB")]
    plan = [
        {"stdout": _history_stdout(rows)},
        {"stdout": json.dumps(_inspect_blob(user="1000:1000"))},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = ia.main(["local", "busybox:latest", "--json"])
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["data"]["summary"]["state"] == "ok"
    assert payload["data"]["summary"]["layer_count"] == 2  # noqa: PLR2004
    assert payload["data"]["findings"] == []


def test_main_nonexistent_image_emits_remote_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = [{"returncode": 1, "stderr": "Error: No such image"}]
    _mock_subprocess(monkeypatch, plan)
    code = ia.main(["local", "nonexistent:tag", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["failure_class"] == "remote_error"


def test_main_history_unparseable_emits_parse_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = [{"stdout": "BROKEN STDOUT"}]
    _mock_subprocess(monkeypatch, plan)
    code = ia.main(["local", "img", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["failure_class"] == "parse_error"


def test_main_inspect_unavailable_keeps_layers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_history_row(size="5MB")]
    plan = [
        {"stdout": _history_stdout(rows)},
        {"returncode": 1, "stderr": "permission denied"},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = ia.main(["local", "img", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["failure_class"] == "inspect_unavailable"
    assert payload["data"]["layers"], "layers must remain populated"
    assert payload["data"]["summary"]["layer_count"] == 1


def test_main_large_layer_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_history_row(size="600MB"),
            _history_row(size="5MB",
                         created_by="pip install --no-cache-dir foo")]
    plan = [
        {"stdout": _history_stdout(rows)},
        {"stdout": json.dumps(_inspect_blob())},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = ia.main(["local", "img", "--threshold-mb", "200", "--json"])
    assert code == EXIT_OK  # warn, not crit -- no failure_class set
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["summary"]["state"] == "warn"
    assert any(f["kind"] == "large_layer" for f in payload["data"]["findings"])
    assert payload["data"]["summary"]["large_layers"] == 1


def test_main_runtime_podman_threads_through(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _mock_subprocess(monkeypatch, [
        {"stdout": _history_stdout([_history_row(size="5MB")])},
        {"stdout": json.dumps(_inspect_blob())},
    ])
    code = ia.main(["local", "img", "--runtime", "podman", "--json"])
    assert code == EXIT_OK
    assert calls[0]["argv"][0] == "podman"
    assert calls[1]["argv"][0] == "podman"


def test_main_root_user_emits_info_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = [
        {"stdout": _history_stdout([_history_row(size="5MB")])},
        {"stdout": json.dumps(_inspect_blob(user="root"))},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = ia.main(["local", "img", "--json"])
    payload = json.loads(capsys.readouterr().out)
    # info finding present, but state should remain ok (info NOT aggregated)
    assert code == EXIT_OK
    assert payload["data"]["summary"]["state"] == "ok"
    f = next(f for f in payload["data"]["findings"]
             if f["kind"] == "running_as_root")
    assert f["severity"] == "info"


def test_main_threshold_mb_respected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = [
        {"stdout": _history_stdout([_history_row(size="80MB")])},
        {"stdout": json.dumps(_inspect_blob())},
    ]
    _mock_subprocess(monkeypatch, plan)
    # threshold 50MB -> 80MB layer trips warn
    code = ia.main(["local", "img", "--threshold-mb", "50", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload["data"]["summary"]["state"] == "warn"
    assert payload["data"]["threshold_mb"] == 50  # noqa: PLR2004


def test_main_human_path_emits_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = [
        {"stdout": _history_stdout([_history_row(size="5MB")])},
        {"stdout": json.dumps(_inspect_blob())},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = ia.main(["local", "img"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "host:" in out
    assert "image:" in out
    assert "summary:" in out
    assert "layers:" in out


def test_main_total_size_aggregates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_history_row(size="10MB"),
            _history_row(size="30MB"),
            _history_row(size="20MB")]
    plan = [
        {"stdout": _history_stdout(rows)},
        {"stdout": json.dumps(_inspect_blob())},
    ]
    _mock_subprocess(monkeypatch, plan)
    ia.main(["local", "img", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["summary"]["total_size_mb"] == 60.0  # noqa: PLR2004
