"""Unit tests for k8s-debug/scripts/diagnose_pod.py v1.0.

The kubectl.run_kubectl primitive is monkey-patched to return fixture
KubectlResult instances or raise KubectlError, so these tests run without
any real cluster or network.

Coverage targets (design.md §5, T2 group):
* 9 × phase classifier (fixture-driven)
* 8 × failure_class injection paths (80-87)
* --previous auto-trigger on CrashLoop + manual flag
* --container filter
* 10MB truncation marker
* ThreadPoolExecutor concurrency
* JSON envelope schema
* SSH_EXECUTE_PATH-missing → 80
* Edge cases (unknown phase, Job Succeeded/Failed, deletionTimestamp priority)
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "k8s-debug" / "skills" / "k8s-debug" / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "k8s"
REFERENCES_DIR = REPO_ROOT / "plugins" / "k8s-debug" / "skills" / "k8s-debug" / "references"

# Exit codes referenced in this module — avoid PLR2004 magic-value warnings.
EXIT_HOST_UNREACHABLE = 80


# Isolate `lib` from any other plugin's lib already cached in sys.modules.
_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))

import diagnose_pod  # noqa: E402, I001
from diagnose_pod import _PHASE_HINTS, _truncate_10mb, classify_phase, main, safe_dig  # noqa: E402
from lib import kubectl as kubectl_mod  # noqa: E402
from lib.kubectl import KubectlError, KubectlResult  # noqa: E402

sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(stdout: str = "", stderr: str = "", rc: int = 0, fc: str | None = None) -> KubectlResult:
    return KubectlResult(
        stdout=stdout,
        stderr=stderr,
        returncode=rc,
        elapsed_s=0.01,
        failure_class=fc,
        route="local",
    )


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"pod_status_{name}.json").read_text())


def _patch_run_kubectl(monkeypatch: pytest.MonkeyPatch, side_effect):
    """Common helper: monkeypatch the run_kubectl symbol seen by diagnose_pod."""
    monkeypatch.setattr(diagnose_pod, "run_kubectl", side_effect)


def _validate_envelope(env: dict) -> None:
    """Minimal JSON envelope schema (mirrors error-handling.md Pillar 1)."""
    assert set(env.keys()) >= {"success", "exit_code", "stdout", "stderr", "data"}
    assert isinstance(env["success"], bool)
    assert isinstance(env["exit_code"], int)
    assert isinstance(env["stdout"], str)
    assert isinstance(env["stderr"], str)
    assert isinstance(env["data"], dict)


# ---------------------------------------------------------------------------
# 9 × phase classifier (fixture-driven)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "expected_phase"),
    [
        ("pending", "Pending"),
        ("container_creating", "ContainerCreating"),
        ("running_notready", "Running-NotReady"),
        ("running_restarting", "Running-Restarting"),
        ("running_healthy", "Running-Healthy"),
        ("terminating", "Terminating"),
        ("crashloop", "CrashLoopBackOff"),
        ("imagepullback", "ImagePullBackOff"),
        ("oomkilled", "OOMKilled"),
    ],
)
def test_classify_phase_each_fixture(fixture_name: str, expected_phase: str) -> None:
    pod_status = _load_fixture(fixture_name)
    assert classify_phase(pod_status) == expected_phase


# ---------------------------------------------------------------------------
# 8 × failure_class injection paths (via main())
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fc", "exit_code"),
    [
        ("host_unreachable", 80),
        ("kubectl_missing", 81),
        ("kubeconfig_missing", 82),
        ("pod_not_found", 83),
        ("namespace_not_found", 84),
        ("forbidden", 85),
        ("api_timeout", 86),
    ],
)
def test_failure_class_injection(
    fc: str, exit_code: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Inject a KubectlError on the first run_kubectl call → expect mapped envelope."""

    def boom(*_args, **_kwargs):
        raise KubectlError(fc, exit_code, f"injected {fc}")

    _patch_run_kubectl(monkeypatch, boom)
    rc = main(["local", "default", "p", "--json"])
    assert rc == exit_code
    out = capsys.readouterr().out
    env = json.loads(out)
    _validate_envelope(env)
    assert env["success"] is False
    assert env["exit_code"] == exit_code
    assert env["data"]["failure_class"] == fc


def test_metrics_unavailable_is_soft(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """87 soft failure: exit 0 + warn finding for 'top'."""
    healthy_status = _load_fixture("running_healthy")
    calls: list[tuple] = []

    def fake_run(host, namespace, args, *, use_json=False, timeout=30):
        calls.append((host, namespace, tuple(args)))
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] == ["get", "pod"]:
            return _result(stdout=json.dumps(healthy_status))
        if args[:2] == ["top", "pod"]:
            return _result(rc=1, stderr="Metrics API not available", fc="metrics_unavailable")
        return _result(stdout="ok")

    _patch_run_kubectl(monkeypatch, fake_run)
    rc = main(["local", "default", "healthy-pod", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    env = json.loads(out)
    _validate_envelope(env)
    assert env["data"]["failure_class"] is None
    # At least one warn finding for metrics_unavailable.
    warn_metrics = [
        f
        for f in env["data"]["findings"]
        if f.get("severity") == "warn" and "metrics" in (f.get("reason", "") + f.get("message", "")).lower()
    ]
    assert warn_metrics, f"expected metrics warning in findings: {env['data']['findings']}"


# ---------------------------------------------------------------------------
# --previous logic
# ---------------------------------------------------------------------------


def test_previous_auto_trigger_on_crashloop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    crash_status = _load_fixture("crashloop")
    seen_args: list[list[str]] = []

    def fake_run(host, namespace, args, *, use_json=False, timeout=30):
        seen_args.append(list(args))
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] == ["get", "pod"]:
            return _result(stdout=json.dumps(crash_status))
        return _result(stdout="ok")

    _patch_run_kubectl(monkeypatch, fake_run)
    rc = main(["local", "default", "crashloop-pod", "--json"])
    assert rc == 0
    # CrashLoop must have triggered --previous even though the flag wasn't passed.
    assert any("--previous" in a for a in seen_args), (
        f"--previous not auto-triggered: {seen_args}"
    )


def test_previous_manual_flag(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """--previous flag forces previous logs on any pod, healthy included."""
    healthy_status = _load_fixture("running_healthy")
    seen_args: list[list[str]] = []

    def fake_run(host, namespace, args, *, use_json=False, timeout=30):
        seen_args.append(list(args))
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] == ["get", "pod"]:
            return _result(stdout=json.dumps(healthy_status))
        return _result(stdout="ok")

    _patch_run_kubectl(monkeypatch, fake_run)
    rc = main(["local", "default", "healthy-pod", "--previous", "--json"])
    assert rc == 0
    assert any("--previous" in a for a in seen_args)


def test_previous_not_set_on_healthy_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    healthy_status = _load_fixture("running_healthy")
    seen_args: list[list[str]] = []

    def fake_run(host, namespace, args, *, use_json=False, timeout=30):
        seen_args.append(list(args))
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] == ["get", "pod"]:
            return _result(stdout=json.dumps(healthy_status))
        return _result(stdout="ok")

    _patch_run_kubectl(monkeypatch, fake_run)
    rc = main(["local", "default", "healthy-pod", "--json"])
    assert rc == 0
    # Default (no --previous) on healthy: --previous must NOT appear.
    assert not any("--previous" in a for a in seen_args)


# ---------------------------------------------------------------------------
# --container filter
# ---------------------------------------------------------------------------


def test_container_filter_passes_through(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    healthy_status = _load_fixture("running_healthy")
    seen_args: list[list[str]] = []

    def fake_run(host, namespace, args, *, use_json=False, timeout=30):
        seen_args.append(list(args))
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] == ["get", "pod"]:
            return _result(stdout=json.dumps(healthy_status))
        return _result(stdout="ok")

    _patch_run_kubectl(monkeypatch, fake_run)
    rc = main(
        ["local", "default", "healthy-pod", "--container", "sidecar", "--json"]
    )
    assert rc == 0
    log_calls = [a for a in seen_args if a and a[0] == "logs"]
    assert log_calls, "expected at least one logs call"
    assert any("-c" in a and "sidecar" in a for a in log_calls), (
        f"missing -c sidecar in log calls: {log_calls}"
    )


# ---------------------------------------------------------------------------
# 10MB truncation
# ---------------------------------------------------------------------------


def test_truncate_10mb_short_string_pass_through() -> None:
    s = "hello world\n"
    assert _truncate_10mb(s) == s


def test_truncate_10mb_long_string_gets_marker() -> None:
    s = "x" * (10 * 1024 * 1024 + 1024)  # 10MB + 1KB
    out = _truncate_10mb(s)
    assert len(out) == 10 * 1024 * 1024 + len("\n... <truncated 10MB cap>\n")
    assert "truncated 10MB cap" in out


# ---------------------------------------------------------------------------
# ThreadPoolExecutor concurrency
# ---------------------------------------------------------------------------


def test_concurrent_kubectl_calls_run_in_parallel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    healthy_status = _load_fixture("running_healthy")

    SLEEP_S = 0.3

    def slow_run(host, namespace, args, *, use_json=False, timeout=30):
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] == ["get", "pod"]:
            return _result(stdout=json.dumps(healthy_status))
        # describe / events / logs / top each sleep 0.3s — 4 calls.
        time.sleep(SLEEP_S)
        return _result(stdout="slow")

    _patch_run_kubectl(monkeypatch, slow_run)
    start = time.monotonic()
    rc = main(["local", "default", "healthy-pod", "--json"])
    elapsed = time.monotonic() - start
    assert rc == 0
    # Sequential would take >= 4 * 0.3 = 1.2s. ThreadPool max_workers=4 brings it
    # under 3 * 0.3 = 0.9s (with overhead). Loose bound, plenty of slack.
    assert elapsed < 4 * SLEEP_S, (
        f"expected ThreadPoolExecutor to parallelize 4 calls; observed {elapsed:.2f}s"
    )


# ---------------------------------------------------------------------------
# JSON envelope schema validation (end-to-end through main)
# ---------------------------------------------------------------------------


def test_main_emits_valid_envelope(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    healthy_status = _load_fixture("running_healthy")

    def fake_run(host, namespace, args, *, use_json=False, timeout=30):
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] == ["get", "pod"]:
            return _result(stdout=json.dumps(healthy_status))
        return _result(stdout="ok")

    _patch_run_kubectl(monkeypatch, fake_run)
    rc = main(["local", "default", "healthy-pod", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    env = json.loads(out)
    _validate_envelope(env)
    # Data shape per design.md §3.
    for key in ("host", "namespace", "pod", "phase", "summary", "findings", "raw"):
        assert key in env["data"], f"missing data.{key}"
    assert env["data"]["phase"] == "Running-Healthy"
    assert env["data"]["summary"]["pods"] == "ok"


# ---------------------------------------------------------------------------
# hint anchor existence (R1 gate, inlined as test)
# ---------------------------------------------------------------------------


def _slug_present_in_md(md_text: str, slug: str) -> bool:
    """Check whether ``slug`` exists as a heading slug in the given markdown.

    GitHub-style slug rule: case-insensitive, spaces → '-', other punctuation
    dropped (but Chinese characters kept). We approximate by matching any
    line starting with `## ` and slugifying it the same way.
    """

    def slugify(s: str) -> str:
        s = s.strip().lower()
        # Replace spaces and tabs with hyphens.
        s = re.sub(r"[\s]+", "-", s)
        # Drop characters that are not Chinese, letters, digits, hyphens.
        s = re.sub(r"[^a-z0-9\-一-鿿]", "", s)
        return s

    target = slug.strip().lower()
    for line in md_text.splitlines():
        # Accept ## or ### headings (3a/3b sit under H3).
        if line.startswith("## ") or line.startswith("### "):
            head = line.lstrip("#").lstrip()
            if slugify(head) == target:
                return True
    return False


def test_phase_hints_all_resolve() -> None:
    """Every non-empty hint must point at an existing heading in references/."""
    missing: list[str] = []
    for phase, hint in _PHASE_HINTS.items():
        if not hint:
            continue
        file_part, _, anchor = hint.partition("#")
        # `file_part` is "references/<file>.md"
        md_file = (
            REPO_ROOT
            / "plugins"
            / "k8s-debug"
            / "skills"
            / "k8s-debug"
            / file_part
        )
        if not md_file.exists():
            missing.append(f"{phase}: file {md_file} missing")
            continue
        if not _slug_present_in_md(md_file.read_text(), anchor):
            missing.append(f"{phase}: anchor #{anchor} missing in {file_part}")
    assert not missing, "Missing hint anchors:\n" + "\n".join(missing)


# ---------------------------------------------------------------------------
# SSH_EXECUTE_PATH missing → 80
# ---------------------------------------------------------------------------


def test_ssh_execute_path_missing_yields_80(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """When SSH_EXECUTE_PATH is bogus and host != 'local', expect 80 + host_unreachable."""
    monkeypatch.setattr(
        kubectl_mod, "SSH_EXECUTE_PATH", tmp_path / "does-not-exist.py"
    )
    rc = main(["bastion-prod", "default", "p", "--json"])
    assert rc == EXIT_HOST_UNREACHABLE
    out = capsys.readouterr().out
    env = json.loads(out)
    _validate_envelope(env)
    assert env["data"]["failure_class"] == "host_unreachable"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_unknown_phase_falls_through_to_pending() -> None:
    pod_status = {"status": {"phase": "WeirdNewPhase"}, "metadata": {}}
    assert classify_phase(pod_status) == "Pending"


def test_empty_container_statuses() -> None:
    pod_status = {"status": {"phase": "Running", "containerStatuses": []}, "metadata": {}}
    # No containerStatuses → no waiting, no failures, no restartCount → Healthy.
    assert classify_phase(pod_status) == "Running-Healthy"


def test_job_succeeded_maps_to_healthy() -> None:
    pod_status = {"status": {"phase": "Succeeded"}, "metadata": {}}
    assert classify_phase(pod_status) == "Running-Healthy"


def test_job_failed_maps_to_crashloop() -> None:
    pod_status = {"status": {"phase": "Failed"}, "metadata": {}}
    assert classify_phase(pod_status) == "CrashLoopBackOff"


def test_deletion_timestamp_overrides_running() -> None:
    """A Pod with deletionTimestamp + restartCount>0 must still be Terminating, not Running-Restarting."""
    pod_status = {
        "metadata": {"deletionTimestamp": "2026-05-27T10:00:00Z"},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": "app", "ready": True, "restartCount": 3, "state": {"running": {}}}
            ],
        },
    }
    assert classify_phase(pod_status) == "Terminating"


def test_safe_dig_handles_none_mid_path() -> None:
    assert safe_dig({"a": None}, "a", "b") is None
    assert safe_dig({"a": {"b": "c"}}, "a", "b") == "c"
    assert safe_dig(None, "a") is None
    assert safe_dig({}, "x", "y", default="fallback") == "fallback"


# ---------------------------------------------------------------------------
# Sanity: help still works (manifest smoke also covers this, but explicit)
# ---------------------------------------------------------------------------


def test_help_exits_zero(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "usage:" in out
