# PoC: ssh_execute key-only path

## Type

**Exploratory spike.** Not a feature delivery. The goal is to **learn** before
committing to v1.0 architecture decisions. Output is knowledge (a findings
document + ADR draft), not production code.

## Why this PoC

`ssh-core` is the dependency root for 6 v0.2 diagnostic plugins. Before
investing in a clean `ssh_execute.py` implementation, we need empirical
answers to several technical questions that paper design cannot settle.
The riskiest unknown is `subprocess.run(["ssh", ...])` behavior in edge
cases — exit code semantics, stderr noise, timeout, `<host>=local`
routing. A wrong call here ripples into every downstream plugin.

## Hypotheses to validate

Each hypothesis is either confirmed, refuted, or refined by the spike.

| ID | Hypothesis | Confirmation method |
|---|---|---|
| H1 | `subprocess.run(["ssh", alias, command], capture_output=True)` cleanly returns remote stdout/stderr and the **remote command's** exit code (not ssh's) when authentication is key-based. | Run a series of remote commands with known exit codes (0, 1, 42, 127) and assert `returncode == expected`. |
| H2 | The SSH client's own stderr ("Permanently added '…' to known_hosts.", `Warning:` lines) pollutes the captured stderr in ways that need filtering before reaching the JSON envelope. | Run against a host with no known_hosts entry; capture and inspect stderr; classify lines. |
| H3 | Native ssh + `IdentityFile` from `~/.ssh/config` works without any Python-side config parsing — we just pass the alias. | Set up two aliases (one with default IdentityFile, one with explicit non-default), run, observe. |
| H4 | We can distinguish three failure modes from `subprocess.run` results: (a) network unreachable, (b) auth failure, (c) remote command returned non-zero. | Force each failure deliberately; record exit codes + stderr signatures. |
| H5 | `subprocess.run(..., timeout=N)` kills both the local ssh process and the remote command cleanly when N elapses (no orphan remote process). | Start a `sleep 60` remote, timeout at 5s, verify on remote with `pgrep sleep`. |
| H6 | `<host>=local` route — when the first argv is the literal string `local`, we should bypass ssh entirely and run the command as `subprocess.run(shlex.split(command))` locally. **Does shlex split match user expectations** for commands like `ls -la /tmp \| head`? | Compare `local "ls -la /tmp"` vs `local "ls -la /tmp | head"` results. |

## Questions to answer

Beyond yes/no hypotheses, these are open design questions the spike should inform:

- **Q1**: Is the JSON envelope `{success, exit_code, stdout, stderr, data}` (from `lib/json_result`) sufficient for this script, or do we need a 6th field (e.g. `remote_exit_code` separated from `local_exit_code` for ssh-wrapper-vs-remote-command distinction)?
- **Q2**: When `--json` is **not** passed and the remote command produces both stdout and stderr, what does the user see — raw passthrough? Prefixed? Should `lib/emit()` be enhanced?
- **Q3**: For H5 timeout: what should the JSON payload look like (`success=False, exit_code=?, stderr="timed out after Ns"`)? Should we add a new exit code or reuse one of 0/1/2?
- **Q4**: Should the PoC try paramiko fallback at all, or is establishing "native ssh + keys covers MVP" already enough to defer paramiko to v1.1?

## Out of scope

- `ssh_daemon.py` connection pooling — explicitly deferred to v1.1.
- `paramiko` / password auth — deferred unless H3 reveals key-auth coverage is unviable.
- Windows MSYS_NO_PATHCONV behavior — single-OS PoC; recorded as a follow-up gap.
- Cluster broadcast, tunnels, transfers — they all depend on ssh_execute being solid, so they wait.
- Integration with any downstream plugin (k8s-debug etc) — that's the *next* PoC if this one succeeds.

## Deliverables (artifact list)

All artifacts written **inside the worktree** under
`.trellis/tasks/05-21-02-poc-ssh-execute/`:

1. `findings.md` — one section per hypothesis (H1-H6), the experiment run,
   observed behavior, and a one-line verdict (CONFIRMED / REFUTED / REFINED).
2. `questions.md` — Q1-Q4 with the answer the spike points to.
3. `adr-001-ssh-execute.md` — draft Architecture Decision Record capturing
   the path forward for v1.0 `ssh_execute.py`. This is the artifact that
   may eventually be **promoted into `.trellis/spec/backend/` after main-session
   review**.
4. `spike-code/` — the throwaway Python that produced the evidence. Not
   meant to ship; only kept for reproducibility.

## Success criteria

The PoC is **complete** (not the same as "successful") when:

- [x] All 6 hypotheses have a CONFIRMED / REFUTED / REFINED verdict, with
      raw evidence (commands run, output captured) recorded in `findings.md`.
- [x] All 4 questions have a recommended answer in `questions.md`.
- [x] `adr-001-ssh-execute.md` exists and is internally consistent (every
      decision in it traces back to evidence in `findings.md`).
- [x] A "follow-up gaps" section in `findings.md` lists the things this PoC
      explicitly did NOT verify (Windows behavior, paramiko, etc.), so the
      v1.0 task PRD knows what to cover.

The PoC is **successful** if either:
- The decisions in `adr-001-ssh-execute.md` are stable enough that the v1.0
  `ssh_execute.py` task can use them as input without further investigation, OR
- The PoC reveals a foundational issue (e.g., "native ssh stderr noise is
  unfilterable in a portable way") that changes the v1.0 plan in a way
  documented papers wouldn't have.

## Time budget

**1 day cap.** If H1-H6 + Q1-Q4 + ADR draft are not done by end of day, stop
and report what we have — partial answers are still better than burned hours.

## Notes

- All experiments run on the developer's local machine. Sample SSH hosts:
  `localhost` over `ssh-keygen`-generated keys; a public test host if
  available; deliberately-broken aliases to trigger H4 failure modes.
- The spike branch is `spike/poc-ssh-execute`, isolated from `main`.
- This task may not produce any commit on `main`. The ADR promotion is a
  separate Trellis task after wrap-up.
