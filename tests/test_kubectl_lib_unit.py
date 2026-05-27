"""Unit tests for k8s-debug/scripts/lib/kubectl.py.

subprocess is mocked via ``monkeypatch.setattr`` so these run without any
real kubectl / network / ssh. See design.md §5 (kubectl-lib group).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "k8s-debug" / "skills" / "k8s-debug" / "scripts"

# Named exit codes referenced in assertions (avoid PLR2004 magic-value warnings).
EXIT_HOST_UNREACHABLE = 80
EXIT_KUBECTL_MISSING = 81
EXIT_POD_NOT_FOUND = 83
EXIT_NAMESPACE_NOT_FOUND = 84
EXIT_FORBIDDEN = 85
EXIT_PYTHON_TIMEOUT = 124

# Each plugin maintains its own `lib`; isolate from any other plugin's lib
# already cached in sys.modules (e.g. ssh-core's lib loaded by another test).
_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import kubectl as kubectl_mod  # noqa: E402, I001
from lib.kubectl import (  # noqa: E402
    SSH_EXECUTE_PATH,
    KubectlError,
    KubectlResult,
    _build_kubectl_argv,
    _classify_local_failure,
    run_kubectl,
)

# Restore any prior lib modules + drop the SCRIPTS_DIR off sys.path so that
# subsequent test modules using `from lib import X` see their own plugin.
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# _classify_local_failure (8 buckets)
# ---------------------------------------------------------------------------


def test_classify_success_returns_none() -> None:
    assert _classify_local_failure(0, "") is None


def test_classify_metrics_unavailable() -> None:
    stderr = "error: Metrics API not available\n"
    assert _classify_local_failure(1, stderr) == "metrics_unavailable"


def test_classify_kubectl_missing_command_not_found() -> None:
    stderr = "/bin/sh: kubectl: command not found\n"
    assert _classify_local_failure(127, stderr) == "kubectl_missing"


def test_classify_kubectl_missing_returncode_127() -> None:
    assert _classify_local_failure(127, "") == "kubectl_missing"


def test_classify_kubeconfig_missing() -> None:
    stderr = "error: KUBECONFIG /tmp/no.yaml does not exist\n"
    assert _classify_local_failure(1, stderr) == "kubeconfig_missing"


def test_classify_kubeconfig_no_configuration() -> None:
    stderr = "error: no configuration has been provided, try setting KUBERNETES_MASTER environment variable\n"
    assert _classify_local_failure(1, stderr) == "kubeconfig_missing"


def test_classify_pod_not_found() -> None:
    stderr = 'Error from server (NotFound): pods "missing" not found\n'
    assert _classify_local_failure(1, stderr) == "pod_not_found"


def test_classify_namespace_not_found() -> None:
    stderr = 'Error from server (NotFound): namespaces "missing-ns" not found\n'
    assert _classify_local_failure(1, stderr) == "namespace_not_found"


def test_classify_forbidden() -> None:
    stderr = 'Error from server (Forbidden): pods is forbidden: User "u" cannot list resource\n'
    assert _classify_local_failure(1, stderr) == "forbidden"


def test_classify_api_timeout_context_deadline() -> None:
    stderr = "Error: Unable to connect to the server: context deadline exceeded\n"
    assert _classify_local_failure(1, stderr) == "api_timeout"


def test_classify_host_unreachable_connection_refused() -> None:
    stderr = "The connection to the server localhost:8080 was refused\n"
    assert _classify_local_failure(1, stderr) == "host_unreachable"


def test_classify_unknown_returns_none() -> None:
    stderr = "some completely unhandled error message we have never seen\n"
    assert _classify_local_failure(1, stderr) is None


# ---------------------------------------------------------------------------
# _build_kubectl_argv
# ---------------------------------------------------------------------------


def test_build_argv_basic() -> None:
    argv = _build_kubectl_argv(["get", "pod", "p"], "ns", use_json=False, timeout=30)
    assert argv == ["kubectl", "--request-timeout=30s", "-n", "ns", "get", "pod", "p"]


def test_build_argv_no_namespace() -> None:
    argv = _build_kubectl_argv(["get", "ns"], None, use_json=False, timeout=10)
    assert "-n" not in argv
    assert argv == ["kubectl", "--request-timeout=10s", "get", "ns"]


def test_build_argv_adds_json_when_missing() -> None:
    argv = _build_kubectl_argv(["get", "pod", "p"], "ns", use_json=True, timeout=30)
    assert argv[-2:] == ["-o", "json"]


def test_build_argv_no_double_json() -> None:
    argv = _build_kubectl_argv(
        ["get", "pod", "p", "-o", "yaml"], "ns", use_json=True, timeout=30
    )
    # User already supplied -o; we don't append again.
    assert argv.count("-o") == 1


# ---------------------------------------------------------------------------
# _run_local — happy path + failures
# ---------------------------------------------------------------------------


def test_run_local_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _completed(0, stdout='{"items": []}', stderr="")

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    r = run_kubectl("local", "default", ["get", "pods"], use_json=True, timeout=30)
    assert isinstance(r, KubectlResult)
    assert r.returncode == 0
    assert r.failure_class is None
    assert r.route == "local"
    assert seen["argv"][0] == "kubectl"
    assert "-n" in seen["argv"]
    assert "default" in seen["argv"]


def test_run_local_kubectl_missing_raises_81(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_fnf(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "kubectl")

    monkeypatch.setattr(kubectl_mod.subprocess, "run", raise_fnf)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("local", None, ["get", "ns"])
    assert excinfo.value.failure_class == "kubectl_missing"
    assert excinfo.value.exit_code == EXIT_KUBECTL_MISSING


def test_run_local_pod_not_found_raises_83(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = 'Error from server (NotFound): pods "p" not found\n'

    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="", stderr=stderr)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("local", "default", ["get", "pod", "p"])
    assert excinfo.value.failure_class == "pod_not_found"
    assert excinfo.value.exit_code == EXIT_POD_NOT_FOUND


def test_run_local_namespace_not_found_raises_84(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = 'Error from server (NotFound): namespaces "n" not found\n'

    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="", stderr=stderr)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("local", None, ["get", "ns", "n"])
    assert excinfo.value.failure_class == "namespace_not_found"
    assert excinfo.value.exit_code == EXIT_NAMESPACE_NOT_FOUND


def test_run_local_forbidden_raises_85(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = 'Error from server (Forbidden): pods is forbidden: User "x" cannot list\n'

    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="", stderr=stderr)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("local", "ns", ["get", "pods"])
    assert excinfo.value.failure_class == "forbidden"
    assert excinfo.value.exit_code == EXIT_FORBIDDEN


def test_run_local_wall_clock_timeout_raises_124(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(kubectl_mod.subprocess, "run", raise_timeout)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("local", "ns", ["get", "pods"], timeout=5)
    # Per design D4: wall-clock timeout is exit 124 (GNU timeout convention)
    # even though failure_class is api_timeout.
    assert excinfo.value.exit_code == EXIT_PYTHON_TIMEOUT
    assert excinfo.value.failure_class == "api_timeout"


def test_run_local_metrics_unavailable_returns_soft_87(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft 87: returns a result with failure_class set, does NOT raise."""

    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="", stderr="error: Metrics API not available\n")

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    r = run_kubectl("local", "ns", ["top", "pod", "p"])
    assert isinstance(r, KubectlResult)
    assert r.failure_class == "metrics_unavailable"


# ---------------------------------------------------------------------------
# _run_remote — envelope mapping
# ---------------------------------------------------------------------------


def _envelope(
    success: bool = True,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    failure_class: str | None = None,
) -> str:
    return json.dumps(
        {
            "success": success,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "data": {"failure_class": failure_class, "elapsed_s": 0.1},
        }
    )


def test_run_remote_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope(success=True, exit_code=0, stdout='{"items": []}')

    def fake_run(argv, **kwargs):
        # Confirm we shell out to ssh_execute.py with --json
        assert "--json" in argv
        assert str(SSH_EXECUTE_PATH) in argv
        return _completed(0, stdout=payload)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    r = run_kubectl("bastion", "default", ["get", "pods"], use_json=True)
    assert isinstance(r, KubectlResult)
    assert r.route == "remote"
    assert r.returncode == 0
    assert '"items"' in r.stdout


def test_run_remote_network_maps_to_80(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope(
        success=False,
        exit_code=255,
        stderr="ssh: Could not resolve hostname",
        failure_class="network",
    )

    def fake_run(*_args, **_kwargs):
        return _completed(0, stdout=payload)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("bastion", "ns", ["get", "pods"])
    assert excinfo.value.failure_class == "host_unreachable"
    assert excinfo.value.exit_code == EXIT_HOST_UNREACHABLE


def test_run_remote_timeout_maps_to_80(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope(
        success=False, exit_code=124, stderr="timed out", failure_class="timeout"
    )

    def fake_run(*_args, **_kwargs):
        return _completed(0, stdout=payload)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("bastion", "ns", ["get", "pods"])
    assert excinfo.value.failure_class == "host_unreachable"


def test_run_remote_auth_maps_to_85(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope(
        success=False,
        exit_code=255,
        stderr="Permission denied (publickey)",
        failure_class="auth",
    )

    def fake_run(*_args, **_kwargs):
        return _completed(0, stdout=payload)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("bastion", "ns", ["get", "pods"])
    assert excinfo.value.failure_class == "forbidden"
    assert excinfo.value.exit_code == EXIT_FORBIDDEN


def test_run_remote_remote_kubectl_pod_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ssh succeeded, but remote kubectl returned pod_not_found."""
    payload = _envelope(
        success=False,
        exit_code=1,
        stderr='Error from server (NotFound): pods "missing" not found',
        failure_class="remote_error",
    )

    def fake_run(*_args, **_kwargs):
        return _completed(0, stdout=payload)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("bastion", "ns", ["get", "pod", "missing"])
    assert excinfo.value.failure_class == "pod_not_found"
    assert excinfo.value.exit_code == EXIT_POD_NOT_FOUND


def test_run_remote_invalid_json_raises_80(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ssh_execute returns non-JSON, surface as host_unreachable."""

    def fake_run(*_args, **_kwargs):
        return _completed(0, stdout="not json at all", stderr="")

    monkeypatch.setattr(kubectl_mod.subprocess, "run", fake_run)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("bastion", "ns", ["get", "pods"])
    assert excinfo.value.failure_class == "host_unreachable"


def test_run_remote_subprocess_timeout_raises_80(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh_execute", timeout=1)

    monkeypatch.setattr(kubectl_mod.subprocess, "run", raise_timeout)
    with pytest.raises(KubectlError) as excinfo:
        run_kubectl("bastion", "ns", ["get", "pods"])
    assert excinfo.value.failure_class == "host_unreachable"


# ---------------------------------------------------------------------------
# Error type & resolver
# ---------------------------------------------------------------------------


def test_kubectl_error_carries_fields() -> None:
    err = KubectlError("pod_not_found", EXIT_POD_NOT_FOUND, "stderr text")
    assert err.failure_class == "pod_not_found"
    assert err.exit_code == EXIT_POD_NOT_FOUND
    assert err.stderr == "stderr text"
    assert "pod_not_found" in str(err)


def test_ssh_execute_path_resolves_to_existing_file() -> None:
    """SSH_EXECUTE_PATH must point at the real ssh-core script."""
    assert SSH_EXECUTE_PATH.exists(), f"SSH_EXECUTE_PATH stale: {SSH_EXECUTE_PATH}"
    assert SSH_EXECUTE_PATH.name == "ssh_execute.py"
