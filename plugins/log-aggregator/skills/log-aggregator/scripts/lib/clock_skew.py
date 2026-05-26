"""clock_skew — concurrently probe remote host clocks via ssh-core.

Public surface:
    probe(remote_hosts: list[str], *, timeout: float = 5.0)
        -> dict[str, int | None]

Returns a {host: skew_ms} mapping where positive skew_ms means the remote
clock is ahead of the local clock. None indicates the probe failed for that
host (network, auth, ssh_execute missing, etc.).

Precision is ~±50ms — single sample, RTT/2 correction. Good enough for
cross-source correlation (typical diagnostic windows are >= 30s); a future
v1.1 may add multi-sample NTP-style minimum-RTT selection.

Transport:
    Shells out to ``plugins/ssh-core/.../scripts/ssh_execute.py`` per
    ADR-001 D1 ("subprocess + native ssh, never import paramiko"). The
    probe command is ``date -u +%s.%N`` — a single non-streaming line, so
    ssh_execute's buffered subprocess.run model is a perfect fit.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path

_THIS = Path(__file__).resolve()
# lib/clock_skew.py -> lib -> scripts -> log-aggregator (skill) -> skills
#   -> log-aggregator (plugin) -> plugins
_PLUGINS_DIR = _THIS.parents[5]
SSH_EXECUTE_PATH = (
    _PLUGINS_DIR
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_execute.py"
)


def probe(
    remote_hosts: list[str], *, timeout: float = 5.0
) -> dict[str, int | None]:
    """Concurrently probe each remote host's clock.

    Args:
        remote_hosts: SSH aliases (skip ``"local"`` — caller filters).
        timeout: Per-host subprocess wall-clock cap; the overall pool also
            caps at this value so a single hung host cannot block startup.

    Returns:
        ``{host: skew_ms}`` — int if probe succeeded, None on any failure.
        Unique hosts only (input duplicates are collapsed).
    """
    unique = list({h for h in remote_hosts if h and h != "local"})
    if not unique:
        return {}

    results: dict[str, int | None] = {h: None for h in unique}
    max_workers = min(8, len(unique))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(_probe_one, h, timeout): h for h in unique}
        try:
            for fut in concurrent.futures.as_completed(
                future_map, timeout=timeout + 1.0
            ):
                host = future_map[fut]
                try:
                    results[host] = fut.result()
                except Exception:  # noqa: BLE001 — per-host failure must not propagate
                    results[host] = None
        except concurrent.futures.TimeoutError:
            # Pool-level timeout: any futures still pending stay as None.
            for fut, host in future_map.items():
                if fut.done() and results[host] is None:
                    try:
                        results[host] = fut.result()
                    except Exception:  # noqa: BLE001
                        results[host] = None
    return results


def _probe_one(host: str, timeout: float) -> int:
    """Shell out to ssh_execute.py, run ``date -u +%s.%N``, return skew_ms.

    Skew is computed as ``remote_now - (local_t0 + rtt/2)`` and clamped to
    int milliseconds. Raises on any subprocess / parse error so the caller
    can record the host as None.
    """
    cmd = [
        sys.executable,
        str(SSH_EXECUTE_PATH),
        host,
        "date -u +%s.%N",
        "--json",
        "--timeout",
        str(int(timeout)),
        "--connect-timeout",
        str(min(5, int(timeout))),
    ]
    t0 = time.time()
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout + 2.0, check=False
    )
    t1 = time.time()
    envelope = json.loads(proc.stdout)
    if not envelope.get("success"):
        raise RuntimeError(
            f"ssh_execute failed: rc={envelope.get('exit_code')} "
            f"stderr={envelope.get('stderr')!r}"
        )
    remote_ts = float(envelope.get("stdout", "").strip())
    rtt_half = (t1 - t0) / 2
    local_at_remote_emit = t0 + rtt_half
    return int((remote_ts - local_at_remote_emit) * 1000)
