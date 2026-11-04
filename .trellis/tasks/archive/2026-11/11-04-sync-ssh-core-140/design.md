# Design: Sync ssh-core 1.4.0 into project plugin

## Source and target

- Source: `ssh-core/skills/ssh-core/` at working-tree version `1.4.0`.
- Target: `plugins/ssh-core/skills/ssh-core/`.
- Metadata target: `plugins/ssh-core/.claude-plugin/plugin.json`.

The target plugin remains self-contained. Other plugins continue to shell out
to its executable scripts and must not import its Python modules.

## Tree replacement strategy

Use a full subtree replacement instead of cherry-picking individual fixes.
The 1.4.0 output pipeline, auth routing, transfer backends, session handling,
audit, and exit-code table are coupled through the expanded shared `lib`
package. Selective backporting would create hidden partial-state drift.

Before copying, remove obsolete files under the target skill subtree. Copy only
source, references, and skill metadata from the standalone subtree. Exclude
`__pycache__`, `.mypy_cache`, local caches, distributions, and editor files.

## Compatibility

The primary cross-plugin surface is:

```text
python3 plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py \
  <host> <command> --json
```

The 1.4.0 script is intentionally additive: it adds authentication, stdin,
follow, sudo, retry, output-file, and stricter validation options. Existing
diagnostic plugins use the base host/command/timeout/JSON surface and do not
import `ssh-core` internals. The obsolete hidden `--no-daemon` option is not
used by project callers.

The target metadata moves from `0.2.0` to `1.4.0`. The root marketplace entry
description may be refreshed, but the root release version remains unchanged.

## Testing strategy

1. Compare the source and target skill trees byte-for-byte after sync.
2. Run focused project tests for ssh-core and the diagnostic plugins that
   shell out to it.
3. Run structural/manifest tests to ensure the plugin remains discoverable.
4. Run compile checks with bytecode disabled and inspect `git status` for
   accidentally copied caches.
5. Update obsolete tests only where they assert removed implementation details;
   do not weaken integration-envelope assertions.

## Documentation and spec

- Record the 1.4.0 source-of-truth relationship and cross-plugin boundary in a
  new backend ADR.
- Update the backend ADR index and error-handling/quality references where they
  still describe the old tree as current.
- Refresh root documentation only where it names ssh-core capabilities or
  dependency behavior.

## Rollback

Because the target is tracked in Git, rollback is `git restore` plus deletion
of newly added plugin files. No data migration or installation state changes
are required.
