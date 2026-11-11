"""kubectl wrapper for k8s-debug scripts.

Provides ``run_kubectl(host, namespace, args, ...)`` so individual scripts
do not re-implement the local-vs-alias dispatch, ``--request-timeout``,
or failure classification. The remote branch shells out to
``plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py --json`` rather
than re-implementing SSH (preserves the ADR-001 envelope contract,
ConnectTimeout, BatchMode, noise filtering).

Failure-class namespace (80-87) is per-script and dedicated to k8s-debug.
See ``.trellis/spec/backend/error-handling.md`` exit code table and
``.trellis/tasks/05-26-k8s-debug-diagnose-pod/design.md`` D4.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import _ssh_core_locator as _locator
from . import msys_safe_env

# ---------------------------------------------------------------------------
# SSH_EXECUTE_PATH resolution
# ---------------------------------------------------------------------------
# Delegated to ``_ssh_core_locator`` so the lookup honours DEVOPS_SSH_CORE_DIR
# / DEVOPS_SSH_CORE_SCRIPTS and survives the marketplace layout, where each
# plugin is installed into its own directory instead of a shared plugins/.
SSH_EXECUTE_PATH = _locator.ssh_core_script("ssh_execute.py")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class KubectlResult:
    """Result of a successful run_kubectl call (or a soft 87 failure)."""

    stdout: str
    stderr: str
    returncode: int
    elapsed_s: float
    failure_class: str | None  # None | "metrics_unavailable" for soft 87
    route: str  # "local" | "remote"
    argv: list[str] = field(default_factory=list)


class KubectlError(Exception):
    """Raised when a kubectl call fails with a hard 80-86 failure class."""

    def __init__(self, failure_class: str, exit_code: int, stderr: str) -> None:
        self.failure_class = failure_class
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"kubectl failed [{failure_class}]: {stderr}")


# ---------------------------------------------------------------------------
# Failure-class table
# ---------------------------------------------------------------------------

_FAILURE_CLASS_TO_EXIT = {
    "host_unreachable": 80,
    "kubectl_missing": 81,
    "kubeconfig_missing": 82,
    "pod_not_found": 83,
    "namespace_not_found": 84,
    "forbidden": 85,
    "api_timeout": 86,
    "metrics_unavailable": 87,
}

# Returncode constants — keep the magic numbers out of comparisons.
_RC_COMMAND_NOT_FOUND = 127
_PYTHON_TIMEOUT_EXIT = 124


def _classify_local_failure(returncode: int, stderr: str) -> str | None:  # noqa: PLR0911
    """Map a (returncode, stderr) pair to one of the 8 failure classes.

    Returns None for clean success. Soft failure ``metrics_unavailable``
    is returned as a string even though it does not raise; callers
    decide policy.

    Substring match, order most-specific -> most-general. Excerpts taken
    from real kubectl output (v1.31).
    """
    # Soft failure first — `top` against a cluster without metrics-server.
    # Comes back as exit code 1 with stderr describing the metrics API.
    if "Metrics API not available" in stderr or "metrics.k8s.io" in stderr:
        return "metrics_unavailable"

    # Hard "we never even ran" errors.
    if "command not found" in stderr or returncode == _RC_COMMAND_NOT_FOUND:
        return "kubectl_missing"
    if (
        "KUBECONFIG" in stderr and "does not exist" in stderr
    ) or "no configuration has been provided" in stderr:
        return "kubeconfig_missing"

    # NotFound — distinguish pod vs namespace from the message body.
    if 'pods "' in stderr and "not found" in stderr:
        return "pod_not_found"
    if 'namespaces "' in stderr and "not found" in stderr:
        return "namespace_not_found"

    # RBAC denial. Real kubectl says e.g.
    # "Error from server (Forbidden): pods is forbidden: User ..."
    if "forbidden:" in stderr.lower():
        return "forbidden"

    # kubectl --request-timeout=30s trip.
    if "context deadline exceeded" in stderr or (
        "Unable to connect to the server" in stderr and "timeout" in stderr.lower()
    ):
        return "api_timeout"

    # API-server unreachable on the local route (cluster down, wrong
    # server URL, refused TCP). Semantically same bucket as "remote SSH
    # unreachable" — design.md D4 host_unreachable covers both.
    if "The connection to the server" in stderr and "refused" in stderr:
        return "host_unreachable"
    if "Unable to connect to the server" in stderr:
        return "host_unreachable"
    if "no such host" in stderr and "dial tcp" in stderr:
        return "host_unreachable"

    # Clean success.
    if returncode == 0:
        return None

    # Non-zero exit that doesn't match any pattern: pass through.
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_kubectl_argv(
    args: list[str], namespace: str | None, use_json: bool, timeout: int
) -> list[str]:
    argv = ["kubectl", f"--request-timeout={timeout}s"]
    if namespace is not None:
        argv += ["-n", namespace]
    argv += args
    if use_json and "-o" not in args and "--output" not in args:
        argv += ["-o", "json"]
    return argv


def _run_local(kubectl_argv: list[str], timeout: int) -> KubectlResult:
    """Run kubectl locally via subprocess.

    Wall-clock budget is ``timeout + 5`` (kubectl's own --request-timeout
    is set to ``timeout``; the +5 absorbs DNS/handshake/parse jitter).
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(
            kubectl_argv,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            env=msys_safe_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        # Python-side wall-clock timeout: distinct from kubectl's own
        # api_timeout. Per design.md D4, this is exit 124 (GNU `timeout(1)`
        # convention) rather than 86.
        raise KubectlError(
            "api_timeout",
            _PYTHON_TIMEOUT_EXIT,
            f"kubectl wall-clock timeout after {timeout + 5}s: {e}",
        ) from None
    except FileNotFoundError:
        raise KubectlError(
            "kubectl_missing",
            _FAILURE_CLASS_TO_EXIT["kubectl_missing"],
            "kubectl binary not found in PATH",
        ) from None

    elapsed = round(time.monotonic() - start, 3)
    fc = _classify_local_failure(proc.returncode, proc.stderr)

    # Soft (87) returns a result; hard (80-86) raises.
    if fc is not None and fc != "metrics_unavailable":
        code = _FAILURE_CLASS_TO_EXIT[fc]
        raise KubectlError(fc, code, proc.stderr)

    return KubectlResult(
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
        elapsed_s=elapsed,
        failure_class=fc,
        route="local",
        argv=kubectl_argv,
    )


def _run_remote(host: str, kubectl_argv: list[str], timeout: int) -> KubectlResult:
    """Run kubectl remotely by delegating to ssh_execute.py --json."""
    if not SSH_EXECUTE_PATH.exists():
        raise KubectlError(
            "host_unreachable",
            _FAILURE_CLASS_TO_EXIT["host_unreachable"],
            f"ssh-core ssh_execute.py not found at expected path "
            f"({SSH_EXECUTE_PATH}); install the ssh-core plugin or use host=local.",
        )

    command_str = " ".join(shlex.quote(a) for a in kubectl_argv)
    ssh_argv = [
        sys.executable,
        str(SSH_EXECUTE_PATH),
        host,
        command_str,
        "--timeout",
        str(timeout + 10),
        "--json",
    ]

    start = time.monotonic()
    try:
        proc = subprocess.run(
            ssh_argv,
            capture_output=True,
            text=True,
            timeout=timeout + 15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise KubectlError(
            "host_unreachable",
            _FAILURE_CLASS_TO_EXIT["host_unreachable"],
            f"ssh_execute timeout after {timeout + 15}s",
        ) from None
    elapsed = round(time.monotonic() - start, 3)

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise KubectlError(
            "host_unreachable",
            _FAILURE_CLASS_TO_EXIT["host_unreachable"],
            f"ssh_execute returned non-JSON output: {e}; stderr={proc.stderr[:200]}",
        ) from None

    # Map ssh-core failure_class to our 80-86 namespace.
    ssh_fc = envelope.get("data", {}).get("failure_class")
    if ssh_fc in ("network", "timeout"):
        raise KubectlError(
            "host_unreachable",
            _FAILURE_CLASS_TO_EXIT["host_unreachable"],
            envelope.get("stderr", "ssh transport failed"),
        )
    if ssh_fc == "auth":
        raise KubectlError(
            "forbidden",
            _FAILURE_CLASS_TO_EXIT["forbidden"],
            envelope.get("stderr", "ssh authentication failed"),
        )

    # ssh succeeded → inspect remote kubectl's own exit code and stderr.
    remote_rc = int(envelope.get("exit_code", 0))
    remote_stdout = envelope.get("stdout", "") or ""
    remote_stderr = envelope.get("stderr", "") or ""
    fc = _classify_local_failure(remote_rc, remote_stderr)

    if fc is not None and fc != "metrics_unavailable":
        code = _FAILURE_CLASS_TO_EXIT[fc]
        raise KubectlError(fc, code, remote_stderr)

    return KubectlResult(
        stdout=remote_stdout,
        stderr=remote_stderr,
        returncode=remote_rc,
        elapsed_s=elapsed,
        failure_class=fc,
        route="remote",
        argv=kubectl_argv,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_kubectl(
    host: str,
    namespace: str | None,
    args: list[str],
    *,
    use_json: bool = False,
    timeout: int = 30,
) -> KubectlResult:
    """Run a kubectl invocation on ``host``.

    ``host == "local"`` runs locally via subprocess; any other alias is
    routed through ``ssh_execute.py --json`` so we inherit ADR-001's
    BatchMode + ConnectTimeout + failure-class plumbing.

    Hard failure classes 80-86 raise ``KubectlError``. Soft failure 87
    (``metrics_unavailable``) is returned in the result so callers can
    decide whether to demote it to a warn finding (the diagnose_pod
    policy: ``top`` failure is non-blocking).
    """
    kubectl_argv = _build_kubectl_argv(args, namespace, use_json, timeout)
    if host == "local":
        return _run_local(kubectl_argv, timeout)
    return _run_remote(host, kubectl_argv, timeout)
