"""Transport-independent liveness probes (DV-15).

``ssh_cluster --health-check`` used to run ``true`` over SSH, which answers
exactly one question: *"can I log in?"* It cannot tell you that nginx is dead
behind a perfectly healthy sshd, and it cannot tell you that port 5432 is
refusing connections while SSH still works — the two most common shapes of a
partial outage.

This module adds two cheap probes that do not need a working SSH session:

``tcp:<port>``
    A plain ``socket.connect_ex`` to ``host:port``. Proves the listener is
    accepting, independent of sshd, auth, or ``~/.ssh/config`` reachability
    of the alias.
``port:<n>``
    Alias for ``tcp:<n>``, kept because ``--probe port:22`` reads better in a
    runbook.

The host is resolved with ``ssh -G <alias>`` — a pure local config parse, no
network — so the probe measures the same address the SSH client would use
(``HostName`` + ``Port``), and falls back to ``user@host:port`` parsing when
the alias is not in any config file.

Every probe returns a plain dict so it can be dropped straight into
``data.results[alias]["probe"]``::

    {"kind": "tcp", "target": "<host>:22", "port": 22, "ok": true,
     "latency_ms": 1.4, "error": null}
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import CHILD_TEXT_KWARGS, msys_safe_env

DEFAULT_PROBE_TIMEOUT = 3.0

#: Probe kinds we accept on the command line.
PROBE_KINDS: tuple[str, ...] = ("tcp", "port")


@dataclass(frozen=True)
class ProbeSpec:
    """One parsed ``--probe`` value."""

    kind: str
    port: int
    raw: str

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "port": self.port, "raw": self.raw}


def parse_probe(value: str) -> ProbeSpec:
    """Parse ``tcp:22`` / ``port:2222`` / ``22`` into a :class:`ProbeSpec`.

    Raises ``ValueError`` with an actionable message on anything else — the
    callers turn that into ``failure_class: "invalid_arguments"`` (exit 2).
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty --probe value")
    kind = "tcp"
    port_text = raw
    if ":" in raw:
        prefix, _, rest = raw.partition(":")
        prefix = prefix.strip().lower()
        if prefix not in PROBE_KINDS:
            raise ValueError(
                f"unsupported probe kind {prefix!r} in --probe {raw!r}; "
                f"use one of: {', '.join(PROBE_KINDS)}"
            )
        kind = "tcp"
        port_text = rest.strip()
    if not port_text.isdigit():
        raise ValueError(
            f"--probe {raw!r} needs a numeric port, e.g. --probe tcp:22"
        )
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"--probe port {port} is outside 1-65535")
    return ProbeSpec(kind=kind, port=port, raw=raw)


def _split_literal(alias: str) -> tuple[str, int | None]:
    """Split ``user@host:port`` / ``[v6]:port`` into ``(host, port|None)``.

    Mirrors ``ssh_execute.split_target``: OpenSSH itself does not understand
    ``host:port`` in the destination argument, so a caller using the sugar
    would otherwise be probed against a hostname containing a colon and the
    connect would always fail.
    """
    stripped = alias.split("@")[-1]
    if stripped.startswith("[") and "]:" in stripped:
        closing = stripped.index("]")
        host = stripped[1:closing]
        tail = stripped[closing + 2:]
        return host, int(tail) if tail.isdigit() else None
    if stripped.count(":") == 1:
        head, _, tail = stripped.partition(":")
        if tail.isdigit():
            return head, int(tail)
    return stripped, None


def resolve_endpoint(
    alias: str, ssh_config: str | None = None
) -> tuple[str, int]:
    """Return ``(host, port)`` the SSH client would connect to for ``alias``.

    ``ssh -G`` is a config parse: it never opens a socket, so this still works
    when the host is down (which is exactly when you need the probe). An
    inline ``user@host:port`` is honoured first (OpenSSH would treat the port
    as part of the hostname), then ``ssh -G``, then the raw alias.
    """
    literal_host, literal_port = _split_literal(alias)
    if literal_port is not None:
        return literal_host, literal_port

    host = ""
    port = 22
    argv = ["ssh", "-G"]
    if ssh_config:
        argv += ["-F", ssh_config]
    argv.append(alias)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=msys_safe_env(),
            **CHILD_TEXT_KWARGS,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        for line in proc.stdout.splitlines():
            key, _, value = line.partition(" ")
            key = key.strip().lower()
            value = value.strip()
            if key == "hostname" and value:
                host = value
            elif key == "port" and value.isdigit():
                port = int(value)
    if not host:
        host = literal_host
    return host, port


def tcp_probe(
    host: str,
    port: int,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> dict[str, object]:
    """Connect to ``host:port`` and report latency. Never raises."""
    started = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        rc = sock.connect_ex((host, port))
    except OSError as exc:  # resolution failure, family mismatch, ...
        return {
            "kind": "tcp",
            "target": f"{host}:{port}",
            "port": port,
            "ok": False,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    elapsed = round((time.monotonic() - started) * 1000, 1)
    if rc == 0:
        return {
            "kind": "tcp",
            "target": f"{host}:{port}",
            "port": port,
            "ok": True,
            "latency_ms": elapsed,
            "error": None,
        }
    return {
        "kind": "tcp",
        "target": f"{host}:{port}",
        "port": port,
        "ok": False,
        "latency_ms": elapsed,
        # connect_ex returns an errno; name it so the envelope is readable.
        "error": _errno_name(rc),
    }


def _errno_name(code: int) -> str:
    try:
        import errno as _errno

        return f"{_errno.errorcode.get(code, 'error')} ({code})"
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return f"error ({code})"


def probe_alias(
    alias: str,
    spec: ProbeSpec,
    *,
    ssh_config: str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> dict[str, object]:
    """Resolve ``alias`` and probe it. Returns the probe record."""
    host, config_port = resolve_endpoint(alias, ssh_config)
    # An explicit ``--probe tcp:2222`` overrides the config Port; ``tcp:22``
    # on a host configured for 2222 is a deliberate "is the *standard* port
    # alive" question, so the explicit number always wins.
    port = spec.port or config_port
    record = tcp_probe(host, port, timeout=timeout)
    record["alias"] = alias
    record["resolved_host"] = host
    record["config_port"] = config_port
    return record


def probe_all(
    alias: str,
    specs: list[ProbeSpec],
    *,
    ssh_config: str | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> list[dict[str, object]]:
    """Run every probe for one alias, in order."""
    return [
        probe_alias(alias, spec, ssh_config=ssh_config, timeout=timeout)
        for spec in specs
    ]


def summarize(records: list[dict[str, object]]) -> dict[str, object]:
    """Fold one alias's probe records into a single ok/failed verdict."""
    failed = [r for r in records if not r.get("ok")]
    return {
        "ok": not failed,
        "probes": len(records),
        "failed": len(failed),
        "failed_targets": [r.get("target") for r in failed],
    }


def cli_flags(parser: object) -> None:
    """Add ``--probe`` / ``--probe-timeout`` to an argparse parser."""
    parser.add_argument(  # type: ignore[attr-defined]
        "--probe",
        action="append",
        default=[],
        metavar="[tcp:|port:]PORT",
        help=(
            "Also probe a TCP port without using ssh (repeatable), e.g. "
            "--probe tcp:22 --probe port:3306. Results land in "
            "data.results[alias].probe; a closed port marks the host unhealthy."
        ),
    )
    parser.add_argument(  # type: ignore[attr-defined]
        "--probe-timeout",
        type=float,
        default=DEFAULT_PROBE_TIMEOUT,
        metavar="SECONDS",
        help=f"Per-probe TCP connect timeout (default: {DEFAULT_PROBE_TIMEOUT})",
    )


__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "PROBE_KINDS",
    "ProbeSpec",
    "cli_flags",
    "parse_probe",
    "probe_alias",
    "probe_all",
    "resolve_endpoint",
    "summarize",
    "tcp_probe",
]

# ``Path`` is re-exported for callers that want to pass a config path object.
_ = Path
