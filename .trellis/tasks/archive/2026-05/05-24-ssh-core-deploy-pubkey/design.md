# Design — deploy_pubkey v1.0

## Architecture overview

```
              ┌────────────────────────────────────┐
CLI args ───▶ │ argparse.build_parser              │
              └─────────┬──────────────────────────┘
                        │
                        ▼
              ┌────────────────────────────────────┐
              │ preflight()                        │
              │   - resolve & expanduser --key     │
              │   - check key file exists & read   │
              │   - check stdin.isatty()           │
              │   - check shutil.which("ssh-copy-id")│
              └─────────┬──────────────────────────┘
                        │
                fail ◄──┤ (returns failure envelope)
                ok  ────┤
                        ▼
              ┌────────────────────────────────────┐
              │ build_ssh_copy_id_argv()           │
              │   - flags → -o KEY=VALUE / -F PATH │
              │   - --accept-new-hostkey opt-in    │
              └─────────┬──────────────────────────┘
                        │
                        ▼
              ┌────────────────────────────────────┐
              │ subprocess.run(argv, timeout=...)  │
              │   - stdin=None (tty inherited)     │
              │   - stdout=PIPE, stderr=PIPE       │
              │   - env=msys_safe_env()            │
              └─────────┬──────────────────────────┘
                        │
                        ▼
              ┌────────────────────────────────────┐
              │ classify_result()                  │
              │   - rc == 0 → parse stderr for     │
              │       outcome (installed |          │
              │       already_installed) +         │
              │       keys_added                   │
              │   - rc != 0 → match stderr against │
              │       failure_class regex table    │
              └─────────┬──────────────────────────┘
                        │
                        ▼
              ┌────────────────────────────────────┐
              │ lib.emit(json_result(...))         │
              └────────────────────────────────────┘
```

Single-process, single-syscall-tree. No daemon, no paramiko, no state files. Everything is a function over the (argparse namespace, environment) pair.

## Module structure

```python
# deploy_pubkey.py
def build_parser() -> argparse.ArgumentParser: ...

# preflight + arg munging
def _resolve_key(path: str) -> Path: ...
def _preflight(args: argparse.Namespace) -> dict | None:
    """Return a failure envelope dict if preflight should refuse; None if ok."""

# ssh-copy-id argv assembly
def _build_argv(args: argparse.Namespace, key_path: Path) -> list[str]: ...

# subprocess wrapper
def _run_ssh_copy_id(argv: list[str], timeout: int) -> tuple[int, str, str, float]:
    """Returns (returncode, stdout, stderr, elapsed_s). Raises nothing.
    Catches TimeoutExpired and surfaces it via a sentinel returncode (-1) +
    a `timeout` marker in stderr, leaving classification to the caller."""

# result classification
def _parse_success_outcome(stderr: str) -> tuple[str, int | None]:
    """For rc==0: return (outcome, keys_added).
    outcome ∈ {'installed', 'already_installed'}."""

def _classify_failure(rc: int, stderr: str, is_tty: bool, timed_out: bool) -> str:
    """Return one of the failure_class strings in PRD D4."""

# envelope construction
def _success_envelope(args, key_path, outcome, keys_added, argv, elapsed, raw_stderr) -> dict: ...
def _failure_envelope(args, key_path, fclass, argv, elapsed, raw_stderr, hint) -> dict: ...

# main
def main(argv: list[str] | None = None) -> int: ...
```

~12 functions, all small (≤ 30 LOC each). Module total ≈ 280-360 LOC including the ~80-line module docstring + argparse blob.

## ssh-copy-id argv construction

```python
def _build_argv(args, key_path):
    argv = ["ssh-copy-id", "-i", str(key_path)]

    # -F is positional vs -o:  ssh-copy-id passes -F directly to underlying ssh.
    if args.ssh_config:
        argv += ["-o", "BatchMode=no"]      # we WANT prompts (password)
        # ssh-copy-id 's -F lives in the wrapped-ssh argv; pass via -o IdentityAgent? No — use the documented passthrough:
        # ssh-copy-id has its own -o accumulator that proxies to ssh.
        # For -F we use the environment-level: -F is plain ssh option, so pass through `-o` would fail. Solution: ssh-copy-id accepts -F via positional argv before <alias>.
        argv += ["-F", args.ssh_config]

    argv += ["-o", f"ConnectTimeout={args.connect_timeout}"]

    if args.accept_new_hostkey:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]

    argv += [args.alias]
    return argv
```

Notes:
- `ssh-copy-id`'s own `-F`, `-i`, `-o` are passed straight through to the underlying ssh invocation (per ssh-copy-id(1) manpage).
- We never pass `-o BatchMode=yes`. The point of deploy_pubkey is exactly the bootstrap password prompt — BatchMode would silently fail before the user sees the prompt.
- `stdin=None` (parent's stdin inherits) is what makes the tty prompt work; setting `stdin=PIPE` would break it.

## stderr parsing

`ssh-copy-id` writes one of three signal lines on a successful run, depending on outcome (verified on OpenSSH 8.x/9.x):

```
Number of key(s) added: 1
```
→ `outcome = "installed"`, `keys_added = 1`.

```
INFO: Source of key(s) to be installed: "/Users/.../id_ed25519.pub"
ERROR: All keys were skipped because they already exist on the remote system.
WARNING: All keys were skipped because they already exist on the remote system.
(if you think this is a mistake, you may want to use -f option)
```
→ Either ERROR or WARNING flavor; both mean `outcome = "already_installed"`, `keys_added = 0`.

Note that ssh-copy-id sometimes exits **non-zero** when *all* keys are already installed (older OpenSSH builds). We special-case that: rc != 0 with "All keys were skipped because they already exist" anywhere in stderr → reclassify as success / `already_installed`. This is **idempotency over exit code**.

Regex set (compiled at import):

```python
_INSTALLED_RE = re.compile(r"Number of key\(s\) added:\s*(\d+)")
_ALREADY_RE   = re.compile(r"All keys were skipped because they already exist")
```

## Failure-class regex table (per PRD D4)

| `failure_class` | Trigger condition |
|---|---|
| `tool_missing` | `shutil.which("ssh-copy-id") is None` (caught in preflight, never spawns subprocess) |
| `key_file_missing` | `Path(args.key).expanduser()` does not exist OR not readable (caught in preflight) |
| `no_tty_for_password` | `not sys.stdin.isatty()` AND `rc != 0` AND stderr matches password-prompt-fail patterns (`Permission denied, please try again` / `password:` / `Authentication failed`) |
| `auth` | `rc != 0` AND stderr matches `Permission denied (publickey,password,...)` AND no `no_tty_for_password` |
| `host_key_unknown` | `rc != 0` AND stderr matches `Host key verification failed` / `No matching host key` |
| `network` | `rc != 0` AND stderr matches `Could not resolve hostname` / `Connection refused` / `Connection timed out` / `Network is unreachable` |
| `timeout` | `subprocess.TimeoutExpired` raised |
| `remote_error` | `rc != 0` AND no match above (catch-all) |

Regex precedence is **as listed**: first match wins. `tool_missing` and `key_file_missing` short-circuit before subprocess runs.

```python
_AUTH_RE          = re.compile(r"Permission denied \([^)]*\)")
_HOST_KEY_RE      = re.compile(r"(Host key verification failed|No matching host key)")
_NETWORK_RE       = re.compile(r"(Could not resolve hostname|Connection refused|Connection timed out|Network is unreachable)")
_PROMPT_FAIL_RE   = re.compile(r"(Permission denied, please try again|password:|Authentication failed)")
```

## Exit-code pin

| failure_class | exit_code |
|---|---|
| `null` (success) | 0 |
| `tool_missing` | 10 |
| `key_file_missing` | 11 |
| `no_tty_for_password` | 12 |
| `auth` | 13 |
| `host_key_unknown` | 14 |
| `network` | 15 |
| `timeout` | 124 |
| `remote_error` | 1 |

To be verified at implement.md phase D1 against current `error-handling.md`. ssh_config_manager uses 4-8, sftp_transport uses 9, ssh_execute uses 124. 10-15 is a fresh contiguous range above all of those.

## JSON envelope (PRD D6 expansion)

```python
def _success_envelope(args, key_path, outcome, keys_added, argv, elapsed, raw_stderr):
    return json_result(
        success=True,
        exit_code=0,
        stdout="",
        stderr="",  # human summary added below for non-JSON mode
        data={
            "alias": args.alias,
            "key_path": str(key_path),
            "outcome": outcome,                          # "installed" | "already_installed"
            "keys_added": keys_added,                    # int (0 for already_installed)
            "failure_class": None,
            "ssh_copy_id_argv": argv,
            "elapsed_s": elapsed,
            "raw_stderr": raw_stderr,
        },
    )

def _failure_envelope(args, key_path, fclass, argv, elapsed, raw_stderr, hint):
    return json_result(
        success=False,
        exit_code=_EXIT_CODES[fclass],
        stdout="",
        stderr=hint,                                     # short, actionable
        data={
            "alias": args.alias,
            "key_path": str(key_path),
            "outcome": None,
            "keys_added": None,
            "failure_class": fclass,
            "ssh_copy_id_argv": argv,                    # may be [] if preflight refused
            "elapsed_s": elapsed,
            "raw_stderr": raw_stderr,
        },
    )
```

Human-mode hint strings (one short line each):

| failure_class | hint to stderr |
|---|---|
| `tool_missing` | `"ssh-copy-id not found on PATH. Install openssh-client (linux) or openssh (macOS via brew)."` |
| `key_file_missing` | `"public key not found: {path}. Generate with `ssh-keygen` or pass --key <path>."` |
| `no_tty_for_password` | `"first-touch deploy requires an interactive terminal for the password prompt. Re-run from a shell, or run `ssh-copy-id` manually then re-invoke deploy_pubkey."` |
| `auth` | `"authentication to {alias} failed. Check the password or use --accept-new-hostkey if first connection."` |
| `host_key_unknown` | `"{alias}'s host key is not in ~/.ssh/known_hosts. Re-run with --accept-new-hostkey to trust on first use."` |
| `network` | `"could not reach {alias}: {first stderr line}"` |
| `timeout` | `"deploy_pubkey timed out after {timeout}s while talking to {alias}"` |
| `remote_error` | `"ssh-copy-id failed with exit {rc}. See data.raw_stderr for full output."` |

Human-mode success: one line `"installed key on {alias} (key={key_basename}, keys_added={N})"` or `"key already on {alias} (key={key_basename})"`.

## Test strategy

### Unit (`tests/test_deploy_pubkey_unit.py`)

All scenarios mock `subprocess.run` + `shutil.which` + `Path.exists/is_file`. Goal ≥ 90 % statement coverage, ≥ 32 cases.

| Group | Cases | Notes |
|---|---|---|
| build_parser | 4 | all flags / required arg / unknown flag / `--help` exit 0 |
| _resolve_key | 3 | `~/.ssh/id_ed25519.pub` default / explicit absolute / relative path |
| preflight pass-through | 1 | happy path returns None |
| preflight refusals | 3 | tool_missing / key_file_missing (does-not-exist) / key_file_missing (not-readable, mock chmod 000) |
| _build_argv | 6 | no flags / --connect-timeout 30 / --ssh-config x / --accept-new-hostkey / all flags together / `-i` always first |
| _run_ssh_copy_id | 3 | rc==0 path / rc!=0 path / TimeoutExpired surfaced |
| _parse_success_outcome | 4 | "Number of key(s) added: 1" / "...added: 3" / "All keys were skipped" / mixed (added + skipped) |
| _classify_failure | 8 | one per failure_class regex match + remote_error catch-all + no_tty special case |
| Idempotency special-case | 2 | rc != 0 + "All keys were skipped" stderr → reclassify as success |
| main() integration | 6 | success-installed / success-already_installed / each failure_class wired correctly (smoke through main) |
| `--json` envelope | 3 | success envelope fields / failure envelope fields / human-mode (no --json) writes stderr hint |

Total ≈ **43 cases**, plenty above ssh_execute's 30 + sftp_transport's modest counts.

### Integration (`tests/test_deploy_pubkey_integration.py`, `-m live_ssh`)

Per PRD D7: three cases, all non-mutating, all run < 1 s. No real ssh-copy-id invocation against POC_HOST.

| Case | Setup | Assertion |
|---|---|---|
| `--help` exits 0 | none | manifest-style smoke; argparse parses |
| non-existent alias | `deploy_pubkey.py nonexistent-alias-xyz123` (with `--connect-timeout=2`) | exit_code == 15 (`network`); data.failure_class == "network"; elapsed < 5s |
| `--key /nonexistent` | `deploy_pubkey.py pai --key /tmp/does-not-exist.pub` | exit_code == 11 (`key_file_missing`); never invokes ssh-copy-id (data.ssh_copy_id_argv == []) |

The "real" end-to-end check is **manual** — run `deploy_pubkey.py pai` once on a fresh setup, verify password prompt + success. Documented in this PRD's manual-test note, not in integration tests.

### Manual happy-path (for e2e-validate task; not automated)

```bash
# Setup: temporary user on pai with password 'changeme'
ssh root@pai 'useradd -m -p $(openssl passwd changeme) deploytestuser'

# Run from interactive terminal
python plugins/ssh-core/skills/ssh-core/scripts/deploy_pubkey.py \
    deploytestuser-on-pai --key ~/.ssh/id_ed25519.pub --json

# Expect: tty prompts for password, then:
# {"success": true, "exit_code": 0, ..., "data": {"outcome": "installed", "keys_added": 1, ...}}

# Verify
ssh -i ~/.ssh/id_ed25519 deploytestuser-on-pai 'echo ok'    # should succeed without prompt

# Idempotency
python deploy_pubkey.py deploytestuser-on-pai     # expect outcome=already_installed

# Cleanup
ssh root@pai 'userdel -rf deploytestuser'
```

## Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| OpenSSH ssh-copy-id stderr wording shifts between versions | _parse_success_outcome misses → outcome=null on success | Regex covers both "Number of key(s) added" and the older / WARNING / ERROR "All keys were skipped" wordings; fall back to `outcome=null, keys_added=null` rather than misreport |
| ssh-copy-id exits non-zero when key is already installed (some builds) | Misclassified as `remote_error` | Explicit idempotency override: rc != 0 + "All keys were skipped" stderr → reclassify as success |
| Password prompt eats inherited stdin and confuses pytest CI | `_run_ssh_copy_id` hangs in CI | Integration tests don't invoke ssh-copy-id at all (preflight refusals only); unit tests mock subprocess; CI never runs `-m live_ssh` |
| User passes a `.pub` that's actually a private key | ssh-copy-id installs the private key as authorized_keys (catastrophic) | Preflight rejects keys whose first line doesn't match `(ssh-rsa|ssh-ed25519|ecdsa-sha2-|ssh-dss) ` prefix → `failure_class="key_file_missing"` with hint "{path} doesn't look like an OpenSSH public key" |
| `--accept-new-hostkey` accidentally writes user's known_hosts | Trust-on-first-use is auto'd | We pass `-o StrictHostKeyChecking=accept-new` only when the flag is explicitly set; default is OpenSSH's default (`ask` in interactive, `yes` in batch); user opts in per call |
| Race: user re-runs deploy_pubkey while a previous invocation is still on the password prompt | Both prompt; second one wins password | Out of scope — no locking; both runs eventually converge to idempotent state |

## Compatibility / Rollback

- **Completely self-contained script.** Imports `lib.json_result, lib.emit, lib.msys_safe_env` only — no new lib helpers. No other v1.0 script imports deploy_pubkey.
- **Rollback path**: revert the file to its 42-LOC stub form; no other code or test changes propagate.
- **Forward path (v1.1)**: when password+daemon path returns (ADR-001 v1.1 work), add `--strip-password` back; the failure_class table and exit-code range stay stable.
- **v0.2 surface compat**: the v0.2 stub argparse accepted `--strip-password` (line 31). v1.0 removes it. **This is a breaking surface change.** Callers using the stub today get an "argparse: error: unrecognized arguments: --strip-password" — acceptable because the stub never had a working implementation behind it (it just called `unimplemented()`). The error message is itself the migration hint.
