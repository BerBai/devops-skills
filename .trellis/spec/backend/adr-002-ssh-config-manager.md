# ADR-002: ssh_config_manager v1.0 implementation

**Status**: Accepted (2026-05-24)
**Date**: 2026-05-24
**Source evidence**: `.trellis/tasks/05-24-ssh-core-ssh-config-manager/{prd,design,implement}.md`, sub-agent dispatch report including the SUPPRESS sharp-edge incident, `pytest` results (47 unit + 3 live_ssh = 50 PASS).

---

## Context

`ssh_config_manager.py` is the CRUD layer over `~/.ssh/config` for the `ssh-core` plugin. Before this ADR the script was a 73-line stub returning `unimplemented()`. Three reference documents already pinned the contract:

- `SKILL.md` workflow step 1 names `ssh_config_manager.py list` as the discovery primitive every other ssh-core script implicitly depends on (`SKILL.md:51`).
- `references/workflows.md` §5 fixed the 4-step write sequence (read AST → mutate → backup → render) and stated reads are O(file size) with no write.
- `references/safety.md` soft conventions fixed the `.bak.<unix-ts>` naming and the no-password-write rule.
- `references/troubleshooting.md` line 84 stated the script must emit `IdentitiesOnly yes` on new hosts with `IdentityFile`.

This ADR formalizes how those pre-fixed contracts are implemented and the new decisions that came up during planning and execution.

---

## Decision

### D1. AST data model

Two dataclasses model the parsed config:

```python
@dataclass
class HostBlock:
    alias: str
    host: str | None             # HostName
    user: str | None
    port: int | None
    identity_file: str | None    # IdentityFile
    proxy_jump: str | None
    description: str | None      # private metadata
    environment: str | None
    tags: list[str]
    location: str | None
    extra_directives: list[tuple[str, str]]  # everything else, preserved
    preceding_comments: list[str]            # user comments above this block

@dataclass
class ConfigDocument:
    path: Path
    hosts: list[HostBlock]                   # in file order
    prologue: list[str]                      # raw lines before the first Host
    epilogue: list[str]                      # raw lines after the last Host (incl. Match blocks)
```

`extra_directives` and `epilogue` are the **extension points**: anything the v1.0 parser doesn't understand is preserved verbatim so `ssh -F <path>` keeps working after round-trip.

### D2. Parser strategy

A line-by-line state machine with four states: `PROLOGUE → COMMENT_BLOCK ↔ HOST_BODY → AFTER_HOSTS`. Implementation: `parse_config(text, path) -> ConfigDocument` at `ssh_config_manager.py:212`.

- A `Host <alias>` line opens a new `HostBlock`; any pending comment-block is attached to it.
- A `Match <...>` line closes the current host and switches to `AFTER_HOSTS`; **the rest of the file is captured verbatim in `epilogue`** (we don't parse Match in v1.0).
- A bare `^Host$` (no alias) raises `ConfigParseError` → exit code 7 / `failure_class="parse_error"`. Silently dropping it would mask a real bug.
- Field key matching is case-insensitive (OpenSSH semantics) but we re-emit with stable capitalization (`HostName`, `User`, `Port`, `IdentityFile`, `ProxyJump`).

### D3. Metadata comment format

Only **four** comment-line keys are interpreted as private metadata; everything else stays in `preceding_comments` verbatim:

```
^\s*#\s*(description|environment|tags|location)\s*:\s*(.+?)\s*$
```

- `tags` is comma-split and trimmed per item.
- Other comment shapes (banners, hand-written notes) round-trip unchanged.
- The render pass **reconstructs** the metadata comments rather than reusing the original lines, so an `update` that changes `tags` writes the new value back in canonical form.

### D4. Atomic write with backup

Sequence implemented in `atomic_write_with_backup(path, content) -> Path` at `ssh_config_manager.py:471`:

1. `shutil.copy2(path, path + ".bak.<unix-ts>")` — preserves the original mode and timestamps in the backup. We don't use hard-links: they fail silently on cross-fs / NFS targets.
2. `path.tmp.write_text(content, encoding="utf-8")`.
3. `tmp.chmod(path.st_mode)` — new file inherits the original mode (typically `0o600`).
4. `os.replace(tmp, path)` — POSIX-atomic; overwrites on Windows where `Path.rename` would not.
5. On any failure mid-flight, the partial `.tmp` is cleaned up so the working copy at `path` is never half-written.

Reads (`list`, `find`) never invoke this path.

### D5. `IdentitiesOnly yes` auto-rule

When a host has `IdentityFile` set, `render` automatically emits `IdentitiesOnly yes` immediately after the `IdentityFile` line **unless** the user has an explicit `IdentitiesOnly` entry in `extra_directives` (e.g. they wrote `IdentitiesOnly no` deliberately).

Why: without `IdentitiesOnly yes`, ssh hands every loaded agent key to the target host until one matches, which trips MaxAuthTries and yields `Too many authentication failures` (already documented in `troubleshooting.md:84`). Default-on is what users want; explicit-off must be respected.

### D6. Failure classification and exit codes

Beyond the `0/1/2/124` baseline in `error-handling.md`, ssh_config_manager introduces five domain-specific exit codes:

| Exit | `data.failure_class` | When |
|------|----------------------|------|
| `4` | `not_found` | `update` / `delete` / `find` on an alias absent from the config |
| `5` | `already_exists` | `create` on an alias already present |
| `6` | `config_missing` | `--config-path` (or `~/.ssh/config`) does not exist |
| `7` | `parse_error` | Config file cannot be parsed (malformed `Host` line, etc.) |
| `8` | `permission` | Read or write denied (e.g. `~/.ssh` mode 000) |

`internal` (exit `1`) is the catch-all for unexpected exceptions; `main()`'s outer `try/except` guarantees no traceback leaks to the user. `error-handling.md` is updated with these five codes alongside the existing `124` precedent from ADR-001.

### D7. argparse default suppression (SUPPRESS)

**Trap encountered and resolved.** Initial implementation declared `--json` / `--config-path` on the top-level parser and again on each subparser via `parents=[common]`. When the subparser re-bound the same arguments with a regular default (e.g. `None`), `args.config_path` resolved to the subparser's default and **silently clobbered** the top-level value. The first end-to-end manual test wrote to the real `~/.ssh/config`.

Fix: both shared flags use `default=argparse.SUPPRESS` in the `common` parent parser. SUPPRESS means "if the user didn't pass this flag, don't touch the namespace attribute," so the top-level value survives. `main()` reads them with `getattr(args, "config_path", None) or "~/.ssh/config"` and `getattr(args, "json", False)`.

This is a **subprocess safety invariant** for any future script that uses `parents=[...]` with shared global flags. Documented inline in `build_parser`'s docstring.

### D8. Match blocks deferred

`Match user X` / `Match exec` / `Match all` blocks are preserved verbatim in `ConfigDocument.epilogue` but not parsed in v1.0. Reason: their semantics are dynamic (evaluated per-connect), so they don't fit the Host-block CRUD shape. Users with existing Match blocks can still round-trip the file safely; v1.1 may introduce a separate `MatchBlock` AST node if a real use case emerges.

### D9. AND semantics for `--tags` filter

`list --tags a,b` returns hosts that have **both** `a` and `b` (set-intersection). Reason: users phrase fleet queries as "the production web servers", not "the production OR web servers". OR semantics can be achieved by two list calls.

### D10. JSON envelope reuse

The script uses `lib.json_result` and `lib.emit` (no duplicates) so it shares the v0.2 contract used by `ssh_execute` and `ssh_cluster`. `data.failure_class` is set on every non-success path; `success` is `True` only when the operation completed as requested.

---

## Consequences

### Positive

- **Drop-in replacement for the stub.** Callers and references already cite the exact command shapes; no other file needed editing besides `error-handling.md` and `index.md`.
- **Round-trip-safe by default.** A `list` on a real config followed by no mutations leaves the file untouched. A user with Match blocks, banner comments, or unfamiliar directives sees them preserved.
- **Cross-script invariant established.** The SUPPRESS pattern is now the recommended way to share global flags across subparsers in this repo (see D7).

### Negative / accepted trade-offs

- **Match blocks are opaque.** A user with complex Match logic can't introspect it via this tool; they fall back to reading the file. v1.1 evaluates a `MatchBlock` node.
- **Render is canonical, not preserving.** Updating any field re-emits the host block with 4-space indent, fixed metadata order, and auto-`IdentitiesOnly yes`. Users with hand-formatted blocks will see diffs after the first `update`. The trade for diff-friendly canonical form was deemed worth it.
- **No concurrent-write locking.** Two processes racing on `create` could lose one write. We document "not concurrent-safe" rather than add fcntl.flock in v1.0; the use case is interactive.
- **Five new exit codes.** Requires the spec update to `error-handling.md` (done in this ADR).

### Downstream impact (other plugins)

- `deploy_pubkey` (planned) can call `ssh_config_manager find <alias>` to introspect the IdentityFile path, and call `ssh_config_manager update --alias X --description "..."` to strip a managed password comment after successful pubkey deployment.
- `ssh_execute` / `ssh_cluster` don't need to change — they already let ssh resolve aliases via `-F ~/.ssh/config`. This tool exists to *manage* the file they rely on.
- Future stubs that need "give me all production web hosts" can call `ssh_config_manager list --environment production --tags web --json` and parse the `data.hosts` array.

---

## Alternatives considered

### A1. Use `paramiko.SSHConfig` for parsing

Rejected. `paramiko.SSHConfig` performs `lookup()` (substitution + resolution) but does not give back an AST that round-trips with comments. We need both **identity** (which Host block is this?) and **ordering** + **comments**, neither of which `paramiko.SSHConfig` preserves. A custom parser is unavoidable for the metadata-comment convention.

### A2. Write directly to `~/.ssh/config` without `.tmp` + `replace`

Rejected. A SIGINT during the write would leave a half-written file. The `.tmp + os.replace` pattern is the standard POSIX recipe; it costs one extra syscall and prevents catastrophic config loss.

### A3. JSON sidecar metadata file

Considered for the metadata (description/environment/tags/location). Rejected: it would diverge from the file users edit by hand, and `ssh` itself ignores comment lines, so embedding metadata inline keeps the convention single-source-of-truth.

### A4. Match-block parsing in v1.0

Rejected. Match semantics are conditional (`Match user X`, `Match exec ...`) and don't fit the Host CRUD shape. Preserving them verbatim in `epilogue` keeps the file usable; parsing them is a separate ADR if/when needed.

### A5. Update mutates only the changed line, preserving original formatting

Rejected. The cost is a second AST that tracks original line ranges per field, and the benefit (smaller diff on update) is marginal. Canonical rendering wins.

---

## Open questions / deferred to v1.1

- `Include` directive expansion. v1.0 reads only the top-level file; included files are not visible to `list`/`find`. Add when a real multi-file user reports a need.
- `MatchBlock` AST node and `match` subcommand. Defer until a downstream caller needs to manage Match blocks programmatically.
- Concurrent-write safety via `fcntl.flock`. Defer; document non-safety until contention is observed.
- Backup garbage collection (e.g. `prune --older-than 30d`). User-driven for now per `references/safety.md`.

---

## Cross-references

- `plugins/ssh-core/skills/ssh-core/scripts/ssh_config_manager.py` — implementation (1055 LOC).
- `tests/test_ssh_config_manager_unit.py` — 47 unit cases.
- `tests/test_ssh_config_manager_integration.py` — 3 live_ssh cases (read-only on real `~/.ssh/config`).
- `.trellis/spec/backend/error-handling.md` — updated exit-code table (D6 above).
- `.trellis/tasks/05-24-ssh-core-ssh-config-manager/{prd,design,implement}.md` — source planning artifacts.
