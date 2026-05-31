# Design: ssh_execute v1.0 + diagnose_host end-to-end

## Architecture overview

Three layers, top-down:

```
remote-debug/diagnose_host.py            (R3)
        │  subprocess invokes
        ▼
ssh-core/.../ssh_execute.py              (R2)
        │  imports
        ▼
ssh-core/.../lib/__init__.py             (R1)
        │  imports
        ▼
stdlib (subprocess, shlex, json, re)
```

The arrow from `diagnose_host` to `ssh_execute` is a **subprocess**, not
a Python import. This is the load-bearing architectural claim of
`CONTRIBUTING.md:29` ("None of them import ssh-core Python directly").

## R1 — lib/__init__.py expansion

### New public surface (added to docstring)

```python
"""Shared helpers for ssh-core scripts.

Stable surface:
    json_result(success, exit_code, stdout, stderr, data) -> dict
    emit(result, as_json: bool) -> None
    msys_safe_env() -> dict
    alias_state_path(alias, kind) -> Path
    filter_ssh_noise(stderr: str) -> tuple[str, list[str]]    # NEW
    classify_failure(returncode: int, stderr: str) -> str | None  # NEW

Everything else is an implementation detail and may move between versions.
"""
```

### `filter_ssh_noise` design

Module-level constant:

```python
SSH_NOISE_PATTERNS = (
    re.compile(r"^Warning: Permanently added .* to the list of known hosts\.$"),
)
```

Function:

```python
def filter_ssh_noise(stderr: str) -> tuple[str, list[str]]:
    """Split captured ssh stderr into (real_stderr, noise_lines).

    Fail-open: any line not matching SSH_NOISE_PATTERNS stays in
    real_stderr. The library of patterns is intentionally small; missing
    a real error is worse than emitting an extra ssh warning to the user.
    """
    real, noise = [], []
    for line in stderr.splitlines():
        if any(p.match(line) for p in SSH_NOISE_PATTERNS):
            noise.append(line)
        else:
            real.append(line)
    out = "\n".join(real)
    # Preserve a trailing newline iff the input had one and we kept content.
    if real and stderr.endswith("\n"):
        out += "\n"
    return out, noise
```

**Note**: this fixes the spike's dropped-newline bug. Unit tests assert.

### `classify_failure` design

Module-level constants:

```python
NETWORK_PATTERN = re.compile(
    r"Could not resolve hostname|Network is unreachable|"
    r"No route to host|Connection refused|Connection timed out"
)
AUTH_PATTERN = re.compile(
    r"Permission denied \(publickey|"
    r"password authentication failed|Authentication failed"
)
```

The `AUTH_PATTERN` requires `publickey` parenthesis context to avoid
false-positive on remote commands that print "Permission denied" alone.

Function:

```python
def classify_failure(returncode: int, stderr: str) -> str | None:
    if returncode == 0:
        return None
    if returncode == 255:
        if NETWORK_PATTERN.search(stderr):
            return "network"
        if AUTH_PATTERN.search(stderr):
            return "auth"
    return "remote_error"
```

`timeout` is **not** classified here — it's set by the caller (which is
the only code that knows whether `subprocess.TimeoutExpired` was raised).
Documenting this in the docstring.

## R2 — ssh_execute.py v1.0

### File layout

```python
#!/usr/bin/env python3
"""ssh_execute.py — run a command on a remote host (v1.0)."""

from __future__ import annotations
import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import (
    classify_failure,
    emit,
    filter_ssh_noise,
    json_result,
    msys_safe_env,
)

# Module-level constants
SHELL_METACHARS = ("|", ">", "<", ";", "&&", "||", "`", "$(")

# Functions: build_parser, _run_local, _run_remote, main
# (~150 lines total)
```

### `_run_local` design

```python
def _run_local(command: str, timeout: int) -> dict:
    # Refuse shell metacharacters upfront
    for meta in SHELL_METACHARS:
        if meta in command:
            return json_result(
                success=False,
                exit_code=1,
                stderr=(f"local route does not support shell metacharacter '{meta}'. "
                        "Use `bash -c '<your command>'` to opt into a shell."),
                data={"route": "local", "refused_metachar": meta},
            )
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return json_result(False, 1, stderr=f"shlex parse error: {e}",
                           data={"route": "local"})
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=msys_safe_env())
    except subprocess.TimeoutExpired:
        return json_result(False, 124, stderr=f"timed out after {timeout}s",
                           data={"route": "local", "shlex_argv": argv,
                                 "failure_class": "timeout"})
    except FileNotFoundError as e:
        return json_result(False, 127, stderr=f"command not found: {e}",
                           data={"route": "local", "shlex_argv": argv})
    return json_result(
        success=(proc.returncode == 0),
        exit_code=proc.returncode,
        stdout=proc.stdout, stderr=proc.stderr,
        data={"route": "local", "shlex_argv": argv,
              "elapsed_s": round(time.monotonic() - start, 2)},
    )
```

### `_run_remote` design

```python
def _run_remote(host: str, command: str, timeout: int,
                connect_timeout: int, ssh_config: str | None) -> dict:
    argv = ["ssh", "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={connect_timeout}"]
    if ssh_config:
        argv += ["-F", ssh_config]
    argv += [host, command]
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=msys_safe_env())
    except subprocess.TimeoutExpired:
        return json_result(
            False, 124, stderr=f"timed out after {timeout}s",
            data={"route": "remote", "ssh_argv": argv,
                  "elapsed_s": round(time.monotonic() - start, 2),
                  "failure_class": "timeout"},
        )
    real_stderr, noise = filter_ssh_noise(proc.stderr)
    failure_class = classify_failure(proc.returncode, proc.stderr)
    return json_result(
        success=(proc.returncode == 0),
        exit_code=proc.returncode,
        stdout=proc.stdout, stderr=real_stderr,
        data={"route": "remote", "ssh_argv": argv,
              "elapsed_s": round(time.monotonic() - start, 2),
              "ssh_noise_lines": noise,
              "failure_class": failure_class,
              "raw_stderr": proc.stderr},
    )
```

### argparse

```python
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_execute.py")
    p.add_argument("host", help="Host alias from ~/.ssh/config, or 'local'")
    p.add_argument("command", help="Shell command to run on the host")
    p.add_argument("--timeout", type=int, default=120,
                   help="Total wall-clock budget in seconds (default 120)")
    p.add_argument("--connect-timeout", type=int, default=8,
                   help="SSH connect timeout in seconds (default 8)")
    p.add_argument("--ssh-config", default=None,
                   help="Pass -F <path> to ssh (default: ~/.ssh/config)")
    p.add_argument("--json", action="store_true",
                   help="Emit the JSON output envelope")
    return p
```

Default `--timeout 120` matches ADR-001 deferred decision (120 is more
forgiving than PoC's 15 for slow real commands).

## R3 — diagnose_host.py v1.0

### Probe catalog (module constant)

```python
PROBES = {
    "uptime":  "uptime",
    "load":    "cat /proc/loadavg",
    "disk":    "df -P / | tail -1",
    "mem":     "free -m | head -2 | tail -1",
    "zombie":  "ps -eo stat | grep -c '^Z' || true",
}
```

Note `|| true` on `zombie` to avoid grep's exit 1 (no matches) being
treated as command failure.

### ssh_execute discovery

```python
def _ssh_execute_path() -> Path:
    """Locate ssh-core's ssh_execute.py relative to this file.

    Layout:
        plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py
        plugins/remote-debug/skills/remote-debug/scripts/diagnose_host.py
                                                                  ^^ this file
    """
    here = Path(__file__).resolve()
    candidate = here.parents[3] / "ssh-core/skills/ssh-core/scripts/ssh_execute.py"
    if not candidate.exists():
        raise FileNotFoundError(
            f"ssh-core ssh_execute.py not found at {candidate}. "
            "remote-debug requires ssh-core to be installed alongside it."
        )
    return candidate
```

### Probe orchestration

```python
def _run_probe(ssh_exec: Path, host: str, probe_cmd: str) -> dict:
    """Returns the JSON envelope from ssh_execute.py for one probe."""
    proc = subprocess.run(
        ["python3", str(ssh_exec), host, probe_cmd, "--json"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return json_result(False, proc.returncode, stdout=proc.stdout,
                           stderr=f"ssh_execute produced non-JSON output: {proc.stderr}",
                           data={"failure_class": "ssh_execute_broken"})
```

### Severity scoring

```python
def _score(probes: dict) -> str:
    sev = "ok"
    # load
    load = probes.get("load", {}).get("parsed", {}).get("load_1m")
    cores = probes.get("load", {}).get("parsed", {}).get("cores", 1)
    if load is not None and cores:
        if load > 2 * cores: sev = "crit"
        elif load > cores and sev != "crit": sev = "warn"
    # disk
    disk_pct = probes.get("disk", {}).get("parsed", {}).get("use_pct")
    if disk_pct is not None:
        if disk_pct >= 95: sev = "crit"
        elif disk_pct >= 80 and sev != "crit": sev = "warn"
    # zombies
    z = probes.get("zombie", {}).get("parsed", {}).get("count")
    if z is not None and z > 5 and sev != "crit":
        sev = "warn"
    return sev
```

### Parsers (one per probe)

Each parser takes the raw stdout string and returns a dict of structured
fields:

- `load`: split `/proc/loadavg` → `{load_1m, load_5m, load_15m, running, threads}`.
  `cores` comes from `nproc` — added as an implicit probe, OR derived
  from another field — actually simpler to invoke `nproc` as part of
  the load probe: change probe to `nproc; cat /proc/loadavg` and parse
  two lines.
- `disk`: parse `df -P` single line → `{use_pct}` from the 5th column.
- `mem`: parse `free -m | head -2 | tail -1` → `{total_mb, used_mb, free_mb}`.
- `uptime`: keep stdout as-is; no parsed structure required.
- `zombie`: int(stdout.strip()) → `{count}`.

## R5 — Test architecture

### Layout

```
tests/
├── test_manifests.py          (existing, untouched)
├── test_ssh_core_lib.py       (new)
├── test_ssh_execute_unit.py   (new)
├── test_diagnose_host_unit.py (new)
├── test_ssh_execute_integration.py     (new, marked live_ssh)
├── test_diagnose_host_integration.py   (new, marked live_ssh)
└── README.md                  (new — explains live_ssh)
```

### pyproject.toml addition

```toml
[tool.pytest.ini_options]
markers = [
    "live_ssh: integration tests that ssh to a real host (pai). Skipped by default. Run with: pytest -m live_ssh",
]
```

Update `[tool.ruff]` if needed (no expected new violations).

### Mocking strategy for unit tests

`subprocess.run` is mocked at the module level via `monkeypatch.setattr`.
Each test supplies a fake `subprocess.CompletedProcess` with known
returncode/stdout/stderr and asserts on the resulting envelope.

For timeout: `monkeypatch.setattr(subprocess, "run", raise_timeout_expired)`.
For local route refusal: no mock needed — refusal happens before
subprocess invocation.

### Live tests

Each `live_ssh`-marked test:

- Imports the script as a module via `subprocess` (don't import the
  script's main into pytest — keep CLI surface as the system boundary).
- Asserts JSON envelope shape AND envelope values.
- One test per PoC hypothesis (H1-H6 re-run as automated tests).

## Cross-cutting decisions

### Top-level main() pattern

Every script follows this template (already used by stubs, preserved):

```python
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = ...
    emit(result, as_json=args.json)
    return result["exit_code"]

if __name__ == "__main__":
    sys.exit(main())
```

### Error envelope structure

Always populate `data.failure_class` for non-success outcomes. Callers
who don't care about classification can ignore `data`; the top-level
`success` and `exit_code` still convey the basic info.

### Where v0.2 stubs differ from v1.0

| Aspect | v0.2 stub | v1.0 |
|--------|-----------|------|
| Return | `exit_code=2`, "not implemented" stderr | Real result |
| `data` payload | empty | populated (failure_class, etc.) |
| argparse signature | already final | unchanged |
| `--help` text | already informative | minor wording cleanup ok |

The `UNIMPLEMENTED_NOTE` constants in `ssh_execute.py` and
`diagnose_host.py` are **removed** (no longer needed). Their absence is
a marker that the script is implemented; the other 35 scripts still
have them. `error-handling.md` already explains this transition.

## Rejected designs

- **Async / asyncio for parallel probes.** Sequential is fine: 5 probes
  × ~250ms = ~1.5s, acceptable for a diagnostic tool. asyncio adds test
  complexity and a third concurrency model (subprocess, asyncio, threads).
- **Custom protocol between diagnose_host and ssh_execute.** Just shell
  out with `--json`. The JSON envelope IS the protocol.
- **In-Python probe definitions in shared lib.** Each plugin owns its
  probes; sharing them centralizes plugin logic in `ssh-core`, which
  contradicts "one plugin per problem domain" (README design principles).

## Rollback / partial-completion plan

This task can be partially rolled back by reverting individual files:

- If R1 lib expansion breaks something: `git checkout
  plugins/ssh-core/.../lib/__init__.py` restores v0.2.
- If R2 ssh_execute v1.0 produces wrong behavior: same — file scope.
- If R3 diagnose_host shell-out doesn't work: ssh_execute v1.0 still
  ships standalone; diagnose_host reverts to stub.
- Tests can be deleted independently.

There is **no** commit that "lands all of v1.0" — each script is its
own commit (see implement.md).
