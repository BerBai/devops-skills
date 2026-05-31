# Open Questions — Answered by PoC

## Q1: JSON envelope adequacy

> Is `{success, exit_code, stdout, stderr, data}` enough, or do we need a 6th top-level field?

**Answer: No new top-level field needed.** The `data` dict absorbs everything diagnostic:

- `data.failure_class` ∈ `{None, "network", "auth", "remote_error", "timeout"}` — for callers that want to branch on failure mode.
- `data.ssh_noise_lines` — filtered known_hosts warnings, available for audit but not in top-level stderr.
- `data.raw_stderr` — verbatim stderr including SSH client output, for the rare case the filter elides something important.
- `data.elapsed_s` — wall-clock duration; useful for latency tracking.
- `data.route` — `"local"` or `"remote"`, lets the caller verify routing.
- `data.ssh_argv` — exact argv passed to subprocess, for reproduction.

**Principle for v1.0**: top-level `exit_code` is always the **user's command** exit code — even when remote. ssh-client-level distinctions live in `data`. This keeps top-level chain-friendly (`if result.exit_code == 0: ...`) while preserving forensic detail.

---

## Q2: Non-JSON output presentation

> What does the user see when `--json` is NOT passed and the remote command produces both stdout and stderr?

**Answer: Raw passthrough.** stdout → stdout, stderr → stderr, no prefix, no labelling. This matches the existing `lib/emit()` implementation (`plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py:42-55`) and matches what users expect from `ssh host cmd` (i.e., as if they ran it directly).

H1-5 evidence: spike returned stdout=`TO_OUT\n`, stderr=`TO_ERR`. A user running `python3 ssh_execute_spike.py pai 'echo TO_OUT; echo TO_ERR >&2; exit 3'` (no `--json`) sees `TO_OUT` on stdout and `TO_ERR` on stderr — exactly as if they had typed `ssh pai 'echo TO_OUT; echo TO_ERR >&2; exit 3'`.

**Implication**: no change to `lib/emit()`. The function already does the right thing; v1.0 `ssh_execute.py` just calls it.

---

## Q3: Timeout exit code

> What should the JSON payload look like when timeout fires? Reuse 0/1/2 or add a new code?

**Answer: Use `exit_code=124`.** Matches GNU `timeout(1)` convention (which uses 124 to indicate "the command itself was killed by timeout"). Tools and shell users recognize it. `data.failure_class="timeout"` further disambiguates.

**Implications for spec**: `.trellis/spec/backend/error-handling.md:35-48` currently lists only 0/1/2 as allowed exit codes and explicitly says "Do not invent new exit codes". The v1.0 ssh_execute task must update this spec to either:

- (a) **Extend the allowed set to {0, 1, 2, 124}** with 124 reserved for "Python-side timeout enforcement triggered before the underlying command could complete", OR
- (b) **Reuse exit_code=1** for timeout (with `data.failure_class="timeout"` carrying the actual signal) — preserves the "don't invent new codes" rule but loses shell-level grep-ability.

Recommendation: (a). Cost is one line of spec update; benefit is compatibility with `timeout(1)` muscle memory across the team.

---

## Q4: Should paramiko be in the PoC?

> Or is "native ssh + keys" already enough to defer paramiko to v1.1?

**Answer: Defer paramiko to v1.1 (or later).**

Rationale:
- 100% of PoC tests passed using native ssh + ed25519 key in ssh-agent.
- `~/.ssh/config` + agent is the common setup for users likely to run DevOps tooling.
- paramiko adds: package dependency, version pinning, security audit surface (passphrase handling, key loading), `IdentitiesOnly` and `UseKeychain` re-implementation in Python.
- Users with password-only access can: (a) install `sshpass` and wrap the call themselves, or (b) configure key auth (which they should be doing anyway for unattended automation).
- Critically: a v1.0 without paramiko is **shippable**. v1.0 with paramiko adds 1-2 weeks to scope for a feature that maybe 5% of users need.

**Trigger to revisit**: if a real-world user blocks adoption because of password-only target environments. Until then, v1.1+.
