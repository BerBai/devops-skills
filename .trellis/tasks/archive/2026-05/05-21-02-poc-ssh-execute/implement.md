# Implementation Plan: ssh_execute PoC

## Pre-flight

- [ ] Confirm `ssh`, `ssh-keygen`, `pgrep` are on PATH (macOS default — should be).
- [ ] EnterWorktree `poc-ssh-execute` to isolate from main. Branch:
      `spike/poc-ssh-execute`. All commands below run inside the worktree.

## Phase A — Environment setup (30 min)

- [ ] A1: Generate dedicated keypair: `ssh-keygen -t ed25519 -f /tmp/poc_ed25519 -N ""`
- [ ] A2: Append public key to `~/.ssh/authorized_keys` (preserve existing entries):
      `cat /tmp/poc_ed25519.pub >> ~/.ssh/authorized_keys`
- [ ] A3: Confirm SSH server is running on localhost (`launchctl list | grep ssh`
      on macOS; if not, enable via System Settings → Sharing → Remote Login,
      or run `sudo systemsetup -setremotelogin on`).
- [ ] A4: Create scratch SSH config at `/tmp/poc-ssh-config`:

      ```
      Host poc-default-key
          HostName 127.0.0.1
          User <whoami>
          # No IdentityFile - tests default behavior

      Host poc-explicit-key
          HostName 127.0.0.1
          User <whoami>
          IdentityFile /tmp/poc_ed25519
          IdentitiesOnly yes

      Host localhost-noauth
          HostName 127.0.0.1
          User <whoami>
          IdentityFile /dev/null
          IdentitiesOnly yes
      ```

- [ ] A5: Sanity-check basic connectivity: `ssh -F /tmp/poc-ssh-config poc-explicit-key true && echo OK`

## Phase B — Spike code skeleton (45 min)

- [ ] B1: Create `spike-code/ssh_execute_spike.py` — minimal implementation:
        - argparse: `host`, `command`, `--timeout`, `--ssh-config`, `--json`
        - if host == "local": shlex.split + subprocess.run
        - else: subprocess.run(["ssh", "-F", config, host, command], capture_output=True, timeout=...)
        - emit envelope {success, exit_code, stdout, stderr, data: {classification}}
- [ ] B2: Create `spike-code/results/` directory for raw evidence files
- [ ] B3: Create `spike-code/run-all.sh` — driver that runs every test case
        and tees output to `results/H<n>-<case>.txt`

## Phase C — Experimental runs (3 hours)

Run hypotheses in order; record evidence; do NOT analyze until Phase D.

- [ ] C1: H1 (exit code propagation) — 5 cases
- [ ] C2: H2 (stderr noise classification) — run + collect raw stderr
- [ ] C3: H3 (config alias resolution) — 3 cases
- [ ] C4: H4 (failure mode discrimination) — 3 cases
- [ ] C5: H5 (timeout cleanup) — 1 case + post-run `pgrep` verification
- [ ] C6: H6 (local route + shlex) — 4 cases

Each step: run the command, capture stdout+stderr+returncode to
`results/H<n>-<case>.txt`, and a one-line summary to `results/INDEX.md`.

## Phase D — Synthesis (1.5 hours)

- [ ] D1: Write `findings.md` — one section per hypothesis:
        - Setup recap
        - Each case: command, result file pointer, observed behavior
        - Verdict: CONFIRMED / REFUTED / REFINED
        - If REFINED: the actual model
- [ ] D2: Write `questions.md` — Q1-Q4 with recommendations grounded in findings
- [ ] D3: Add "follow-up gaps" section to `findings.md` covering what was
        NOT tested (Windows, paramiko, daemon, large stdout, slow networks,
        IPv6 hosts, kerberos, etc.)

## Phase E — ADR draft (1 hour)

- [ ] E1: Write `adr-001-ssh-execute.md` using a standard ADR template:
        - **Status**: Proposed (pending main-session review)
        - **Context**: why we need ssh_execute, what we already know from CONTRIBUTING.md and v0.2 stub docstrings
        - **Decision**: the path forward for v1.0 ssh_execute (concrete:
          subprocess vs paramiko, stderr filtering, timeout semantics,
          exit code conventions, local route behavior)
        - **Consequences**: what this enables, what it limits, what
          downstream plugins should rely on
        - **Alternatives considered**: paramiko-first, persistent daemon
          from day 1, pure-Python ssh impl — and why we rejected them

## Phase F — Wrap-up (15 min)

- [ ] F1: Update task `prd.md` checklist (mark all Success Criteria boxes).
- [ ] F2: Self-review: does every decision in `adr-001-ssh-execute.md`
        cite evidence from `findings.md`? If not, mark the unsupported
        decisions as "pending v1.0 implementation feedback".
- [ ] F3: Run `python3 spike-code/ssh_execute_spike.py --help` to confirm
        the spike code at least loads. (Spike code is throwaway, but it
        should run.)
- [ ] F4: Hand back to main session. The wrap-up message reports:
        - How many hypotheses CONFIRMED vs REFUTED vs REFINED
        - Whether the ADR is stable enough to drive a v1.0 implementation task
        - Time spent vs 1-day budget
        - Recommended next Trellis task (likely: "Implement ssh_execute v1.0
          per ADR-001" or "Re-spike X" if a hypothesis blew up)

## Cleanup (post-wrap-up)

- [ ] Revoke the PoC public key from `~/.ssh/authorized_keys`
- [ ] Delete `/tmp/poc_ed25519*` and `/tmp/poc-ssh-config`
- [ ] Decide (with main session) whether to keep the spike branch for
      reference (likely yes, until ADR is committed to main).

## Validation commands (run after each phase)

```bash
# After Phase C
ls spike-code/results/ | wc -l   # Should be 6 (H1-H6 minimum)

# After Phase D
test -s findings.md && test -s questions.md

# After Phase E
grep -E "^## Decision|^## Consequences|^## Alternatives" adr-001-ssh-execute.md | wc -l   # Should be 3+

# After Phase F
grep -c "\[x\]" prd.md  # Success criteria checked
```

## Rollback / abort points

If Phase C reveals an aborting condition (per design.md "Failure / abort
criteria"), skip Phase D-E. Instead:

- Write `findings.md` with the negative result only
- Skip `adr-001-ssh-execute.md`
- Write `re-plan.md` proposing what the next spike should investigate
- Hand back to main session with abort report
