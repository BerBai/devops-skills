# Logging Guidelines

> Skill output IS the logging channel. The stdlib `logging` module is not used.

---

## Overview

This repo deliberately does not use Python's `logging` module. There is no
log file, no log level configuration, no `logger.info(...)`. Skill scripts
are short-lived CLIs that Claude Code shells out to and parses; their
stdout/stderr is the only output channel that matters.

Document the rules below as current reality, not aspiration. If we ever
introduce structured logging, this file gets a new section — until then,
do not import `logging`.

---

## Output channels

| Stream | What goes here |
|--------|---------------|
| stdout | Normal results. JSON if `--json` was passed, otherwise human-readable text. |
| stderr | Errors, diagnostics, warnings, the `UNIMPLEMENTED_NOTE` payload, anything not part of the script's intended result. |

`plugins/k8s-debug/skills/k8s-debug/scripts/diagnose_pod.py` lines 54-58
shows the split: with `--json`, the whole payload is written to stdout;
without `--json`, only `stderr` text reaches the user (because there is no
implemented result yet).

For long-running output (e.g., `tail_log.py`, `tail_multi.py`), stream to
stdout line by line. There is no rotating file sink — the consumer (a
terminal or Claude Code) handles capture.

---

## No emoji, anywhere

CONTRIBUTING.md line 38:

> No emoji in skill output unless the user explicitly asks. Skill markdown
> should also stay emoji-free.

This applies to:
- Script stdout / stderr.
- `SKILL.md` body.
- `references/*.md`.
- Any string baked into a Python source file in `scripts/`.

The rule is repo-wide. The only exception is when the user explicitly
asks Claude Code for emoji in the response; that's a runtime choice, not
a code change.

---

## Never log secrets

`plugins/ssh-core/skills/ssh-core/SKILL.md` line 54 is the rule:

> Never show keys, passwords, or `IdentityFile` paths.

Forbidden from all output streams:
- Private key contents (PEM body, OpenSSH body).
- `IdentityFile` paths (they leak filesystem layout and which keys exist).
- Passwords, passphrases, `sshpass` arguments.
- `Authorization:` / `X-API-Key:` style headers.
- `.env` values.

`ssh-guarded` ships a **redact module** that is default-on for all string
output paths (CONTRIBUTING.md line 56). When `ssh-guarded` is in the call
chain, every stdout/stderr string is filtered before it reaches the
caller. Do not bypass it.

---

## What to put in stderr (text mode)

When `--json` is NOT in effect, treat stderr as the diagnostic channel:
- One-line error summaries.
- Hints pointing the user at a `references/<topic>.md` section.
- Scaffold notices (the `UNIMPLEMENTED_NOTE` pattern from `diagnose_pod.py`).

Keep it short and machine-greppable when reasonable, but the audience is
the human running `python scripts/foo.py`. Long context goes in
`data.hint` of the JSON payload, not in a stderr wall-of-text.

---

## Aggregator scripts: surface child failure stderr in human output

A script that aggregates multiple sub-results (cluster broadcast, multi-host
compare, multi-target audit) MUST NOT swallow a failed child's `stderr` in
its default human output. If the user has to re-run with `--json` to learn
*why* a child failed, the script has failed the "errors should be loud"
contract.

Convention (see `ssh_cluster.py:_format_human`):
- Per-row line stays one line with `[FAIL]`, exit code, elapsed, failure_class.
- On failure with non-blank `stderr`, append an indented continuation line
  with the first line of stderr, truncated to ~200 chars + `"..."`.
- `--json` payload structure is **unchanged** — the `stderr` field is the
  source of truth; the human continuation is rendering on top of it.

Applies to: `ssh_cluster`, `tail_log` (path mode), `compare_across_hosts`,
`image_audit`, and any future fleet/multi-target aggregator.

---

## What NOT to do

- Do NOT `import logging` and add a logger to a script.
- Do NOT add ANSI color codes — Claude Code's terminal capture and `--json`
  consumers both choke on them.
- Do NOT `print(password)` even "for one debug run". Use `pdb` locally,
  remove before commit.
- Do NOT add emoji to error messages thinking it improves readability.

---

## Honest note

If we later need structured logging (e.g., for long-running daemons that
outlive a single CLI call, like `ssh_daemon.py`), add a section here at
that time. Today, nothing in the repo uses `import logging`, and that's
intentional.
