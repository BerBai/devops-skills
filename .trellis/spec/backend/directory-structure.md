# Directory Structure

> How code is organized in this Claude Code plugin marketplace.

---

## Overview

This repo is **not a backend service** — it is a Claude Code plugin marketplace.
There are no HTTP routes, no controllers, no service layer. The unit of
organization is the **plugin**. Eight plugins ship from this repo, each one
self-contained under `plugins/<name>/`.

The shape is mandated by Claude Code's plugin/skill loader, not by our
preference. Do not invent new top-level layout.

---

## Top-level layout

```
devops-skills/
├── .claude-plugin/marketplace.json   # the marketplace entry — lists all plugins
├── plugins/                          # one directory per plugin
│   ├── ssh-core/                     # v1.0 sample (full scripts + references)
│   ├── ssh-guarded/                  # v1.0 sample (safety layer)
│   ├── remote-debug/
│   ├── k8s-debug/                    # v0.2 scaffold sample
│   ├── docker-quick/
│   ├── log-aggregator/
│   ├── iac-state/
│   └── cicd-debug/
├── tests/                            # pytest manifest linters
└── pyproject.toml
```

Anchors:
- `/.claude-plugin/marketplace.json` — every plugin must be registered here;
  `tests/test_manifests.py::test_marketplace_plugin_sources_resolve` enforces
  marketplace ↔ filesystem alignment.
- `/plugins/ssh-core/` — reference for a fully-fleshed plugin.
- `/plugins/k8s-debug/` — reference for a v0.2 scaffold (script returns
  `exit_code=2` until implemented).

---

## Inside a single plugin

```
plugins/<name>/
├── .claude-plugin/plugin.json        # required; `description` must be >= 40 chars
└── skills/<name>/
    ├── SKILL.md                      # YAML frontmatter + workflow + quick reference
    ├── references/*.md               # progressively-loaded deep references
    └── scripts/*.py                  # Python CLIs Claude Code shells out to
```

Notes:
- Plugin name and skill subdirectory name must match (`plugins/k8s-debug/skills/k8s-debug/`).
- `references/` must be non-empty — `test_references_directory_populated` fails otherwise.
- `scripts/` may contain a `lib/` subpackage for cross-script helpers
  within one plugin. The canonical example is
  `plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py` — 87 lines
  defining the JSON output contract (`json_result` / `emit`), the
  Windows MSYS safety env helper (`msys_safe_env`), per-alias state
  paths (`alias_state_path`), and the shared scaffold stub
  (`unimplemented`). It is the only fully-implemented Python code in
  the v0.2.0 tree.

---

## Cross-plugin coordination

Each plugin is **self-contained**. v0.2 diagnosis plugins
(`k8s-debug` / `docker-quick` / `log-aggregator` / `iac-state` / `cicd-debug`)
need remote execution and `ssh-guarded` needs the same, but **none of them
import `ssh-core` Python directly**. They shell out to the user-installed
`ssh-core` CLI (CONTRIBUTING.md line 29).

What this means in practice:
- No `from ssh_core import ...` across plugin boundaries.
- Cross-plugin contracts are CLI argv + JSON stdout (the JSON output contract,
  see `error-handling.md`).
- A plugin can be removed by the user without breaking the others.

---

## Where new files go

| What you're adding | Where |
|---|---|
| A new plugin | `plugins/<new-name>/.claude-plugin/plugin.json` + `skills/<new-name>/SKILL.md` + register in `.claude-plugin/marketplace.json` |
| A new CLI in an existing plugin | `plugins/<plugin>/skills/<plugin>/scripts/<verb>_<noun>.py` |
| A new reference doc | `plugins/<plugin>/skills/<plugin>/references/<topic>.md` |
| Shared helpers (within ONE plugin) | `plugins/<plugin>/skills/<plugin>/scripts/lib/` |
| A test | `tests/test_*.py` |

What does NOT belong anywhere:
- A top-level `src/` directory.
- Cross-plugin shared Python packages.
- New files at the repo root (the marketplace entry is the only "config" we ship).

## Local helper import collision

Several plugins use the same import package name, `scripts/lib`, because each
plugin is independently distributable. That is safe when scripts are executed as
subprocesses, but it can collide in pytest because Python caches the first
module named `lib` in `sys.modules`.

When a test imports script modules from multiple plugins in one process, newly
implemented scripts that depend on their plugin-local `scripts/lib` should load
that helper by file path (or otherwise use a plugin-unique module name) instead
of relying on a bare `from lib import ...`. This keeps full-suite import order
from changing runtime behavior.
