# tail_log: emit remote_stderr to stderr on path-mode failure

## Goal

When `tail_log` (any mode, but most often path mode) fails because the remote
command itself errors out (e.g. `tail: cannot open '/var/log/syslog': No such
file or directory`), the script today exits 1 with **empty local stdout AND
empty local stderr**. The actual reason lives only in `--json`'s
`data.remote_stderr` (single-host) or `data.hosts[<alias>].remote_stderr`
(multi-host). Users hit `exit=1` blind and have to re-run with `--json` to
learn why.

This violates `error-handling.md` line 28: "Without `--json`, write errors and
diagnostics to `stderr`". The fix mirrors the recent `ssh_cluster` patch
(commit f4c072b, Finding #1): surface the first line of each failing host's
remote stderr on the local stderr stream so users get an immediate clue.

Reference: Finding #2 in `.trellis/tasks/05-24-e2e-validate-skills-on-pai/
validation-report.md`.

## Requirements

1. **Emit on failure only.** When a host's `_fetch_one_host` record has
   `success=False`, its `remote_stderr` first line (truncated) is written to
   the local `stderr` stream via the envelope `stderr` field. When all hosts
   succeed, the envelope `stderr` stays empty (no regression on happy path).

2. **First line, 200-char cap.** Same shape as the ssh_cluster patch: split
   `remote_stderr` on `\n`, take the first non-empty line, truncate to 200
   chars and append `...` if longer. Empty stderr falls back to a generic
   `failure_class=<x>` hint so users always get *something*.

3. **Multi-host: one line per failing host.** Format:
   `tail_log: <alias>: <first_line_truncated>` per failing host, one per
   line, newline-terminated. Successful hosts contribute nothing to stderr.

4. **Single-host: same format.** `tail_log: <alias>: <message>` — consistent
   with the multi-host case, no special-cased shorter form.

5. **--json payload unchanged.** `data.remote_stderr` / `data.hosts[...]
   .remote_stderr` keep their current shape. The envelope's top-level
   `stderr` field gets populated whether `--json` or human mode; in human
   mode `_emit` already writes it to `sys.stderr`, in `--json` mode it
   appears inside the JSON object (so JSON consumers see it too).

6. **No change to exit code logic.** EXIT_FAIL / EXIT_TIMEOUT thresholds
   stay identical.

## Acceptance Criteria

- [ ] Single-host path-mode failure (mocked `_fetch_one_host` returning
      `success=False, remote_stderr="tail: cannot open ..."`) yields local
      stderr containing `tail_log: pai: tail: cannot open ...` (non-JSON
      mode, asserted via `capsys`).
- [ ] Same scenario in `--json` mode populates the envelope's top-level
      `stderr` field with the same string (asserted via JSON parse).
- [ ] Multi-host with 1 ok + 1 failing host produces exactly one stderr
      line for the failing host, none for the ok one.
- [ ] Multi-line remote_stderr keeps only the first line (subsequent lines
      do NOT appear in envelope stderr).
- [ ] Remote_stderr longer than 200 chars is truncated with `...` suffix.
- [ ] Happy-path single-host and multi-host runs leave envelope `stderr`
      empty (regression guard).
- [ ] Empty `remote_stderr` on failure still produces a fallback line
      mentioning the alias and `failure_class` (no silent failure).
- [ ] All existing `tests/test_tail_log_unit.py` tests still pass.
- [ ] `pytest tests/ -m 'not live_ssh'` baseline green.
- [ ] Manual sanity check: `python3 .../tail_log.py pai /var/log/syslog
      --lines 5` on `pai` (Debian 13, no syslog file) prints the
      tail-error line to stderr without `--json`.

## Notes

- Mirrors the ssh_cluster fix shape (commit f4c072b) — same cap (200),
  same first-line-only rule, same "JSON payload unchanged" guarantee.
- No `design.md` / `implement.md` — single-file change in `tail_log.py`,
  ~15 LOC + ~6 unit tests, scope identical to the ssh_cluster precedent.
