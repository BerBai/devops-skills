# ADR-004: track the standalone ssh-core 1.5.0 tree

- **Status:** Accepted (reconstructed record); version identity superseded by the `1.0.0` unification
- **Decision date:** 2026-11-09, the date of sync commit `d46dee9`
- **Supersedes:** the 1.4.0 sync ADR deleted in `1a6eade`
- **Source commits:** `1a6eade^:.trellis/spec/backend/adr-004-ssh-core-140-sync.md`
  (recovered text), `d46dee9` (the 1.5.0 sync), `0ae5d45` (its test surface),
  `1ead9e8` (the reference to ADR-004 in user-facing docs)

> **Version note (2026-11-09).** This ADR records the decision to sync the
> standalone **1.5.0** feature set into the plugin. The marketplace then
> unified every plugin, including `ssh-core`, to distribution version
> **1.0.0**; `plugin.json`, `lib/version.py` and `--version` now report
> `1.0.0`. The `1.4.0`/`1.5.0` references below are feature history, not the
> current distribution version. See the `1.0.0` entry in
> [`CHANGELOG.md`](../../CHANGELOG.md).

## Why this file is a reconstruction

The repository referenced ADR-004 from `README.md`, `CONTRIBUTING.md` and
`CONTRIBUTING.zh-CN.md`, but no `docs/adr/` file ever existed in the tracked
tree. `git log --all -- '*ADR*'` shows the only ADR text that was ever tracked
lived under `.trellis/spec/backend/` and was deleted in `1a6eade`. That text
documents the **1.4.0** sync, not 1.5.0.

This ADR therefore does two things, and says which is which:

1. It restates the surviving 1.4.0 decision, attributed to the deleted blob.
2. It records the 1.5.0 decision that commit `d46dee9` actually implements,
   derived from that diff and the tests that followed. It does **not** invent
   rationale that no commit, test or code comment supports.

## Provenance (section → source)

| Section | Source |
|---|---|
| "Why this file is a reconstruction" | `git log --all -- '*ADR*'`; `1a6eade` deletion |
| Context | `1a6eade^:.trellis/spec/backend/adr-004-ssh-core-140-sync.md` (1.4.0 text); `d46dee9` stat; `version.py:40-49` |
| Decision D1/D2 | `git diff --name-status d46dee9^ d46dee9`; file line counts at `aa6bda6` |
| Decision D3 | `1a6eade^` ADR-004 decision 3; `docs/architecture.md:33-45` |
| Decision D4 | `git diff d46dee9^ d46dee9 -- plugins/ssh-core/.claude-plugin/plugin.json` |
| Decision D5 | `git diff d46dee9^ d46dee9 -- .../lib/exit_codes.py` |
| Decision D6 | `git show --stat 0ae5d45` |
| "What changed" | `d46dee9` diff; `SKILL.md:57-70`, `:159-173`; live `pai` transcript |
| Consequences / Alternatives | 1.4.0 ADR text recovered above; current manifests |
| Verification | commands in the block at the end of this file |

## Context

The 1.4.0 decision (recovered verbatim in substance from
`1a6eade^:.trellis/spec/backend/adr-004-ssh-core-140-sync.md`) was:

> Replace the whole `plugins/ssh-core/skills/ssh-core/` subtree with the
> standalone repository's matching subtree at version `1.4.0`; keep the plugin
> self-contained; treat the CLI argv + JSON envelope as the only cross-plugin
> contract.

The 1.4.0 ADR also said: "Future ssh-core changes must be synchronized
deliberately; the standalone repository remains the development source of
truth." Commit `d46dee9` is the next such deliberate sync. Its stat is 41 files
changed, 5549 insertions, 308 deletions, and the plugin manifest moves from
`1.4.0` to `1.5.0`.

The 1.5.0 work is a field-review release. The version history in
`scripts/lib/version.py:40-49` names the themes: a launcher for hosts without
`python`, askpass scratch-directory and Windows ACL repair, `doctor.py`, `--user`
/ `user_required`, `--allow-exit`, batch `results[].stderr`, `data.host_id`,
`--output-file`, `ssh_slice.py`, and `--script-file` / `--normalize-lf`.

## Decision

**D1. Replace the plugin subtree at 1.5.0 rather than cherry-picking.**
`d46dee9` changes 41 files at once, including `lib/transfer.py` (+519),
`ssh_execute.py` (+780), `ssh_cluster.py` (+384), `ssh_session.py` (+114) and
`lib/__init__.py` (+267). The 1.4.0 ADR rejected selective backports for the same
reason: the output pipeline, auth helpers, transfer backends, session handling
and exit-code table are coupled through the shared library.

**D2. Add the new entry points inside the plugin, not as external tooling.**
`d46dee9` adds exactly four files:

| File | Lines at `aa6bda6` | Role |
|---|---|---|
| `doctor.py` | 1606 | capability probes; `--profile`, `--require`, `--host` |
| `ssh_slice.py` | 703 | bounded line/byte/stat/grep reads of a remote file |
| `run.sh` | 31 | POSIX launcher for hosts without `python` on `PATH` |
| `run.cmd` | 69 | Windows launcher with the same fallback chain |

No standalone release manifest, build script or test harness is copied into the
plugin. This preserves the 1.4.0 "self-contained bundle" rule.

**D3. Keep the cross-plugin contract at argv + JSON.** `d46dee9` does not add an
import path from another plugin into `ssh-core`; the boundary decision is
recorded separately in [ADR-005](./ADR-005-ssh-core-boundary.md).

**D4. Publish the version and the new capabilities in the plugin metadata.**
`plugins/ssh-core/.claude-plugin/plugin.json` moves `1.4.0 -> 1.5.0` and adds
"log following and slicing", "capability probing (selfcheck/doctor)" and
"SFTP/scp/tar/rsync file transfer" to the description in the same commit.

**D5. Extend the failure-class table instead of reusing unrelated codes.**
`lib/exit_codes.py` gains `askpass_unavailable` (114) and `user_required` (115).
Both are registered in `EXIT_CODES`, so `exit_code_for()` returns them rather
than falling back to `internal`.

**D6. Cover the synced surface with project-local tests.** Commit `0ae5d45`
(the next commit) adds `tests/test_ssh_core_slice_json_and_doctor_profile.py`
(623 lines), extends `tests/test_ssh_core_session.py` (+671), and adds
upload/download unit suites. Those tests are the behaviour contract for the new
flags; the ADR does not claim behaviour that only the standalone repository
tests.

## What changed from 1.4.0 to 1.5.0

| Capability | Evidence in this checkout |
|---|---|
| Launcher fallback (`$SSH_CORE_PYTHON`, `$DSH_PYTHON`, `python`/`py -3`, harness payload) | `scripts/run.sh`, `scripts/run.cmd`; `SKILL.md:57-70` |
| `doctor.py` profiles and `--require` | `doctor.py --help`; `tests/test_ssh_core_slice_json_and_doctor_profile.py` |
| `ssh_slice.py` line/byte windows, `--append`, `next_offset` | `ssh_slice.py --help`; live run against `pai:/etc/debian_version` |
| `--output-file` streams stdout to disk | `ssh_execute.py --help`; parser action `output_file` |
| `--script-file` / `--normalize-lf` / `--keep-crlf` | `ssh_execute.py --help` |
| `--allow-exit` / `--expect-exit` | `ssh_execute.py --help`; `EXIT_CODES` unchanged for the command's own code |
| `user_required` / `askpass_unavailable` | `exit_codes.py:77-81`, `:162-163` |
| Batch `results[].stderr`, consistent batch exit code | `ssh_execute.py --help`; `tests/test_ssh_execute_integration.py` |

## Consequences

- A user installing `ssh-core@1.5.0` gets `doctor.py`, `ssh_slice.py` and the
  launchers without installing the standalone repository.
- The 1.4.0 surface stays additive: existing flags keep their names and the
  envelope keeps `{success, exit_code, stdout, stderr, data}`.
- `doctor.py` and `selfcheck.py` are now two different contracts: `selfcheck`
  reports tool presence, `doctor` exercises capabilities. The 1.5.0 docs state
  that distinction, and `SKILL.md` points at both.
- At the time of the sync the plugin version was `1.5.0` while the marketplace
  was `0.2.0`. The marketplace has since unified every plugin, including
  `ssh-core`, to `1.0.0`; the marketplace entry and `plugin.json` must still
  agree per plugin, which
  `tests/test_manifests.py::test_marketplace_entry_version_matches_plugin_json`
  enforces.

## Alternatives considered (from the surviving 1.4.0 record)

- **Selective backports.** Rejected then and still rejected now: the 1.5.0 diff
  touches the same coupled files, and a partial copy would be an untested
  hybrid.
- **Vendor the whole standalone repository.** Rejected: it would add release
  tooling, CI configuration, packaging manifests and duplicate tests that do not
  belong in a Claude plugin bundle.
- **Keep the plugin at 1.4.0 and document 1.5.0 as "coming".** Rejected because
  `d46dee9` ships the code; documenting it as future would violate
  `CONTRIBUTING.md`'s "docs examples must show shipped behaviour only" rule.

## Verification

```text
$ python scripts/gen_cli_reference.py --check --all
ssh-core CLI reference: OK (45621 bytes)
CLI inventory: OK (8 plugins, ssh-core reference current)

$ .venv/bin/python plugins/ssh-core/skills/ssh-core/scripts/selfcheck.py --json | jq -r .data.version
1.0.0

$ .venv/bin/python plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py pai "uname -srm" --json
{"success": true, "exit_code": 0, "stdout": "Linux 7.0.13+deb13-amd64 x86_64\n", ...}
```
