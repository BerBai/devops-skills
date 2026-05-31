# ship ssh-core ssh_daemon v1.0

## Verdict

**Deferred to v1.1 per ADR-001 A2.** This child task ships **zero new implementation code**. It produces:

1. PRD-only documentation of the defer decision (this file)
2. Updates to the stub docstring to mark it `Status: Deferred to v1.1`
3. Sync of SKILL.md / workflows.md sections that still describe the v0.2 daemon path, since those descriptions now conflict with shipped v1.0 ssh_execute (BatchMode-only, no daemon)

Phase 1 child count effectively becomes **6 ship + 1 deferred = 7** in parent v1-rollout PRD; the parent's Notes section gains a closing entry recording this.

## Why deferred — evidence

- **ADR-001 D1 forbids `ssh_daemon` from v1.0**: *"No paramiko, no custom socket protocol, no daemon. One subprocess per call. Connection reuse (ControlMaster or ssh_daemon.py) is a v1.1 optimization."* (`adr-001-ssh-execute.md` D1)
- **ADR-001 A2 (Alternatives considered)**: *"Custom daemon protocol from day 1 (`ssh_daemon.py` v1.0) — **Rejected**. PoC confirms subprocess works correctly. Daemon adds: protocol design, crash recovery, port conflicts, IPC complexity. v1.0 ships without; v1.1 evaluates OpenSSH ControlMaster (zero Python, free reliability) before considering a custom daemon."*
- **ADR-001 Negative consequences**: *"v1.1 introduces ControlMaster (likely simpler than building ssh_daemon.py from scratch)."*
- **Zero callers in shipped v1.0 code.** `grep ssh_daemon plugins/ssh-core/.../scripts/` shows no references outside the stub itself. ssh_execute / ssh_cluster / ssh_config_manager (shipped); deploy_pubkey / ssh_upload / ssh_download / ssh_server_transfer / ssh_tunnel (planned per their PRDs) — none invoke ssh_daemon. Shipping daemon now = dead code.
- **ssh_tunnel does not require it.** Although the stub says "Same daemon shape as ssh_daemon.py", that means the **process model** (detached + PID file + idle exit + signal handling), NOT the wire protocol. ssh_tunnel's subcommands need PID-file + signal, not length-prefixed JSON. ssh_tunnel can independently `fork + supervise ssh -N -L`. → ssh_tunnel child task is unblocked.
- **v1.1 path is uncertain.** ADR-001 says v1.1 "evaluates ControlMaster before considering a custom daemon" — shipping a custom paramiko daemon today risks being thrown away in v1.1.

## Trigger conditions for v1.1 revival

Revisit this task when **at least one** of these conditions holds:

1. `ssh_execute` v1.1 needs password / keyboard-interactive auth and a survey concludes ControlMaster is insufficient (e.g. for paramiko's interactive auth flows).
2. `ssh_cluster` v1.x bulk operations hit a measured performance ceiling (the ADR-001 "~200-300 ms/call handshake overhead" prediction surfaces as a real pain point) AND ControlMaster is benchmarked insufficient.
3. A new caller emerges that genuinely needs a Python-controlled long-lived connection (e.g. a streaming RPC pattern that ControlMaster's exec channels can't model).

Until then, the stub stays in place but clearly marked deferred.

## In scope (work product of this task)

1. **`scripts/ssh_daemon.py` docstring update.** Add a `Status: Deferred to v1.1 per ADR-001 A2.` line near the top. Keep the v1.0 design block intact (it documents what would be built when revived). Leave the argparse skeleton and `unimplemented` body untouched — `--help` still works and the manifest tests still pass.
2. **`SKILL.md` daemon-description sync.** Three locations to amend so v1.0 reality matches v1.0 documentation:
   - **Line 3 frontmatter description**: *"SSH transport primitives — persistent daemon connections, cluster broadcast, …"* → drop "persistent daemon connections" (no daemon ships in v1.0).
   - **Line 8 CRITICAL block**: *"Direct calls bypass the persistent daemon, lose connection pooling, leak credentials …"* → drop the daemon/pooling sentence (still leaks credentials / breaks MSYS; those remain accurate).
   - **Lines 52, 61, 100-109 (the entire `## Daemon model` section)**: Replace with a one-paragraph note: *"v1.0 ssh-core does not boot a daemon. Each `ssh_execute` call is a fresh `ssh` subprocess (ADR-001 D1). Connection pooling is planned for v1.1 (likely OpenSSH ControlMaster, see `ssh_daemon.py` Status block)."* Remove the latency figure ("~0.45 s → ~0.12 s") — that was a v0.2 PoC measurement under the daemon path, not v1.0.
3. **`references/workflows.md` §6 sync.** Replace the "key auth → native ssh / password → daemon-backed paramiko" table with the v1.0 reality (BatchMode-only). Add a "v1.1 will revisit" line at the end of §6.
4. **`references/safety.md` review (no change expected).** The daemon-state-file conventions (line 22) are still accurate as a **future** spec — leave the convention block; if any line says "the daemon does X" in present tense, flip to "will do X in v1.1".
5. **Parent `v1-rollout/prd.md` Notes append** — one line: *"2026-05-25 ssh_daemon deferred to v1.1 per ADR-001 A2. Phase 1: 6 ship + 1 deferred."*

## Out of scope

- Any code change inside `scripts/ssh_daemon.py` beyond the docstring `Status:` line.
- Any new `lib/` helpers.
- ADR mutation (`adr-001` already documents the deferral; no new ADR needed; `.trellis/spec/` is gitignored anyway per parent Notes §82).
- Updates to test files (no test exercises ssh_daemon today; manifest test `--help` still passes after docstring edit).
- ssh_tunnel design decisions (separate child task; this PRD merely records the fact that ssh_tunnel doesn't depend on this file).
- v1.1 implementation work — this task does NOT design the v1.1 daemon; it only marks v1.0 deferred and records trigger conditions.

## Acceptance Criteria

- [ ] `scripts/ssh_daemon.py` docstring has a `Status: Deferred to v1.1 per ADR-001 A2.` line and is otherwise unchanged (still imports `unimplemented`, still returns it, `--help` still parses).
- [ ] `SKILL.md` frontmatter (line 3), CRITICAL block (line 8), and `## Daemon model` section reflect v1.0 reality (no daemon).
- [ ] `references/workflows.md` §6 reflects v1.0 reality (BatchMode-only) with a "v1.1 will revisit" line.
- [ ] `references/safety.md` checked; any present-tense "the daemon does X" wording flipped to "will do in v1.1" (likely 0 changes required).
- [ ] Parent `v1-rollout/prd.md` Notes has a 2026-05-25 line recording the defer.
- [ ] `pytest tests/` default suite passes (manifest test still green; nothing else exercises ssh_daemon).
- [ ] `pytest tests/ -m live_ssh` passes locally (no integration tests touch ssh_daemon).
- [ ] `ruff check plugins/ssh-core` clean.
- [ ] One commit: `docs(ssh-core): defer ssh_daemon to v1.1 per ADR-001` (or similar — parent Notes §81 says commits should be focused).

## Notes

- This is the cleanest possible v1.0 outcome for ssh_daemon: zero code, zero test infrastructure, the project's actual surface matches its actual documentation.
- The decision is reversible: if a v1.0.x patch release decides to revive ssh_daemon, this PRD's "Trigger conditions" section becomes the entry criterion for a new child task; the deferred stub still embeds the original design block ready to use.
