# PoC Findings — ssh_execute key-only path

**Spike machine**: macOS 25.1 (Darwin), `ssh` from `/usr/bin/ssh` (OpenSSH).
**Target host**: `pai` alias → Debian 13 (Linux 6.19) at 192.168.2.115, user `root`.
**Auth**: ED25519 key in macOS ssh-agent (`SHA256:h9nIA6gSDnJdmSYQVlkM2pyNmCpee7cvX/4scSe3t3E`).
**Spike code**: `spike-code/ssh_execute_spike.py` (~150 lines). Driver: `spike-code/run-all.sh`. Raw evidence: `spike-code/results/*.txt`.

---

## H1 — Remote exit code propagation

**Hypothesis**: `subprocess.run(["ssh", alias, command], capture_output=True)` cleanly returns the **remote command's** exit code (not ssh's own).

**Method**: 5 cases with known remote exit codes.

| Case | Remote command | Expected exit | Observed exit | File |
|---|---|---|---|---|
| H1-1 | `true` | 0 | 0 | `H1-1_true.txt` |
| H1-2 | `false` | 1 | 1 | `H1-2_false.txt` |
| H1-3 | `bash -c 'exit 42'` | 42 | 42 | `H1-3_exit42.txt` |
| H1-4 | `nonexistent_command_xyz` | 127 | 127 | `H1-4_nocmd.txt` |
| H1-5 | `bash -c 'echo TO_OUT; echo TO_ERR >&2; exit 3'` | 3 + stdout/stderr split | 3 + stdout=`TO_OUT\n` stderr=`TO_ERR` (newline lost by filter, see notes) | `H1-5_mixedio.txt` |

**Verdict: CONFIRMED.** ssh transparently propagates remote exit codes including the conventional `127` for "command not found". stdout and stderr arrive separated. `subprocess.run(..., capture_output=True, text=True)` is the right primitive.

**Note**: my `filter_ssh_noise()` uses `splitlines()` + `"\n".join()` which silently strips a single trailing newline. Bug in the spike, not in the hypothesis. v1.0 should preserve trailing newline (or document the trim).

---

## H2 — SSH client stderr noise classification

**Hypothesis**: known_hosts warnings pollute captured stderr in ways that need filtering before the JSON envelope.

**Method**:
- H2-1: spike against `pai` with existing known_hosts entry → expect no warning.
- H2-2: raw `ssh` with `UserKnownHostsFile=$TMP` to force fresh-host scenario.
- H2-3 (added mid-run): spike against `pai` with a scratch config pinning `UserKnownHostsFile` to a temp file + `StrictHostKeyChecking=accept-new` → exercise the filter.

| Case | Observed stderr | `data.ssh_noise_lines` | File |
|---|---|---|---|
| H2-1 | empty | `[]` | `H2-1_freshkh.txt` |
| H2-2 (raw ssh) | `Warning: Permanently added '192.168.2.115' (ED25519) to the list of known hosts.` | n/a (raw ssh) | `H2-2_force_warn.txt` |
| H2-3 (spike) | empty | `["Warning: Permanently added ..."]` | `H2-3_spike_filter.txt` |

**Filter regex used**: `^Warning: Permanently added .* to the list of known hosts\.$`

**Verdict: CONFIRMED.** Single-line regex cleanly separates the well-known known_hosts warning. The `data.ssh_noise_lines` field (with `data.raw_stderr` preserved for audit) handles this without polluting top-level `stderr`.

**Refinement**: more SSH warnings exist (host key changed, host key not found in NEW format, etc.) — only the most common one is filtered today. v1.0 needs a small library of patterns plus a "pass through unknown warnings to top-level stderr by default" rule (fail-open, not fail-closed: never silently drop real errors).

---

## H3 — Config alias resolution

**Hypothesis**: native ssh + `IdentityFile` from `~/.ssh/config` (or via ssh-agent) works without any Python-side config parsing.

| Case | Alias | Auth path | Observed | File |
|---|---|---|---|---|
| H3-1 | `poc-explicit-key` | explicit `IdentityFile ~/.ssh/id_ed25519` (key file doesn't exist on disk; satisfied by agent) | success exit 0 | `H3-1_explicit.txt` |
| H3-2 | `poc-via-agent` | no `IdentityFile`, `IdentitiesOnly no` → agent | success exit 0 | `H3-2_agent.txt` |
| H3-3 | `bogus-not-defined` | alias absent from scratch config | fail exit 255, fail_class=network ("Could not resolve hostname") | `H3-3_bogus.txt` |

**Verdict: CONFIRMED.** Passing the alias to `ssh` and letting it consult `~/.ssh/config` (or whatever `-F` points at) is sufficient. **No Python-side config parsing needed.** When the alias isn't defined, ssh treats it as a literal hostname and the failure surfaces cleanly as a `network` class (DNS resolution failure).

---

## H4 — Failure mode discrimination

**Hypothesis**: 3 failure modes can be distinguished from (`returncode`, `stderr`).

| Case | Setup | Observed exit | Observed `failure_class` | File |
|---|---|---|---|---|
| H4-1 | DNS-unresolvable `nonexistent.invalid.local` | 255 | `network` ("Could not resolve hostname") | `H4-1_network.txt` |
| H4-2 | `poc-noauth` (IdentityFile=/dev/null) | 255 | `auth` ("Permission denied") | `H4-2_auth.txt` |
| H4-3 | `pai` + `false` | 1 | `remote_error` (residual class) | `H4-3_remote.txt` |

**Classifier function** (see `ssh_execute_spike.py:FAILURE_CLASSIFIERS`):

```python
FAILURE_CLASSIFIERS = [
    ("network", re.compile(r"Could not resolve hostname|Network is unreachable|No route to host|Connection refused|Connection timed out")),
    ("auth",    re.compile(r"Permission denied|publickey|password authentication failed|Authentication failed")),
    # remote_error is the residual: ssh returned non-zero but stderr doesn't match the above
]
```

**Verdict: CONFIRMED.** 3 classes are distinguishable with high confidence using `(returncode, stderr substring)`. Plus a 4th class (`timeout`) added during H5 — see below.

**Caveat**: `auth` regex matches `Permission denied` which can also appear in `remote_error` payloads (e.g. `rm /root/x` returning "Permission denied" from sudo). Mitigation: the auth check must be **scoped to lines starting with `ssh:` prefix** or to `returncode == 255`. Current spike uses returncode-agnostic substring; v1.0 should tighten this.

---

## H5 — Timeout cleanup

**Hypothesis**: `subprocess.run(..., timeout=N)` kills both local ssh and remote command cleanly.

**Method**: `ssh pai 'sleep 60 # <marker>'` with `--timeout 3`. After `TimeoutExpired`, re-`ssh` to the host and `pgrep -af sleep | grep -F <marker>`.

**Observed**:
- Spike: `subprocess.TimeoutExpired` raised at 3.0s. Envelope returned with `exit_code=124`, `data.failure_class="timeout"`.
- Orphan probe: `pgrep` finds only the probe command itself (which contains the marker in its own cmdline). **No standalone `sleep 60 # <marker>` process**. File: `H5-1_timeout.txt`.

**Verdict: CONFIRMED.** Timeout cleanly tears down both ends. Likely mechanism: SIGTERM/SIGKILL to local ssh closes the SSH channel; sshd kills the remote child process. No `ServerAliveInterval` tuning needed for this case.

**Refinement**: tested with a 3-second sleep against a `sleep 60`. Did not test:
- Long-running CPU-bound remote process (might require `ServerAliveInterval`).
- Detached remote process (`nohup sleep 60 &`) — would orphan because parent dies but child detaches. Out of scope; ssh_execute is not meant for detach. `exec_detached.py` handles that.

---

## H6 — `local` route + shlex behavior

**Hypothesis**: `local` route via `shlex.split` + `subprocess.run` matches user expectation for argv-style commands; fails predictably for shell metacharacters.

| Case | Command | Predicted | Observed | File |
|---|---|---|---|---|
| H6-1 | `ls /tmp` | success | success | `H6-1_simple.txt` |
| H6-2 | `ls -la /tmp` | success | success | `H6-2_args.txt` |
| H6-3 | `ls /tmp \| head -3` | fail (pipe is literal arg) | exit 1, `ls: cannot access '|'` | `H6-3_pipe.txt` |
| H6-4 | `echo "hello world"` | success, "hello world" preserved | success, stdout=`hello world\n` | `H6-4_quoted.txt` |
| H6-5 | `echo hi > /tmp/poc_spike_redir.tmp` | predicted FAIL | **exit 0**, stdout=`hi > /tmp/poc_spike_redir.tmp\n`, file NOT created | `H6-5_redir.txt` |

**Verdict: REFINED.** Predictions for H6-1/2/3/4 held. H6-5 is the troublesome case: **shlex makes redirection silently misbehave** — the command "succeeds" (exit 0) but doesn't do what the user asked. This is the worst failure mode (silent semantic error, not loud crash).

**Decision for v1.0** (deferred to ADR-001): `local` route must either
- (a) document loud-and-clear "argv-only, no shell metacharacters, use `bash -c '...'` if you need shell features" and reject lines containing `|`, `>`, `<`, `;`, `&&`, `||`, `` ` ``, `$(` with a clear error, OR
- (b) accept `shell=True` for the `local` route only (the safety story already excludes `local` from cross-host attack surface).

Option (a) is the safer default; (b) is more ergonomic. Both are documented in ADR-001.

---

## Verdict summary

| H | Verdict | Implication |
|---|---|---|
| H1 | CONFIRMED | Use `subprocess.run + capture_output`. Top-level `exit_code` = remote exit. |
| H2 | CONFIRMED | Filter known_hosts warnings; preserve in `data.ssh_noise_lines`. |
| H3 | CONFIRMED | Trust ssh + `~/.ssh/config` + agent. No Python-side config parsing. |
| H4 | CONFIRMED | 4 failure classes (network / auth / remote_error / timeout) distinguishable. |
| H5 | CONFIRMED | Timeout cleans up both ends. Use exit_code=124 for timeout. |
| H6 | REFINED | `local` route is risky with shell metacharacters. Need explicit policy (see ADR). |

**Status**: PoC **complete**, and **successful** — all 6 hypotheses produced a clear verdict, and the findings give the v1.0 task enough to proceed without further investigation.

---

## Follow-up gaps (explicitly NOT tested in this PoC)

The v1.0 implementation task or follow-up spikes must address:

1. **Windows behavior**: `MSYS_NO_PATHCONV=1` was not exercised. ssh argv quoting differs on Windows MSYS bash. Risk: argument splitting differences.
2. **`ssh_daemon.py` / ControlMaster**: spike used a fresh SSH connection per call (200-300ms overhead). Production may need connection reuse. The PoC validates we don't *need* a daemon for correctness, but performance characterization is open.
3. **paramiko / password auth**: deferred. Spike confirms key-auth covers the common case; password fallback is a v1.1 spike of its own.
4. **Large stdout / stderr**: did not test multi-MB outputs. Spec mentions a 10 MB cap on stream capture — not enforced or tested here.
5. **IPv6 hosts**: not tested.
6. **ProxyJump chains**: not tested. Should "just work" since we delegate to ssh, but unverified.
7. **High-latency / lossy networks**: `ConnectTimeout=8` may be too aggressive. Needs field testing.
8. **Auth class false-positive**: `Permission denied` substring may match `remote_error` too. Tighten classifier (see H4 caveat).
9. **More known_hosts warning variants**: only the most common pattern is filtered. Need a corpus.
10. **Concurrent invocations**: thread safety of subprocess invocation under high concurrency. Spike was sequential.
