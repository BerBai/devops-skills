#!/usr/bin/env python3
"""ssh_cluster.py - Concurrent SSH broadcast across a fleet (v1.4.0).

Public surface::

    ssh_cluster.py <command> [--hosts a,b,c]
                   [--tags t1,t2] [--environment ENV] [--ssh-config PATH]
                   [--stdin-file PATH]
                   [--max-workers N] [--health-check]
                   [--health-check-command CMD] [--probe [tcp:]PORT ...]
                   [--retry N] [--retry-delay S] [--retry-backoff F]
                   [--fail-fast]
                   [-o KEY=VALUE ...]
                   [--timeout T] [--connect-timeout T] [--json]

Behaviour
---------
ssh_cluster does not talk to ssh directly. Each target host is delegated
to a subprocess call of the **same-directory** ``ssh_execute.py``; the
two scripts share a ``lib/`` package for the envelope helpers but never
import each other (see design.md S 7). That keeps failure_class /
connect-timeout / noise filtering single-source in ssh_execute, and
keeps ssh_cluster a pure orchestrator on top.

Two properties the orchestrator has to enforce itself (SSHCORE-018/024):

* ``--ssh-config`` is forwarded to every worker as ``ssh -F``, so the file
  used to *select* a host is the file used to *connect* to it. It used to
  affect selection only, which could silently reach a different machine.
* workers never inherit the caller's stdin (N readers on one descriptor is
  a race, and the losers reported success). ``--stdin-file PATH`` gives each
  worker its own handle to the same bytes.

Per call the workflow is::

    1. resolve targets        --hosts, --tags, --environment (AND-combined)
    2. health-check           `true` (or --health-check-command) + --probe
    3. broadcast              ThreadPoolExecutor(max_workers), with --retry
    4. aggregate              per-host envelopes + top failure_class

Health checking (DV-15)
-----------------------
The old probe was a bare ``true``: it proved *"SSH accepts me"* and nothing
else. A host with a dead nginx behind a healthy sshd looked fine.

* ``--health-check-command "<cmd>"`` replaces ``true`` (e.g.
  ``systemctl is-active nginx``); a non-zero exit marks the host unhealthy and
  skips it in the broadcast phase.
* ``--probe tcp:22`` / ``--probe port:3306`` runs a plain socket connect that
  does **not** need ssh at all, which is what tells you "sshd is wedged but the
  service is up" and vice versa. Probe records land in
  ``data.results[alias].probe``.
* Both run before the broadcast, in parallel, and a host that fails either one
  is reported with ``failure_class: "health_check_failed"`` and
  ``skipped: true``.

Retry (DV-17)
-------------
``--retry N`` re-runs a *host's* broadcast when the failure is transient
(``network`` / ``timeout``). Each host retries independently, so one flaky
node does not force a whole second sweep. ``data.results[alias].attempts``
says how many tries that host needed.

Target resolution (SSHCORE-010): ``--tags`` and ``--environment`` read the
ssh-core comment metadata above each ``Host`` block in ``~/.ssh/config``
(written by ``ssh_config_manager.py``). ``--tags`` uses AND semantics so
``--tags web,prod`` means "has both". All filters intersect: with
``--hosts`` given, tags/environment narrow that list; without it, they
select from the whole config. Wildcard ``Host`` patterns are skipped.

The aggregated envelope follows ``error-handling.md`` and the host loop
contract used by fleet tooling::

    data.command               echoed command
    data.hosts                 resolved aliases, in declaration order
    data.summary               {total, ok, fail, skipped, elapsed_ms}
    data.results               dict[alias, per_host_envelope]
    data.failure_class         null | target_resolution_failed
                                    | ssh_execute_missing
                                    | partial_failure | all_hosts_failed
    data.fail_fast             echoed flag
    data.health_check          echoed flag
    data.health_check_command  echoed command (default "true")
    data.probes                echoed --probe list

Top-level ``success`` is True iff every host envelope says success.
Otherwise the failure_class differentiates partial vs total.

Exit codes follow ``error-handling.md``::

    0    every host succeeded
    1    one or more host failed
    2    argv error
    124  worker subprocess timed out (per-host only; top-level just
         reflects "fail")

See:
    skills/ssh-core/SKILL.md                 — cluster section
    skills/ssh-core/references/workflows.md  — target resolution + fan-out
    skills/ssh-core/references/safety.md     — envelope + exit-code rules
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    CHILD_TEXT_KWARGS,
    append_audit,
    audit_cli_flags,
    audit_enabled,
    emit,
    iter_ssh_config_hosts,
    json_result,
    validate_ssh_option,
)
from lib.cli import SSHArgumentParser, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_DEPENDENCY_MISSING,
    EXIT_INVALID_ARGUMENTS,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_TIMEOUT,
    exit_code_for,
)
from lib.probe import ProbeSpec  # noqa: E402
from lib.probe import cli_flags as probe_cli_flags  # noqa: E402
from lib.probe import parse_probe, probe_all, summarize as summarize_probes  # noqa: E402
from lib.redact import redactor_for  # noqa: E402
from lib.retry import RetryPolicy, retrying  # noqa: E402
from lib.retry import cli_flags as retry_cli_flags  # noqa: E402
from lib.retry import policy_from_args  # noqa: E402

# --- Constants ---------------------------------------------------------------

DEFAULT_MAX_WORKERS = 8       # mirrors sshd MaxStartups defaults
DEFAULT_TIMEOUT = 120         # business command budget (seconds)
DEFAULT_CONNECT_TIMEOUT = 8   # SSH ConnectTimeout passed to ssh_execute
HEALTH_CHECK_TIMEOUT = 5      # short probe command budget
WORKER_TIMEOUT_PADDING = 5    # extra seconds on top of ssh_execute timeout
#: The command used when ``--health-check`` is on and no
#: ``--health-check-command`` was given: proves login works, nothing more.
DEFAULT_HEALTH_CHECK_COMMAND = "true"

# Exit codes: the aggregated envelope reports 0/1, while argv/target errors
# carry their shared-table codes (SSH-19).
EXIT_FAIL = 1
EXIT_ARGS = EXIT_INVALID_ARGUMENTS
EXIT_TARGET_RESOLUTION = EXIT_NOT_FOUND
EXIT_SSH_EXECUTE_MISSING = EXIT_DEPENDENCY_MISSING
EXIT_TIMEOUT_CODE = EXIT_TIMEOUT


# --- ssh_execute.py discovery (same directory) -------------------------------


def _ssh_execute_path() -> Path:
    """Locate ssh_execute.py inside the SAME directory as this script.

    Unlike cross-plugin scripts (tail_log, inspect_container) which walk
    parents[4], ssh_cluster lives next to ssh_execute and uses the
    relative path.
    """
    return Path(__file__).resolve().parent / "ssh_execute.py"


# --- per-host envelope helpers ----------------------------------------------


def _per_host_env(
    success: bool,
    exit_code: int,
    alias: str,
    *,
    elapsed_ms: int,
    stdout: str = "",
    stderr: str = "",
    failure_class: str | None = None,
    skipped: bool = False,
) -> dict[str, Any]:
    """Build the per-host result envelope with the documented field schema.

    ``probe`` / ``health_check`` are added by the callers that fill them, so a
    plain broadcast keeps exactly the original eight fields (and any consumer
    that validates the shape keeps working).
    """
    return {
        "alias": alias,
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_ms": elapsed_ms,
        "failure_class": failure_class,
        "skipped": skipped,
    }


# --- target resolution -------------------------------------------------------


def _parse_targets(
    hosts_arg: str | None,
    tags_arg: str | None,
    env_arg: str | None,
    ssh_config: str | None = None,
) -> tuple[list[str] | None, str | None]:
    """Resolve the target alias list from --hosts / --tags / --environment.

    * ``--hosts`` alone: ordered, deduped alias list (v1.0 behaviour).
    * ``--tags`` / ``--environment``: select from ``~/.ssh/config`` metadata
      (AND across tags, exact match for environment, SSHCORE-010).
    * All three: intersection, preserving the --hosts order.

    Returns ``(aliases, None)`` on success and
    ``(None, "target_resolution_failed")`` when no filter was given or no
    host matched.
    """
    tags = [t.strip() for t in (tags_arg or "").split(",") if t.strip()]
    env = (env_arg or "").strip() or None

    if not hosts_arg and not tags and not env:
        return None, "target_resolution_failed"

    explicit: list[str] | None = None
    if hosts_arg and hosts_arg.strip():
        explicit = []
        seen: set[str] = set()
        for raw in hosts_arg.split(","):
            cleaned = raw.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                explicit.append(cleaned)

    if tags or env:
        candidates = explicit
        if candidates is None:
            candidates = [rec["alias"] for rec in iter_ssh_config_hosts(ssh_config)]
        matched = []
        for record in iter_ssh_config_hosts(ssh_config):
            alias = record["alias"]
            if candidates is not None and alias not in candidates:
                continue
            if env is not None and record.get("environment") != env:
                continue
            host_tags = {
                t.strip() for t in record.get("tags", "").split(",") if t.strip()
            }
            if tags and not all(t in host_tags for t in tags):
                continue
            if alias not in matched:
                matched.append(alias)
        if explicit is not None:
            # Preserve the caller's order, keep only matched entries.
            matched = [alias for alias in explicit if alias in set(matched)]
        if not matched:
            return None, "target_resolution_failed"
        return matched, None

    if not explicit:
        return None, "target_resolution_failed"
    return explicit, None


# --- worker: delegate to ssh_execute.py via subprocess -----------------------


@dataclass(frozen=True)
class AuthOptions:
    """Auth switches forwarded to every ssh_execute.py worker."""

    auth: str = "auto"
    password_env: str = "SSH_PASSWORD"
    accept_new_hostkey: bool = False

    def as_args(self) -> tuple[str, str, bool]:
        return (self.auth, self.password_env, self.accept_new_hostkey)




def _run_one(
    ssh_exec: Path,
    alias: str,
    command: str,
    timeout: int,
    connect_timeout: int,
    ssh_options: list[str] | None = None,
    auth: str = "auto",
    password_env: str = "SSH_PASSWORD",
    accept_new_hostkey: bool = False,
    ssh_config: str | None = None,
    retry: "RetryPolicy | None" = None,
    stdin_file: str | None = None,
) -> dict[str, Any]:
    """Run `command` on `alias` by shelling out to ssh_execute.py.

    Never imports ssh_execute -- argv-list subprocess only. The result is
    normalized into a per-host envelope; failure_class is promoted from
    ssh_execute's envelope when present. ``ssh_options`` (validated ``-o``
    pairs, SSHCORE-009) are forwarded to every host, as are the auth
    switches — a fleet of password-only hosts otherwise has to be resolved
    one alias at a time. ``ssh_config`` is forwarded too (SSHCORE-018): it
    selects the target *and* configures the connection, so the two cannot
    disagree. ``stdin_file`` gives every worker its own stdin (SSHCORE-024).

    ``retry`` (DV-17) re-runs *this host* when the failure is transient, so a
    single flaky node does not force a whole second sweep. ``attempts`` is
    stamped on the envelope.
    """
    policy = retry or RetryPolicy()

    def _attempt(_n: int) -> dict[str, Any]:
        return _run_one_attempt(
            ssh_exec, alias, command, timeout, connect_timeout,
            ssh_options, auth, password_env, accept_new_hostkey, ssh_config,
            stdin_file,
        )

    env = retrying(_attempt, policy) if policy.enabled else _attempt(1)
    if policy.enabled:
        # ``retrying`` stamps the counters on ``data``; mirror them at the top
        # level too, because a per-host envelope's consumers read ``attempts``
        # directly (DV-17 acceptance: "attempts=2").
        env["attempts"] = len(env.get("data", {}).get("retry_history") or []) + 1
        env["retry_history"] = env.get("data", {}).get("retry_history")
    return env


def _run_one_attempt(
    ssh_exec: Path,
    alias: str,
    command: str,
    timeout: int,
    connect_timeout: int,
    ssh_options: list[str] | None = None,
    auth: str = "auto",
    password_env: str = "SSH_PASSWORD",
    accept_new_hostkey: bool = False,
    ssh_config: str | None = None,
    stdin_file: str | None = None,
) -> dict[str, Any]:
    argv = [
        sys.executable,
        str(ssh_exec),
        alias,
        command,
        "--json",
        "--timeout",
        str(timeout),
        "--connect-timeout",
        str(connect_timeout),
        "--auth",
        auth,
        "--password-env",
        password_env,
    ]
    if accept_new_hostkey:
        argv.append("--accept-new-hostkey")
    if ssh_config:
        # Without this the worker re-reads ~/.ssh/config, so an alias that only
        # exists in --ssh-config resolves during target selection and then
        # fails with "Could not resolve hostname" in the worker.
        argv += ["--ssh-config", ssh_config]
    if stdin_file:
        argv += ["--stdin-file", stdin_file]
    for option in ssh_options or []:
        argv += ["-o", option]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            **CHILD_TEXT_KWARGS,
            # SSHCORE-024: never inherit the caller's stdin. N concurrent
            # workers sharing one fd meant the first host to read got the
            # whole payload and the others got EOF — and every one of them
            # reported `success: true`. `--stdin-file` is the explicit
            # alternative: each worker opens its own handle.
            stdin=subprocess.DEVNULL,
            timeout=timeout + connect_timeout + WORKER_TIMEOUT_PADDING,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _per_host_env(
            False,
            EXIT_TIMEOUT_CODE,
            alias,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            failure_class="timeout",
            stderr=(
                f"ssh_cluster: worker timed out after "
                f"{timeout + connect_timeout + WORKER_TIMEOUT_PADDING}s"
            ),
        )
    except (OSError, ValueError) as exc:
        # SSHCORE-016: a worker that cannot even be spawned (or whose output
        # cannot be decoded) must not take the whole batch's envelope down
        # with a traceback.
        return _per_host_env(
            False,
            EXIT_FAIL,
            alias,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            failure_class="ssh_execute_broken",
            stderr=f"ssh_cluster: worker could not be run: {exc}",
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)

    try:
        env = json.loads(proc.stdout or "")
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        # SSHCORE-016: `json.loads(bytes)` sniffs the encoding, so a worker
        # that emitted a non-UTF-8 byte raised UnicodeDecodeError — which is a
        # ValueError but *not* a JSONDecodeError, so it escaped `main()` as a
        # traceback and the whole batch lost its envelope. It is caught here
        # and reported against the one host that produced it.
        return _per_host_env(
            False,
            proc.returncode if proc.returncode != 0 else EXIT_FAIL,
            alias,
            elapsed_ms=elapsed_ms,
            failure_class="ssh_execute_broken",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    inner_data = env.get("data") or {}
    return _per_host_env(
        bool(env.get("success", False)),
        int(env.get("exit_code", EXIT_FAIL)),
        alias,
        elapsed_ms=elapsed_ms,
        stdout=env.get("stdout", "") or "",
        stderr=env.get("stderr", "") or "",
        failure_class=inner_data.get("failure_class"),
    )


# --- health-check phase ------------------------------------------------------


def _health_check(
    ssh_exec: Path,
    aliases: list[str],
    connect_timeout: int,
    max_workers: int,
    ssh_options: list[str] | None = None,
    auth: "AuthOptions | None" = None,
    ssh_config: str | None = None,
    *,
    command: str = DEFAULT_HEALTH_CHECK_COMMAND,
    probes: list[ProbeSpec] | None = None,
    probe_timeout: float = 3.0,
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Probe each host over SSH and/or with raw TCP connects.

    Returns ``(alive, dead, healthy_extra)``:

    * ``alive`` — the subset that responded ok, preserving input order so the
      subsequent broadcast keeps deterministic alias ordering;
    * ``dead`` — failed-probe envelopes keyed by alias;
    * ``healthy_extra`` — the probe verdict for the hosts that *passed*, keyed
      by alias (FA-17). It has to come back from here: the broadcast phase
      re-runs the command and had no access to the probe data, so a healthy
      host used to lose `data.results[alias].probe` — exactly the field
      `--probe` exists to provide.

    ``command`` replaces the old hardcoded ``true`` (DV-15): a health check
    that cannot fail is not a health check. ``probes`` adds transport-level
    liveness that does not depend on sshd at all.
    """
    workers = max(1, min(max_workers, len(aliases)))
    futures: dict[str, Future[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for alias in aliases:
            futures[alias] = pool.submit(
                _probe_host,
                ssh_exec, alias, command, connect_timeout, ssh_options,
                auth or AuthOptions(), ssh_config, probes or [], probe_timeout,
            )
        alive: list[str] = []
        dead: dict[str, dict[str, Any]] = {}
        healthy_extra: dict[str, dict[str, Any]] = {}
        for alias in aliases:
            env = futures[alias].result()
            if env["success"]:
                alive.append(alias)
                extra = {
                    key: env[key] for key in ("probe", "probe_records") if key in env
                }
                if extra:
                    healthy_extra[alias] = extra
            else:
                env["failure_class"] = "health_check_failed"
                env["skipped"] = True
                dead[alias] = env
    return alive, dead, healthy_extra


def _probe_host(
    ssh_exec: Path,
    alias: str,
    command: str,
    connect_timeout: int,
    ssh_options: list[str] | None,
    auth: "AuthOptions",
    ssh_config: str | None,
    probes: list[ProbeSpec],
    probe_timeout: float,
) -> dict[str, Any]:
    """One host's health verdict: TCP probes first, then the SSH check.

    TCP first is deliberate. It is the cheaper question and it distinguishes
    the two failure shapes that matter operationally: "the port is closed"
    (host down / service not listening) versus "the port is open but ssh
    refuses me" (auth, host key, wedged sshd). The SSH check is skipped when a
    probe already failed, because the answer would be noise.
    """
    started = time.monotonic()
    probe_records = probe_all(
        alias, probes, ssh_config=ssh_config, timeout=probe_timeout
    ) if probes else []
    probe_summary = summarize_probes(probe_records) if probe_records else None

    env = _run_one(
        ssh_exec, alias, command,
        HEALTH_CHECK_TIMEOUT, connect_timeout, ssh_options,
        *auth.as_args(),
        ssh_config=ssh_config,
    )
    if probe_summary is not None:
        env["probe"] = probe_summary
        env["probe_records"] = probe_records
        # Merge durations: the TCP probes are part of this host's health cost.
        env["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    env["health_check_command"] = command
    if probe_summary is not None and not probe_summary["ok"] and env["success"]:
        # SSH works but the service port is closed: that is exactly the
        # partial outage the old `true`-only check reported as healthy.
        env["success"] = False
        env["exit_code"] = exit_code_for("health_check_failed")
        env["stderr"] = (
            "ssh_cluster: health check failed at the transport level; closed "
            f"ports: {', '.join(str(t) for t in probe_summary['failed_targets'])}"
        )
    return env


# --- broadcast phase ---------------------------------------------------------


def _broadcast_serial(
    ssh_exec: Path,
    aliases: list[str],
    command: str,
    timeout: int,
    connect_timeout: int,
    fail_fast: bool,
    ssh_options: list[str] | None = None,
    auth: "AuthOptions | None" = None,
    ssh_config: str | None = None,
    retry: "RetryPolicy | None" = None,
    stdin_file: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Sequential broadcast. Preserves alias order. Used when
    max_workers <= 1 so tests can assert deterministic ordering."""
    results: dict[str, dict[str, Any]] = {}
    for idx, alias in enumerate(aliases):
        env = _run_one(
            ssh_exec, alias, command, timeout, connect_timeout, ssh_options,
            *(auth or AuthOptions()).as_args(),
            ssh_config=ssh_config,
            retry=retry,
            stdin_file=stdin_file,
        )
        results[alias] = env
        if fail_fast and not env["success"]:
            for skip in aliases[idx + 1:]:
                results[skip] = _per_host_env(
                    False, EXIT_FAIL, skip,
                    elapsed_ms=0,
                    failure_class="skipped_fail_fast",
                    skipped=True,
                )
            break
    return results


def _broadcast_parallel(
    ssh_exec: Path,
    aliases: list[str],
    command: str,
    timeout: int,
    connect_timeout: int,
    max_workers: int,
    fail_fast: bool,
    ssh_options: list[str] | None = None,
    auth: "AuthOptions | None" = None,
    ssh_config: str | None = None,
    retry: "RetryPolicy | None" = None,
    stdin_file: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Concurrent broadcast via ThreadPoolExecutor.

    fail-fast cancels futures that have NOT yet started; already-started
    futures run to completion (no thread cancellation in stdlib).
    """
    workers = max(1, min(max_workers, len(aliases)))
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[str, Future[dict[str, Any]]] = {
            alias: pool.submit(
                _run_one, ssh_exec, alias, command, timeout, connect_timeout,
                ssh_options, *(auth or AuthOptions()).as_args(),
                ssh_config=ssh_config,
                retry=retry,
                stdin_file=stdin_file,
            )
            for alias in aliases
        }

        if not fail_fast:
            for alias in aliases:
                results[alias] = futures[alias].result()
            return results

        # fail-fast: collect via as_completed, stop on first failure
        first_fail_seen = False
        for fut in as_completed(futures.values()):
            alias = next(a for a, f in futures.items() if f is fut)
            env = fut.result()
            results[alias] = env
            if not env["success"]:
                first_fail_seen = True
                break

        if first_fail_seen:
            for alias, fut in futures.items():
                if alias in results:
                    continue
                if fut.cancel():
                    results[alias] = _per_host_env(
                        False, EXIT_FAIL, alias,
                        elapsed_ms=0,
                        failure_class="skipped_fail_fast",
                        skipped=True,
                    )
                else:
                    # Already running -- wait it out
                    results[alias] = fut.result()
        else:
            # No failure encountered (defensive)
            for alias, fut in futures.items():
                if alias not in results:
                    results[alias] = fut.result()
    return results


def _broadcast(
    ssh_exec: Path,
    aliases: list[str],
    command: str,
    timeout: int,
    connect_timeout: int,
    max_workers: int,
    fail_fast: bool,
    ssh_options: list[str] | None = None,
    auth: "AuthOptions | None" = None,
    ssh_config: str | None = None,
    retry: "RetryPolicy | None" = None,
    stdin_file: str | None = None,
) -> dict[str, dict[str, Any]]:
    if max_workers <= 1 or len(aliases) <= 1:
        return _broadcast_serial(
            ssh_exec, aliases, command, timeout, connect_timeout, fail_fast,
            ssh_options, auth, ssh_config=ssh_config, retry=retry,
            stdin_file=stdin_file,
        )
    return _broadcast_parallel(
        ssh_exec, aliases, command, timeout, connect_timeout,
        max_workers, fail_fast, ssh_options, auth, ssh_config=ssh_config,
        retry=retry, stdin_file=stdin_file,
    )


# --- aggregation -------------------------------------------------------------


def _classify_top(
    results: dict[str, dict[str, Any]],
) -> tuple[bool, str | None]:
    total = len(results)
    if total == 0:
        return False, "target_resolution_failed"
    ok = sum(1 for env in results.values() if env["success"])
    if ok == total:
        return True, None
    if ok == 0:
        return False, "all_hosts_failed"
    return False, "partial_failure"


def _summarize(
    results: dict[str, dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    total = len(results)
    ok = sum(1 for env in results.values() if env["success"])
    skipped = sum(1 for env in results.values() if env.get("skipped"))
    fail = total - ok - skipped
    return {
        "total": total,
        "ok": ok,
        "fail": fail,
        "skipped": skipped,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }


# --- human formatter ---------------------------------------------------------


_STDOUT_HEAD_CAP = 80
_STDERR_HEAD_CAP = 200


def _format_human(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    results = data.get("results") or {}
    lines = [
        f"command:  {data.get('command') or ''}",
        f"hosts:    {len(results)}  "
        f"(ok={summary.get('ok', 0)}  fail={summary.get('fail', 0)}  "
        f"skipped={summary.get('skipped', 0)})",
        f"elapsed:  {summary.get('elapsed_ms', 0)} ms",
        "",
    ]
    for alias, env in results.items():
        tag = "ok  " if env.get("success") else "FAIL"
        head = (env.get("stdout") or "").splitlines()[0] if env.get("stdout") else ""
        if len(head) > _STDOUT_HEAD_CAP:
            head = head[:_STDOUT_HEAD_CAP] + "..."
        fc = env.get("failure_class") or "-"
        lines.append(
            f"  [{tag}] {alias:<24s} "
            f"exit={env.get('exit_code', '?'):<3}  "
            f"elapsed={env.get('elapsed_ms', 0):>5}ms  "
            f"fc={fc:<22s} {head}"
        )
        # Surface sub-host stderr on failure so users don't have to re-run with
        # --json just to read the reason. Only the first line, capped, for FAIL.
        if not env.get("success"):
            stderr_raw = env.get("stderr") or ""
            if stderr_raw.strip():
                first_line = stderr_raw.splitlines()[0]
                if len(first_line) > _STDERR_HEAD_CAP:
                    first_line = first_line[:_STDERR_HEAD_CAP] + "..."
                lines.append(f"          stderr: {first_line}")
    if data.get("failure_class"):
        lines.append("")
        lines.append(f"failure_class: {data['failure_class']}")
    return "\n".join(lines) + "\n"


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="ssh_cluster.py",
        description=(
            "Broadcast one command across a fleet. Delegates each host "
            "to ssh_execute.py via subprocess. Targets come from --hosts "
            "and/or the ~/.ssh/config metadata filters --tags / "
            "--environment (AND-combined)."
        ),
    )
    p.add_argument("command", help="Remote command string (passed to ssh_execute)")
    p.add_argument("--hosts", help="Comma-separated host aliases")
    p.add_argument(
        "--tags",
        help=(
            "Comma-separated tags; matches hosts whose '# tags:' metadata "
            "contains every tag (AND). Resolves against ~/.ssh/config."
        ),
    )
    p.add_argument(
        "--environment",
        help=(
            "Match hosts whose '# environment:' metadata equals this value. "
            "Resolves against ~/.ssh/config."
        ),
    )
    p.add_argument(
        "--ssh-config",
        default=None,
        help=(
            "Config file used BOTH to resolve targets (--tags / --environment) "
            "and to connect: it is forwarded as ssh -F to every worker, so the "
            "host selected is the host connected to (SSHCORE-018). "
            "Default: ~/.ssh/config"
        ),
    )
    p.add_argument(
        "--stdin-file",
        default=None,
        metavar="PATH",
        help=(
            "File piped to every worker's stdin (each host opens its own "
            "handle). Cluster never inherits the caller's stdin: with N "
            "workers sharing one fd only one host would read the data and all "
            "would report success (SSHCORE-024)"
        ),
    )
    p.add_argument(
        "--auth",
        choices=("auto", "key", "password"),
        default="auto",
        help=(
            "Auth routing forwarded to every host: 'auto' reads # auth: "
            "metadata, 'password' uses $SSH_PASSWORD via the pty session"
        ),
    )
    p.add_argument(
        "--password-env",
        default="SSH_PASSWORD",
        help="Environment variable holding the password (default: SSH_PASSWORD)",
    )
    p.add_argument(
        "--accept-new-hostkey",
        action="store_true",
        help="Forward --accept-new-hostkey to every worker (password route)",
    )
    p.add_argument(
        "-o",
        "--ssh-option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra `ssh -o` option forwarded to every host; repeatable",
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help=(
            "Accepted for compatibility; concurrency is governed entirely "
            "by --max-workers and passing --parallel is a no-op."
        ),
    )
    p.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=(
            f"Thread pool size (default: {DEFAULT_MAX_WORKERS}). "
            "Set to 1 for deterministic serial execution."
        ),
    )
    p.add_argument(
        "--health-check",
        action="store_true",
        help=(
            "Probe each host before the broadcast and skip the ones that fail. "
            "The probe is `true` unless --health-check-command says otherwise, "
            "and --probe adds transport-level checks (DV-15)"
        ),
    )
    p.add_argument(
        "--health-check-command",
        default=DEFAULT_HEALTH_CHECK_COMMAND,
        metavar="CMD",
        help=(
            "Command whose non-zero exit marks a host unhealthy, e.g. "
            "'systemctl is-active nginx' (default: true, which only proves "
            "that SSH works) (DV-15)"
        ),
    )
    probe_cli_flags(p)
    p.add_argument(
        "--fail-fast",
        action="store_true",
        help=(
            "Stop submitting new hosts after the first failure. "
            "Already-running workers complete; unstarted hosts get "
            "failure_class=skipped_fail_fast."
        ),
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-host command timeout (default: {DEFAULT_TIMEOUT}s)",
    )
    p.add_argument(
        "--connect-timeout",
        type=int,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=f"SSH ConnectTimeout (default: {DEFAULT_CONNECT_TIMEOUT}s)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the shared JSON output contract on stdout",
    )
    p.add_argument("--version", "-V", action="store_true", help="Print the ssh-core version")
    audit_cli_flags(p)
    retry_cli_flags(p)
    p.add_argument(
        "--redact", dest="redact", action="store_true", default=True,
        help="Hide host identifiers in the envelope (default: on)",
    )
    p.add_argument("--no-redact", dest="redact", action="store_false", help=argparse.SUPPRESS)
    return p


# --- main --------------------------------------------------------------------


def _emit_top_fail(
    args: argparse.Namespace,
    failure_class: str,
    message: str,
    *,
    exit_code: int | None = None,
) -> int:
    data = {
        "command": args.command,
        "hosts": [],
        "summary": {"total": 0, "ok": 0, "fail": 0, "skipped": 0, "elapsed_ms": 0},
        "results": {},
        "failure_class": failure_class,
        "fail_fast": args.fail_fast,
        "health_check": args.health_check,
        "tags": getattr(args, "tags", None),
        "environment": getattr(args, "environment", None),
    }
    code = exit_code if exit_code is not None else EXIT_FAIL
    emit(
        json_result(False, code, stderr=message + "\n", data=data),
        args.json,
        redactor=redactor_for("cluster", args.ssh_config, enabled=getattr(args, "redact", True)),
    )
    return code


def _audit(args: argparse.Namespace, data: dict[str, Any], code: int) -> None:
    """One audit record per broadcast, per host (SSH-25)."""
    if not audit_enabled(getattr(args, "audit", None), default=False):
        return
    for alias, env in (data.get("results") or {}).items():
        append_audit(
            alias,
            {
                "host": alias,
                "route": "cluster",
                "command": args.command,
                "success": env.get("success"),
                "exit_code": env.get("exit_code"),
                "failure_class": env.get("failure_class"),
                "elapsed_ms": env.get("elapsed_ms"),
                "batch_exit_code": code,
            },
        )


def _stdin_file_problem(path: str) -> str | None:
    """Validate ``--stdin-file`` before any host is touched (SSHCORE-024)."""
    target = Path(path).expanduser()
    if not target.exists():
        return f"--stdin-file does not exist: {target}"
    if not target.is_file():
        return f"--stdin-file is not a regular file: {target}"
    return None


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_cluster.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    started_at = time.monotonic()

    for option in args.ssh_option:
        reason = validate_ssh_option(option)
        if reason is not None:
            return _emit_top_fail(
                args, "invalid_option", f"ssh_cluster: {reason}",
                exit_code=exit_code_for("invalid_option"),
            )

    aliases, err = _parse_targets(
        args.hosts, args.tags, args.environment, args.ssh_config,
    )
    if aliases is None:
        return _emit_top_fail(
            args, err or "target_resolution_failed",
            "ssh_cluster: no targets. Pass --hosts, or --tags / --environment "
            "matching ~/.ssh/config metadata.",
            exit_code=EXIT_TARGET_RESOLUTION,
        )

    ssh_exec = _ssh_execute_path()
    if not ssh_exec.exists():
        return _emit_top_fail(
            args, "ssh_execute_missing",
            f"ssh_cluster: ssh_execute.py not found at {ssh_exec}",
            exit_code=EXIT_SSH_EXECUTE_MISSING,
        )

    auth_opts = AuthOptions(
        auth=args.auth,
        password_env=args.password_env,
        accept_new_hostkey=args.accept_new_hostkey,
    )

    probes: list[ProbeSpec] = []
    for raw in args.probe:
        try:
            probes.append(parse_probe(raw))
        except ValueError as exc:
            return _emit_top_fail(
                args, "invalid_arguments", f"ssh_cluster: {exc}",
                exit_code=EXIT_ARGS,
            )

    dead: dict[str, dict[str, Any]] = {}
    healthy_extra: dict[str, dict[str, Any]] = {}
    if args.health_check or probes:
        aliases, dead, healthy_extra = _health_check(
            ssh_exec, aliases, args.connect_timeout, args.max_workers,
            args.ssh_option, auth_opts, ssh_config=args.ssh_config,
            command=args.health_check_command,
            probes=probes,
            probe_timeout=args.probe_timeout,
        )

    retry_policy = policy_from_args(args)
    stdin_file = args.stdin_file
    if stdin_file:
        stdin_problem = _stdin_file_problem(stdin_file)
        if stdin_problem is not None:
            return _emit_top_fail(
                args, "invalid_arguments", f"ssh_cluster: {stdin_problem}",
                exit_code=EXIT_ARGS,
            )
    if aliases:
        results = _broadcast(
            ssh_exec, aliases, args.command, args.timeout,
            args.connect_timeout, args.max_workers, args.fail_fast,
            args.ssh_option, auth_opts, ssh_config=args.ssh_config,
            retry=retry_policy, stdin_file=stdin_file,
        )
    else:
        results = {}

    for alias, env in dead.items():
        results[alias] = env

    # FA-17: every result — healthy or not — carries the probe verdict it was
    # judged by. It used to be attached only inside the health-check phase, so a
    # *healthy* host lost `data.results[alias].probe`, which is exactly the data
    # `--probe` exists to report. The second pass is a fallback for hosts whose
    # probe data predates the health check (e.g. --fail-fast skips).
    if healthy_extra:
        for alias, extra in healthy_extra.items():
            env = results.get(alias)
            if env is not None:
                env.update(extra)
    if probes:
        for alias, env in results.items():
            if "probe" in env:
                continue
            records = probe_all(
                alias, probes, ssh_config=args.ssh_config,
                timeout=args.probe_timeout,
            )
            env["probe"] = summarize_probes(records)
            env["probe_records"] = records

    success, fc = _classify_top(results)
    summary = _summarize(results, started_at)

    data = {
        "command": args.command,
        "hosts": list(results.keys()),
        "summary": summary,
        "results": results,
        "failure_class": fc,
        "fail_fast": args.fail_fast,
        "health_check": args.health_check,
        "health_check_command": args.health_check_command,
        "probes": [spec.as_dict() for spec in probes],
        "retry": retry_policy.describe() if retry_policy.enabled else None,
        "tags": args.tags,
        "environment": args.environment,
        "auth": args.auth,
        "ssh_config": args.ssh_config,
        "stdin": f"file:{stdin_file}" if stdin_file else "none",
    }
    code = EXIT_OK if success else EXIT_FAIL
    _audit(args, data, code)
    stdout = "" if args.json else _format_human(data)
    emit(
        json_result(success, code, stdout=stdout, data=data),
        args.json,
        redactor=redactor_for("cluster", args.ssh_config, enabled=getattr(args, "redact", True)),
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
