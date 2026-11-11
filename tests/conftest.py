"""Shared test fixtures and suite-wide guards.

* ``kind_cluster`` (session-scoped): spins up / reuses a kind cluster
  named ``k8s-debug-test`` and sets KUBECONFIG. Skipped if ``kind`` is
  not on PATH so the rest of the suite still runs.
* ``fixture_pods``: applies three Pods to the ``live-k8s-debug`` namespace
  (crashloop / healthy / pending) and waits for them to reach their
  target phases.
* ``pytest_collection_modifyitems``: applies the capability skips once, for the
  whole suite, instead of asking 26 integration modules to copy a guard
  (DEVOPS-GAP-001 / DEVOPS-GAP-010).

Both fixtures are only consumed by tests under ``@pytest.mark.live_k8s``.
"""

from __future__ import annotations

import importlib.util
import json as _json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_CORE_SCRIPTS = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
)

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_support  # noqa: E402  (needs the path above)


def pytest_collection_modifyitems(config, items):
    """Skip capability-gated cases once, centrally.

    Three markers, three meanings, and one precedence rule:

    * ``live_ssh`` -- needs a reachable host. Skipped when ``POC_HOST`` is
      unset or no credential works, with a message that names what is missing.
      Individual modules may add their own finer-grained preconditions, but
      they no longer all invent a slightly different copy of this one.
    * ``sftp_only`` -- asserts the paramiko/SFTP backend. Skipped when
      paramiko is not importable; the scp/tar fallback has its own cases, so a
      paramiko-free machine still exercises a real transport.
    * ``needs_posix`` -- needs a POSIX shell (setsid, mode bits, /tmp paths).
      Skipped on Windows.

    **Precedence: live first.** With no host configured nothing else can run, so
    that is the only reason worth reporting; otherwise a Windows box with no
    paramiko reported "needs a POSIX shell" (or "paramiko is not installed")
    for cases whose real blocker was the missing `POC_HOST`, and the run read
    as if the suite had tested a capability it never reached. When the live
    group *can* run, the two capability markers behave exactly as before.
    """
    has_paramiko = live_support.has_paramiko()
    is_posix = live_support.posix_shell_ok()
    live_reason = None
    live_checked = False

    marker_expr = getattr(config.option, "markexpr", "") or ""
    for item in items:
        keywords = item.keywords
        if "live_k8s" in keywords and "live_k8s" not in marker_expr:
            item.add_marker(
                pytest.mark.skip(
                    reason="live_k8s tests need a kind cluster; run with "
                    "`pytest -m live_k8s`"
                )
            )
            continue
        if "live_ssh" in keywords:
            if not live_checked:
                live_reason = live_support.live_skip_reason()
                live_checked = True
            if live_reason is not None:
                item.add_marker(pytest.mark.skip(reason=live_reason))
                continue
        if "sftp_only" in keywords and not has_paramiko:
            item.add_marker(
                pytest.mark.skip(
                    reason="paramiko is not installed; this case asserts the SFTP "
                    "backend (the scp/tar fallback is covered separately)"
                )
            )
        if "needs_posix" in keywords and not is_posix:
            item.add_marker(
                pytest.mark.skip(reason="needs a POSIX shell (setsid, mode bits, /tmp)")
            )


def _load_ssh_core_lib() -> None:
    """Pin the plugin-private ``lib`` package before any test imports it.

    Several plugins deliberately use the same bare ``lib`` package name.
    The synced ssh-core suite needs its own package for subprocess and
    in-process coverage; loading it once at collection avoids pytest file
    order deciding which plugin wins.
    """
    spec = importlib.util.spec_from_file_location(
        "lib", SSH_CORE_SCRIPTS / "lib" / "__init__.py",
        submodule_search_locations=[str(SSH_CORE_SCRIPTS / "lib")],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load ssh-core lib package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)


_load_ssh_core_lib()
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "k8s"

_KIND_CLUSTER_NAME = "k8s-debug-test"
_LIVE_NS = "live-k8s-debug"


def _have_binary(name: str) -> bool:
    return shutil.which(name) is not None


@pytest.fixture(scope="session")
def kind_cluster():
    """Session-scoped kind cluster.

    Skipped (not failed) when ``kind`` / ``kubectl`` / a running Docker
    daemon is missing, so the default test suite still runs in CI
    without K8s tooling. The cluster is reused across pytest runs;
    explicitly delete with
    ``kind delete cluster --name k8s-debug-test`` to reset.
    """
    if not _have_binary("kind"):
        pytest.skip("kind binary not installed")
    if not _have_binary("kubectl"):
        pytest.skip("kubectl binary not installed")
    if not _have_binary("docker"):
        pytest.skip("docker binary not installed")
    # kind needs a running Docker daemon to provision the node container.
    docker_ok = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, check=False
    )
    if docker_ok.returncode != 0:
        pytest.skip("docker daemon not running (kind needs it)")

    existing = subprocess.run(
        ["kind", "get", "clusters"],
        capture_output=True,
        text=True,
        check=False,
    )
    if _KIND_CLUSTER_NAME not in existing.stdout.splitlines():
        create = subprocess.run(
            ["kind", "create", "cluster", "--name", _KIND_CLUSTER_NAME, "--wait", "120s"],
            capture_output=True,
            text=True,
            check=False,
        )
        if create.returncode != 0:
            pytest.skip(f"kind create cluster failed: {create.stderr[:300]}")

    kubeconfig_dir = Path(tempfile.mkdtemp(prefix="kind-kubeconfig-"))
    cfg_path = kubeconfig_dir / "config"
    subprocess.run(
        [
            "kind",
            "export",
            "kubeconfig",
            "--name",
            _KIND_CLUSTER_NAME,
            "--kubeconfig",
            str(cfg_path),
        ],
        check=True,
    )
    previous = os.environ.get("KUBECONFIG")
    os.environ["KUBECONFIG"] = str(cfg_path)

    yield {"name": _KIND_CLUSTER_NAME, "kubeconfig": cfg_path}

    if previous is None:
        os.environ.pop("KUBECONFIG", None)
    else:
        os.environ["KUBECONFIG"] = previous
    # Cluster intentionally retained between runs for developer speed.


def _wait_for_phase(
    namespace: str, pod: str, expected_predicate, timeout_s: int = 120
) -> None:
    """Poll ``kubectl get pod`` JSON until predicate(pod_json) is True."""
    deadline = time.monotonic() + timeout_s
    last_phase = None
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["kubectl", "-n", namespace, "get", "pod", pod, "-o", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            try:
                data = _json.loads(proc.stdout)
                last_phase = data.get("status", {}).get("phase")
                if expected_predicate(data):
                    return
            except _json.JSONDecodeError:
                pass
        time.sleep(2.0)
    raise AssertionError(
        f"Pod {pod} did not reach expected state within {timeout_s}s "
        f"(last observed phase={last_phase})"
    )


@pytest.fixture
def fixture_pods(kind_cluster):
    """Apply 3 fixture pods and wait for each to reach its target state.

    Yields the namespace + pod names. Namespace is created idempotently
    and reused across tests.
    """
    manifest = FIXTURES_DIR / "live_pods.yaml"
    assert manifest.exists(), f"missing {manifest}"

    # Namespace.
    subprocess.run(
        ["kubectl", "create", "namespace", _LIVE_NS],
        capture_output=True,
        text=True,
        check=False,
    )

    subprocess.run(
        ["kubectl", "apply", "-n", _LIVE_NS, "-f", str(manifest)],
        check=True,
        capture_output=True,
        text=True,
    )

    # Healthy: phase Running + ready=True.
    _wait_for_phase(
        _LIVE_NS,
        "healthy-pod",
        lambda d: d.get("status", {}).get("phase") == "Running"
        and all(
            c.get("ready") for c in d.get("status", {}).get("containerStatuses", []) or []
        ),
        timeout_s=120,
    )

    # CrashLoop: waiting reason CrashLoopBackOff (requires kubelet to back
    # off after a few rapid restarts — typically 30-60s with exit-1 loop).
    _wait_for_phase(
        _LIVE_NS,
        "crashloop-pod",
        lambda d: any(
            (c.get("state", {}) or {}).get("waiting", {}).get("reason") == "CrashLoopBackOff"
            for c in d.get("status", {}).get("containerStatuses", []) or []
        ),
        timeout_s=180,
    )

    # Pending: phase stays Pending.
    _wait_for_phase(
        _LIVE_NS,
        "pending-pod",
        lambda d: d.get("status", {}).get("phase") == "Pending",
        timeout_s=60,
    )

    yield {
        "namespace": _LIVE_NS,
        "pods": ["crashloop-pod", "healthy-pod", "pending-pod"],
    }
    # Pods are retained for inspection between test runs; namespace is
    # cleaned up by `kind delete cluster` or explicit kubectl delete ns.
