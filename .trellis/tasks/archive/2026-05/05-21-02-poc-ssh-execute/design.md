# Experiment Design: ssh_execute PoC

## Test environment

Single developer machine (macOS, the spike machine). All SSH targets:

| Alias | Purpose | Setup |
|---|---|---|
| `localhost` | Baseline | `ssh-keygen -t ed25519 -f ~/.ssh/poc_ed25519 -N ""`; append public key to `~/.ssh/authorized_keys`; add `Host localhost` block with `IdentityFile ~/.ssh/poc_ed25519` |
| `poc-default-key` | H3 default key | Alias resolving to `127.0.0.1` with **no** explicit `IdentityFile` (rely on default `~/.ssh/id_*`) |
| `poc-explicit-key` | H3 explicit non-default key | Alias resolving to `127.0.0.1` with explicit `IdentityFile ~/.ssh/poc_ed25519` |
| `nonexistent.invalid` | H4(a) unreachable | DNS-unresolvable host |
| `localhost-noauth` | H4(b) auth failure | Alias to `127.0.0.1` with `IdentityFile=/dev/null` |

SSH config additions go into a **scratch config** `~/.ssh/config.d/poc.conf`
(or temp file passed via `-F`), NOT the user's main `~/.ssh/config`, to avoid
polluting their setup. If `Include` isn't supported, we use `ssh -F /tmp/poc-ssh-config`.

## Experimental procedure

For each hypothesis, the format is:

1. **Setup**: prepare environment / config
2. **Action**: exact command run (recorded verbatim)
3. **Expected**: what we predict
4. **Observed**: raw result (stdout, stderr, returncode)
5. **Verdict**: CONFIRMED / REFUTED / REFINED
6. **Follow-up**: if REFINED, what's the actual model

## Test matrix

### H1 — Remote exit code propagation

| Case | Remote command | Expected returncode |
|---|---|---|
| 1.1 | `true` | 0 |
| 1.2 | `false` | 1 |
| 1.3 | `bash -c 'exit 42'` | 42 |
| 1.4 | `nonexistent_command` | 127 |
| 1.5 | `bash -c 'echo to-out; echo to-err >&2; exit 3'` | 3, with stdout=`to-out\n`, stderr=`to-err\n` (modulo SSH noise) |

### H2 — SSH stderr noise classification

Run against a clean host where known_hosts has no entry. Capture stderr.
Classify each line as:

- **ignore-always**: `Warning: Permanently added '…' to the list of known hosts.`
- **ignore-if-no-stricthostchecking**: host key change warnings
- **pass-through**: actual error from remote command

Output: regex or substring filters that we can wrap around `result.stderr`.

### H3 — Config alias resolution

| Case | Alias | Expect |
|---|---|---|
| 3.1 | `poc-default-key` | Should fail or succeed depending on whether `~/.ssh/id_*` is loaded; document which |
| 3.2 | `poc-explicit-key` | Should succeed using the explicit key |
| 3.3 | `bogus-alias` (not in config) | Should fail with a recognizable error message; capture exit code and stderr |

### H4 — Failure mode discrimination

| Case | Setup | Expected signature |
|---|---|---|
| 4.1 network unreachable | `ssh nonexistent.invalid uptime` | exit ≠ 0, stderr contains `Could not resolve hostname` |
| 4.2 auth failure | `ssh localhost-noauth uptime` | exit 255, stderr contains `Permission denied` |
| 4.3 remote nonzero | `ssh localhost false` | exit 1, stderr empty (or just known_hosts noise) |

The verdict for H4 is: can we write a Python function `classify(returncode, stderr) -> {"network", "auth", "remote_error"}` that's correct for all 3 cases?

### H5 — Timeout cleanup

1. SSH a `sleep 60` on `localhost`.
2. Call `subprocess.run(..., timeout=5)`.
3. After `TimeoutExpired`, on the localhost side, `pgrep -fl sleep` to check
   whether the remote `sleep` is still running.

Verdict CONFIRMED if no orphan process; REFINED if there's an orphan but a
workaround exists (e.g., `ssh -o ServerAliveInterval=…`).

### H6 — `local` route + shlex behavior

| Case | Command | Method | Expected |
|---|---|---|---|
| 6.1 simple | `local "ls /tmp"` | `subprocess.run(shlex.split("ls /tmp"))` | success, lists `/tmp` |
| 6.2 args | `local "ls -la /tmp"` | same | success |
| 6.3 pipe | `local "ls /tmp \| head -3"` | shlex.split keeps `|` as literal token, `ls` gets `|` as arg → FAIL | confirm or refute the prediction |
| 6.4 quotes | `local 'echo "hello world"'` | shlex preserves the quoted span | should succeed with `hello world` |

Verdict: which patterns work, which fail; recommend whether `local`
should support shell metacharacters (and if so, the `subprocess.run(cmd,
shell=True)` cost) or whether the docs should restrict `local` to argv-only.

## Measurement

- **Raw evidence**: stdout/stderr/returncode captured to text files in
  `spike-code/results/H<n>-<case>.txt`. Not interpreted, just preserved.
- **Findings synthesis**: `findings.md` summarizes evidence per hypothesis,
  citing the result files.

## Failure / abort criteria

Abort the spike (and re-plan) if:

- H1 returns inconsistent exit codes (e.g., ssh swallows remote's `exit 42`
  and returns its own code) — this would invalidate the entire model of
  `ssh_execute` as a transparent remote runner.
- H5 leaves orphan processes that cannot be cleaned up by any flag — would
  force a major rethink (per-call connection?).

Both abort cases should produce a **negative-result findings.md** documenting
what was tried, what failed, and what the v1.0 plan needs to address instead.

## Reproducibility

All commands recorded verbatim. Anyone with the same macOS + `ssh` should
be able to re-run from `spike-code/run-all.sh` and reproduce results
(modulo specific paramiko versions, which we're not testing here).
