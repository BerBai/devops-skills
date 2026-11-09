#!/usr/bin/env python3
"""scan_software.py — cached "what's installed?" probe (v1.0).

Public surface::

    scan_software.py <alias> [--refresh] [--no-refresh-stale]
        [--name <tool> | --has <tool>]
        [--catalog PATH] [--json]

One SSH round-trip probes a fixed catalog of common tools (language
runtimes, build chains, GPU stack, container CLIs, IaC, shell utilities,
Xilinx EDA). The remote shell emits NDJSON; the local side normalises
versions through per-entry regex, then caches the full payload at
``~/.devops/cache/<alias>/software.json`` (TTL 7 days, atomic write).

Three modes:

* full scan (default) — refresh cache from remote, summary or JSON.
* ``--name <tool>``  — read one record; cache miss / stale triggers a
  full rescan, then returns just that entry.
* ``--has <tool>``   — shell-style branch helper; exit 0 if present,
  1 if absent, 2 if the tool is not in the catalog.

Cache behaviour follows design.md §8 D6 decision matrix:

==========  ====================  ============  ==========================
--refresh   --no-refresh-stale    cache state   action
==========  ====================  ============  ==========================
✓           *                     *             rescan, write
✗           ✗                     fresh         read cache, 0 SSH
✗           ✗                     stale         auto rescan
✗           ✗                     stale+fail    fall back stale, exit 0
✗           ✓                     stale         read stale, warn, exit 0
✗           *                     missing       scan, write
==========  ====================  ============  ==========================

Exit codes (per-script namespace 75-79; ADR follow-up TBD)::

    0    success (incl. stale-fallback, partial cache from corrupted
         remote line — paired data.failure_class signals the nuance)
    1    generic (unknown tool name in --name)
    2    --has on a tool not in catalog
    75   catalog_load_failed (user catalog parse / schema fail / missing)
    76   cache_write_failed  (os.replace / mkdir / fsync OSError)
    77   cache_corrupt        (cache file exists but unparseable)
    78   ssh_batch_failed     (ssh_execute non-zero)
    79   tool_probe_corrupted (remote emitted unparseable NDJSON line)
    124  ssh_execute timeout (transparent passthrough)

Transport: shell out to ``plugins/ssh-core/.../ssh_execute.py``. Remote
shell script is base64-encoded into a single argv command so that
``ssh_execute.py`` is used through its v1.0 surface only (no stdin
pipe). See design.md §5 D3.

See:
    .trellis/tasks/05-25-ssh-guarded-scan-software/prd.md       — R1-R10
    .trellis/tasks/05-25-ssh-guarded-scan-software/design.md    — §1-§15
    .trellis/spec/backend/adr-001-ssh-execute.md                — transport ADR
    .trellis/spec/backend/error-handling.md                     — envelope contract
    plugins/ssh-guarded/skills/ssh-guarded/SKILL.md             — workflow
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Re-use the shared envelope helpers from ssh-core lib.
# parents[4] == plugins/ — the monorepo plugin root.
_SSH_CORE_LIB = (
    Path(__file__).resolve().parents[4]
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
)
sys.path.insert(0, str(_SSH_CORE_LIB))
from lib import emit  # noqa: E402, I001

# GAP-005: the ssh-guarded envelope (six keys, incl. ``error``) is loaded by
# path under a private module name. Importing ``lib.envelope`` would collide
# with ssh-core's ``lib`` package above; the file itself has no relative
# imports precisely so it can be used this way.
_envelope_spec = importlib.util.spec_from_file_location(
    "ssh_guarded_envelope",
    Path(__file__).resolve().parent / "lib" / "envelope.py",
)
if _envelope_spec is None or _envelope_spec.loader is None:  # pragma: no cover
    raise ImportError("ssh-guarded lib/envelope.py is missing")
_guarded_envelope = importlib.util.module_from_spec(_envelope_spec)
_envelope_spec.loader.exec_module(_guarded_envelope)
GuardedArgumentParser = _guarded_envelope.GuardedArgumentParser
guarded_json_envelope = _guarded_envelope.json_envelope

_version_spec = importlib.util.spec_from_file_location(
    "ssh_guarded_version_cli",
    Path(__file__).resolve().parent / "lib" / "version_cli.py",
)
if _version_spec is None or _version_spec.loader is None:  # pragma: no cover
    raise ImportError("ssh-guarded lib/version_cli.py is missing")
_guarded_version = importlib.util.module_from_spec(_version_spec)
_version_spec.loader.exec_module(_guarded_version)
add_version_flag = _guarded_version.add_version_flag
handle_version = _guarded_version.handle_version

# --- Constants --------------------------------------------------------------

_DEVOPS_HOME = Path(os.path.expanduser("~/.devops"))
_CACHE_DIR = _DEVOPS_HOME / "cache"
_USER_CATALOG_DEFAULT = _DEVOPS_HOME / "software_catalog.json"
_SSH_EXECUTE = (
    Path(__file__).resolve().parents[4]
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_execute.py"
)
_TTL_SECONDS = 7 * 24 * 3600  # design §8 D6
_DEFAULT_TIMEOUT = 60
_REMOTE_VERSION_MAX_BYTES = 4000  # cap version_raw on remote, design §5

# Failure class names + numeric exit codes, per design §10 D8.
FAIL_CATALOG_LOAD = "catalog_load_failed"
FAIL_CACHE_WRITE = "cache_write_failed"
FAIL_CACHE_CORRUPT = "cache_corrupt"
FAIL_SSH_BATCH = "ssh_batch_failed"
FAIL_TOOL_PROBE = "tool_probe_corrupted"

_EXIT_CODES: dict[str | None, int] = {
    None: 0,
    FAIL_CATALOG_LOAD: 75,
    FAIL_CACHE_WRITE: 76,
    FAIL_CACHE_CORRUPT: 77,
    FAIL_SSH_BATCH: 78,
    FAIL_TOOL_PROBE: 79,
}

_HUMAN_HINTS: dict[str, str] = {
    FAIL_CATALOG_LOAD: (
        "catalog load failed. Check --catalog path / "
        "~/.devops/software_catalog.json schema (expect a list of "
        "entries or {'version':1,'patterns':[...]})."
    ),
    FAIL_CACHE_WRITE: (
        "cache write failed: ~/.devops/cache/<alias>/ unwritable or "
        "disk full? Original cache (if any) was preserved."
    ),
    FAIL_CACHE_CORRUPT: (
        "cache file unparseable; delete it or rerun with --refresh."
    ),
    FAIL_SSH_BATCH: (
        "ssh_execute returned non-zero. Check the alias, network, "
        "and remote shell. See data.raw_stderr."
    ),
    FAIL_TOOL_PROBE: (
        "remote shell emitted a malformed NDJSON line for at least one "
        "tool. Partial cache written for the rest; rerun with --refresh."
    ),
}

# --- Default catalog --------------------------------------------------------
#
# 33 entries across 7 categories (design §3 D1). 1:1 with SKILL.md
# `Software scan` listing and the stub docstring.
# Schema: {name, category, probe_cmd, version_cmd, version_regex}
# Xilinx entries use {scan_method='xilinx_dir', xilinx_subdir} instead.

_DEFAULT_CATALOG: list[dict[str, Any]] = [
    # --- lang (11) ---------------------------------------------------------
    {"name": "python", "category": "lang",
     "probe_cmd": "command -v python",
     "version_cmd": "python --version 2>&1",
     "version_regex": r"Python\s+([0-9][0-9.]*)"},
    {"name": "python3", "category": "lang",
     "probe_cmd": "command -v python3",
     "version_cmd": "python3 --version 2>&1",
     "version_regex": r"Python\s+([0-9][0-9.]*)"},
    {"name": "pip", "category": "lang",
     "probe_cmd": "command -v pip",
     "version_cmd": "pip --version 2>&1",
     "version_regex": r"pip\s+([0-9][0-9.]*)"},
    {"name": "conda", "category": "lang",
     "probe_cmd": "command -v conda",
     "version_cmd": "conda --version 2>&1",
     "version_regex": r"conda\s+([0-9][0-9.]*)"},
    {"name": "nodejs", "category": "lang",
     "probe_cmd": "command -v node",
     "version_cmd": "node --version 2>&1",
     "version_regex": r"v?([0-9][0-9.]*)"},
    {"name": "npm", "category": "lang",
     "probe_cmd": "command -v npm",
     "version_cmd": "npm --version 2>&1",
     "version_regex": r"([0-9][0-9.]*)"},
    {"name": "yarn", "category": "lang",
     "probe_cmd": "command -v yarn",
     "version_cmd": "yarn --version 2>&1",
     "version_regex": r"([0-9][0-9.]*)"},
    {"name": "pnpm", "category": "lang",
     "probe_cmd": "command -v pnpm",
     "version_cmd": "pnpm --version 2>&1",
     "version_regex": r"([0-9][0-9.]*)"},
    {"name": "java", "category": "lang",
     "probe_cmd": "command -v java",
     "version_cmd": "java -version 2>&1",
     "version_regex": r'version\s+"?([0-9][0-9._]*)"?'},
    {"name": "mvn", "category": "lang",
     "probe_cmd": "command -v mvn",
     "version_cmd": "mvn --version 2>&1",
     "version_regex": r"Apache Maven\s+([0-9][0-9.]*)"},
    {"name": "gradle", "category": "lang",
     "probe_cmd": "command -v gradle",
     "version_cmd": "gradle --version 2>&1",
     "version_regex": r"Gradle\s+([0-9][0-9.]*)"},
    # --- build (5) ---------------------------------------------------------
    {"name": "gcc", "category": "build",
     "probe_cmd": "command -v gcc",
     "version_cmd": "gcc --version 2>&1",
     "version_regex": r"gcc[^0-9]*([0-9][0-9.]*)"},
    {"name": "g++", "category": "build",
     "probe_cmd": "command -v g++",
     "version_cmd": "g++ --version 2>&1",
     "version_regex": r"g\+\+[^0-9]*([0-9][0-9.]*)"},
    {"name": "clang", "category": "build",
     "probe_cmd": "command -v clang",
     "version_cmd": "clang --version 2>&1",
     "version_regex": r"clang version\s+([0-9][0-9.]*)"},
    {"name": "cmake", "category": "build",
     "probe_cmd": "command -v cmake",
     "version_cmd": "cmake --version 2>&1",
     "version_regex": r"cmake version\s+([0-9][0-9.]*)"},
    {"name": "make", "category": "build",
     "probe_cmd": "command -v make",
     "version_cmd": "make --version 2>&1",
     "version_regex": r"GNU Make\s+([0-9][0-9.]*)"},
    # --- gpu (2) -----------------------------------------------------------
    {"name": "nvidia_driver", "category": "gpu",
     "probe_cmd": "command -v nvidia-smi",
     "version_cmd": (
         "nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -n 1"
     ),
     "version_regex": r"([0-9][0-9.]*)"},
    {"name": "cuda", "category": "gpu",
     "probe_cmd": "command -v nvcc",
     "version_cmd": "nvcc --version 2>&1",
     "version_regex": r"release\s+([0-9][0-9.]*)"},
    # --- containers (5) ----------------------------------------------------
    {"name": "docker", "category": "containers",
     "probe_cmd": "command -v docker",
     "version_cmd": "docker --version 2>&1",
     "version_regex": r"Docker version\s+([0-9][0-9.]*)"},
    {"name": "podman", "category": "containers",
     "probe_cmd": "command -v podman",
     "version_cmd": "podman --version 2>&1",
     "version_regex": r"podman version\s+([0-9][0-9.]*)"},
    {"name": "kubectl", "category": "containers",
     "probe_cmd": "command -v kubectl",
     "version_cmd": (
         "kubectl version --client --short 2>/dev/null "
         "|| kubectl version --client 2>&1"
     ),
     "version_regex": r"v?([0-9][0-9.]+)"},
    {"name": "helm", "category": "containers",
     "probe_cmd": "command -v helm",
     "version_cmd": "helm version --short 2>&1",
     "version_regex": r"v?([0-9][0-9.]*)"},
    {"name": "kustomize", "category": "containers",
     "probe_cmd": "command -v kustomize",
     "version_cmd": "kustomize version --short 2>&1 || kustomize version 2>&1",
     "version_regex": r"v?([0-9][0-9.]*)"},
    # --- iac (6) -----------------------------------------------------------
    {"name": "terraform", "category": "iac",
     "probe_cmd": "command -v terraform",
     "version_cmd": "terraform version 2>&1",
     "version_regex": r"Terraform\s+v?([0-9][0-9.]*)"},
    {"name": "tofu", "category": "iac",
     "probe_cmd": "command -v tofu",
     "version_cmd": "tofu version 2>&1",
     "version_regex": r"OpenTofu\s+v?([0-9][0-9.]*)"},
    {"name": "ansible", "category": "iac",
     "probe_cmd": "command -v ansible",
     "version_cmd": "ansible --version 2>&1",
     "version_regex": r"ansible[^0-9]*([0-9][0-9.]*)"},
    {"name": "helmfile", "category": "iac",
     "probe_cmd": "command -v helmfile",
     "version_cmd": "helmfile --version 2>&1",
     "version_regex": r"v?([0-9][0-9.]*)"},
    {"name": "argocd", "category": "iac",
     "probe_cmd": "command -v argocd",
     "version_cmd": "argocd version --client 2>&1",
     "version_regex": r"argocd:\s*v?([0-9][0-9.]*)"},
    {"name": "flux", "category": "iac",
     "probe_cmd": "command -v flux",
     "version_cmd": "flux --version 2>&1",
     "version_regex": r"v?([0-9][0-9.]*)"},
    # --- shell (5) ---------------------------------------------------------
    {"name": "git", "category": "shell",
     "probe_cmd": "command -v git",
     "version_cmd": "git --version 2>&1",
     "version_regex": r"git version\s+([0-9][0-9.]*)"},
    {"name": "jq", "category": "shell",
     "probe_cmd": "command -v jq",
     "version_cmd": "jq --version 2>&1",
     "version_regex": r"jq-([0-9][0-9.]*)"},
    {"name": "yq", "category": "shell",
     "probe_cmd": "command -v yq",
     "version_cmd": "yq --version 2>&1",
     "version_regex": r"v?([0-9][0-9.]*)"},
    {"name": "rsync", "category": "shell",
     "probe_cmd": "command -v rsync",
     "version_cmd": "rsync --version 2>&1 | head -n 1",
     "version_regex": r"rsync\s+version\s+v?([0-9][0-9.]*)"},
    {"name": "tmux", "category": "shell",
     "probe_cmd": "command -v tmux",
     "version_cmd": "tmux -V 2>&1",
     "version_regex": r"tmux\s+([0-9][0-9.]*)"},
    # --- xilinx (3) — design §6 D4 special path ----------------------------
    {"name": "vivado", "category": "xilinx",
     "scan_method": "xilinx_dir", "xilinx_subdir": "Vivado"},
    {"name": "vitis", "category": "xilinx",
     "scan_method": "xilinx_dir", "xilinx_subdir": "Vitis"},
    {"name": "vivado_hls", "category": "xilinx",
     "scan_method": "xilinx_dir", "xilinx_subdir": "Vivado_HLS"},
]

_DEFAULT_BY_NAME: dict[str, dict[str, Any]] = {
    entry["name"]: entry for entry in _DEFAULT_CATALOG
}

# Schema fields every catalog entry MUST carry; xilinx_dir entries swap
# probe/version fields for xilinx_subdir.
_REQUIRED_FIELDS_CMD = {"name", "category", "probe_cmd", "version_cmd", "version_regex"}
_REQUIRED_FIELDS_XILINX = {"name", "category", "scan_method", "xilinx_subdir"}


# --- Exceptions -------------------------------------------------------------


class CatalogLoadError(Exception):
    """User catalog could not be loaded / parsed / validated."""

    def __init__(self, message: str, path: Path | None = None):
        super().__init__(message)
        self.path = path


class CacheCorruptError(Exception):
    """Cache file exists but is not a valid v1 software.json."""

    def __init__(self, message: str, path: Path | None = None):
        super().__init__(message)
        self.path = path


# --- Parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = GuardedArgumentParser(
        prog="scan_software.py",
        description=(
            "Cache-backed remote software inventory probe. See "
            ".trellis/tasks/05-25-ssh-guarded-scan-software/prd.md."
        ),
    )
    p.add_argument("alias", help="SSH alias (from ~/.ssh/config) or 'local'")
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--name",
        help="Report a single tool record (cache-on-miss triggers full scan)",
    )
    g.add_argument(
        "--has",
        help="Exit 0 if present, 1 if absent, 2 if tool is not in catalog",
    )
    p.add_argument(
        "--refresh", action="store_true",
        help="Ignore cache, force a fresh remote scan",
    )
    p.add_argument(
        "--no-refresh-stale", action="store_true",
        help="Do not auto-rescan when cached payload is past TTL; warn instead",
    )
    p.add_argument(
        "--catalog", default=None,
        help=(
            "Path to a user catalog JSON. Default: "
            "~/.devops/software_catalog.json (silently optional)."
        ),
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit the shared {success,exit_code,stdout,stderr,data} envelope.",
    )
    add_version_flag(p)
    return p


# --- Catalog loading --------------------------------------------------------


def _validate_entry(entry: dict[str, Any]) -> None:
    if not isinstance(entry, dict) or "name" not in entry:
        raise CatalogLoadError(f"catalog entry missing 'name': {entry!r}")
    if entry.get("scan_method") == "xilinx_dir":
        missing = _REQUIRED_FIELDS_XILINX - set(entry.keys())
    else:
        missing = _REQUIRED_FIELDS_CMD - set(entry.keys())
    if missing:
        raise CatalogLoadError(
            f"catalog entry '{entry.get('name')}' missing fields: {sorted(missing)}"
        )


def _load_catalog(arg_path: str | None) -> dict[str, dict[str, Any]]:
    """Return the effective {name: entry} catalog after merging defaults
    with the user catalog (design §4 D2)."""
    base: dict[str, dict[str, Any]] = {
        name: dict(entry) for name, entry in _DEFAULT_BY_NAME.items()
    }

    if arg_path is not None:
        user_path = Path(arg_path).expanduser()
        if not user_path.exists():
            raise CatalogLoadError(
                f"--catalog path does not exist: {user_path}", path=user_path
            )
    else:
        user_path = _USER_CATALOG_DEFAULT
        if not user_path.exists():
            return base  # silent: default path optional

    try:
        with user_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise CatalogLoadError(
            f"could not parse {user_path}: {e}", path=user_path
        ) from e

    # Accept two shapes (design §4 D2 step 4):
    #   1) {"version": 1, "patterns": [entry, ...]}
    #   2) [entry, ...]    (bare list)
    if isinstance(payload, dict):
        if payload.get("version") != 1:
            raise CatalogLoadError(
                f"catalog 'version' must be 1, got {payload.get('version')!r}",
                path=user_path,
            )
        entries = payload.get("patterns")
        if not isinstance(entries, list):
            raise CatalogLoadError(
                f"catalog 'patterns' must be a list, got {type(entries).__name__}",
                path=user_path,
            )
    elif isinstance(payload, list):
        entries = payload
    else:
        raise CatalogLoadError(
            f"catalog root must be list or {{version,patterns}}, got {type(payload).__name__}",
            path=user_path,
        )

    for entry in entries:
        _validate_entry(entry)
        base[entry["name"]] = entry  # whole-entry replace, not field-merge

    return base


# --- Remote shell builder ---------------------------------------------------

_REMOTE_HEADER = r"""
emit_present() {
  printf '{"name":"%s","present":true,"version_raw":"%s"}\n' "$1" "$2"
}
emit_absent() {
  printf '{"name":"%s","present":false}\n' "$1"
}
scan_xilinx() {
  tool="$1"
  name="$2"
  if [ -d "/opt/Xilinx/$tool" ]; then
    ver=$(ls -1 "/opt/Xilinx/$tool" 2>/dev/null | sort -V | tail -n 1)
    if [ -n "$ver" ]; then
      emit_present "$name" "/opt/Xilinx/$tool/$ver"
    else
      emit_absent "$name"
    fi
  else
    emit_absent "$name"
  fi
}
"""


def _shell_escape_single(value: str) -> str:
    """Escape a string for safe single-quote embedding in POSIX sh."""
    return value.replace("'", "'\"'\"'")


def _build_remote_script(catalog: dict[str, dict[str, Any]]) -> str:
    """Generate the POSIX sh payload that probes every catalog entry once.

    Each entry becomes either an ``if command -v X; then ... else ... fi``
    block (most tools) or a ``scan_xilinx <Subdir> <name>`` call (Xilinx
    EDA toolchain). Output is one NDJSON line per entry on stdout.
    """
    parts: list[str] = ["#!/bin/sh", "set -u", _REMOTE_HEADER]
    for name in sorted(catalog.keys()):
        entry = catalog[name]
        if entry.get("scan_method") == "xilinx_dir":
            subdir = _shell_escape_single(entry["xilinx_subdir"])
            n = _shell_escape_single(entry["name"])
            parts.append(f"scan_xilinx '{subdir}' '{n}'")
            continue

        n = _shell_escape_single(entry["name"])
        probe = entry["probe_cmd"]
        version_cmd = entry["version_cmd"]
        # `version_raw` is sanitised on-remote: cap byte count, collapse
        # newlines, escape embedded double quotes (NDJSON safety).
        block = (
            f"if {probe} >/dev/null 2>&1; then\n"
            f"  v=$({version_cmd} | head -c {_REMOTE_VERSION_MAX_BYTES} "
            f"| tr '\\n' ' ' | sed 's/\"/\\\\\"/g')\n"
            f"  emit_present '{n}' \"$v\"\n"
            f"else\n"
            f"  emit_absent '{n}'\n"
            f"fi"
        )
        parts.append(block)
    return "\n".join(parts) + "\n"


# --- Remote scan dispatch ---------------------------------------------------


def _run_remote_scan(
    alias: str, script: str, timeout: int
) -> tuple[str, int, str]:
    """Shell out to ssh_execute.py with the base64'd payload.

    Returns ``(stdout, exit_code, stderr)`` where stdout is the captured
    remote stdout (NDJSON lines). Raises ``subprocess.TimeoutExpired`` if
    the outer subprocess.run hits its own budget.
    """
    payload_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    remote_cmd = f"echo {payload_b64} | base64 -d | bash"
    proc = subprocess.run(
        [
            sys.executable, str(_SSH_EXECUTE), alias, remote_cmd,
            "--json", "--timeout", str(timeout),
        ],
        capture_output=True,
        text=True,
        timeout=timeout + 10,
        check=False,
    )
    try:
        envelope = json.loads(proc.stdout) if proc.stdout else {}
    except json.JSONDecodeError:
        envelope = {}
    if not envelope:
        # ssh_execute should always emit a JSON envelope with --json; treat
        # an empty/broken envelope as a transport failure.
        return "", proc.returncode or 1, proc.stderr or "ssh_execute emitted no envelope"
    return (
        envelope.get("stdout", "") or "",
        int(envelope.get("exit_code", 1)),
        envelope.get("stderr", "") or "",
    )


# --- NDJSON parse + version extract -----------------------------------------


def _extract_version(
    name: str, version_raw: str, catalog: dict[str, dict[str, Any]]
) -> str:
    """Extract a canonical version string from version_raw.

    Xilinx entries treat ``version_raw`` as a path
    (``/opt/Xilinx/Vivado/2023.2``) and slice the trailing segment.
    Command-v entries run the per-entry ``version_regex``.
    """
    entry = catalog.get(name)
    if entry is None:
        return "unknown"
    if entry.get("scan_method") == "xilinx_dir":
        tail = version_raw.rstrip("/").split("/")[-1]
        return tail or "unknown"
    pattern = entry.get("version_regex", "")
    if not pattern:
        return "unknown"
    m = re.search(pattern, version_raw)
    if m is None:
        return "unknown"
    return m.group(1) if m.groups() else m.group(0)


def _parse_ndjson(
    ndjson_text: str, catalog: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Parse NDJSON stdout into ``{name: record}`` + ``corrupted_lines``.

    Each record is enriched with ``scan_method`` + ``category`` from the
    catalog so the cache file is self-describing without re-joining
    against the catalog on read.
    """
    tools: dict[str, dict[str, Any]] = {}
    corrupted: list[str] = []
    for raw_line in ndjson_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            corrupted.append(raw_line)
            continue
        if not isinstance(rec, dict) or "name" not in rec or "present" not in rec:
            corrupted.append(raw_line)
            continue
        name = rec["name"]
        entry = catalog.get(name)
        out: dict[str, Any] = {
            "present": bool(rec["present"]),
            "category": entry["category"] if entry else "unknown",
            "scan_method": (
                entry.get("scan_method", "command_v") if entry else "command_v"
            ),
        }
        if out["present"]:
            version_raw = str(rec.get("version_raw", "")).strip()
            out["version_raw"] = version_raw
            out["version"] = _extract_version(name, version_raw, catalog) if version_raw else "unknown"
        tools[name] = out
    return tools, corrupted


# --- Cache I/O --------------------------------------------------------------


def _cache_path(alias: str) -> Path:
    return _CACHE_DIR / alias / "software.json"


def _cache_read(alias: str) -> dict[str, Any] | None:
    """Load + validate the cache file. Returns None on cache-miss, raises
    ``CacheCorruptError`` on a present-but-broken file."""
    path = _cache_path(alias)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise CacheCorruptError(f"could not parse {path}: {e}", path=path) from e
    if not isinstance(payload, dict):
        raise CacheCorruptError(f"{path}: root is not an object", path=path)
    if payload.get("version") != 1:
        raise CacheCorruptError(
            f"{path}: unsupported version {payload.get('version')!r}", path=path
        )
    if not isinstance(payload.get("tools"), dict):
        raise CacheCorruptError(f"{path}: 'tools' is not an object", path=path)
    return payload


def _cache_write(alias: str, payload: dict[str, Any]) -> None:
    """Atomic write: same-dir temp + fsync + os.replace.

    Raises ``OSError`` on any failure; caller maps to cache_write_failed.
    """
    target = _cache_path(alias)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


def _parse_iso8601_utc(s: str) -> _dt.datetime:
    """Parse the cache's ``scanned_at`` (ISO 8601 with Z or offset)."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(s)


def _is_stale(payload: dict[str, Any]) -> bool:
    try:
        scanned_at = _parse_iso8601_utc(payload["scanned_at"])
    except (KeyError, ValueError):
        return True
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    return (now - scanned_at).total_seconds() > _TTL_SECONDS


# --- Envelope helpers -------------------------------------------------------


def _envelope(
    *,
    success: bool,
    exit_code: int,
    data: dict[str, Any],
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    """Shared six-key envelope (GAP-005): ssh-core shape plus ``error``."""
    error = None
    if not success:
        failure_class = data.get("failure_class")
        error = {
            "message": stderr.strip() or f"exit {exit_code}",
            "failure_class": failure_class,
            "exit_code": int(exit_code),
        }
    return guarded_json_envelope(
        success=success,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        data=data,
        error=error,
    )


def _emit_failure(
    args: argparse.Namespace,
    failure_class: str,
    *,
    data: dict[str, Any] | None = None,
    extra_stderr: str = "",
) -> int:
    payload = {
        "alias": args.alias,
        "failure_class": failure_class,
    }
    if data:
        payload.update(data)
    hint = _HUMAN_HINTS.get(failure_class, "")
    stderr_msg = (extra_stderr.strip() + "\n" if extra_stderr else "") + hint
    if not stderr_msg.endswith("\n"):
        stderr_msg += "\n"
    result = _envelope(
        success=False,
        exit_code=_EXIT_CODES[failure_class],
        data=payload,
        stderr=stderr_msg,
    )
    emit(result, as_json=args.json)
    return int(result["exit_code"])


# --- Scan orchestration -----------------------------------------------------


def _do_scan(
    args: argparse.Namespace,
    catalog: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str], int, str]:
    """Run the remote scan and parse NDJSON.

    Returns ``(payload, corrupted_lines, ssh_exit_code, ssh_stderr)``.
    ``payload`` is a partial cache file dict; if ssh failed it's an empty
    tools dict but ``scanned_at`` / ``server`` still filled so callers
    can inspect.
    """
    script = _build_remote_script(catalog)
    try:
        stdout, exit_code, stderr = _run_remote_scan(
            args.alias, script, _DEFAULT_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return (
            _empty_payload(args.alias),
            [],
            124,
            f"ssh_execute outer timeout after {_DEFAULT_TIMEOUT + 10}s",
        )

    tools, corrupted = _parse_ndjson(stdout, catalog)
    # Ensure every catalog entry shows up — absent tools whose NDJSON line
    # the remote dropped (e.g. partial corrupted payload) should still
    # surface as present=false to keep the schema predictable.
    for name, entry in catalog.items():
        if name not in tools:
            tools[name] = {
                "present": False,
                "category": entry["category"],
                "scan_method": entry.get("scan_method", "command_v"),
            }

    payload = {
        "version": 1,
        "scanned_at": _dt.datetime.now(tz=_dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server": args.alias,
        "tools": tools,
    }
    return payload, corrupted, exit_code, stderr


def _empty_payload(alias: str) -> dict[str, Any]:
    return {
        "version": 1,
        "scanned_at": _dt.datetime.now(tz=_dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "server": alias,
        "tools": {},
    }


# --- Mode dispatchers -------------------------------------------------------


def _render_human_summary(payload: dict[str, Any], *, cache_stale: bool) -> str:
    """Build a per-category summary table for non-JSON full-scan output."""
    tools = payload.get("tools", {})
    by_category: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, rec in tools.items():
        by_category.setdefault(rec.get("category", "unknown"), []).append(
            (name, rec)
        )
    lines: list[str] = []
    header = f"# software scan: {payload.get('server', '?')}"
    if cache_stale:
        header += "  (cache_stale=true)"
    lines.append(header)
    lines.append(f"# scanned_at: {payload.get('scanned_at', '?')}")
    for category in sorted(by_category.keys()):
        items = sorted(by_category[category], key=lambda x: x[0])
        present_names = [n for n, r in items if r.get("present")]
        absent_names = [n for n, r in items if not r.get("present")]
        lines.append(
            f"\n[{category}] {len(present_names)} present / {len(absent_names)} absent"
        )
        for name, rec in items:
            if rec.get("present"):
                ver = rec.get("version", "?")
                lines.append(f"  + {name:<20} {ver}")
            else:
                lines.append(f"  - {name:<20} (absent)")
    return "\n".join(lines) + "\n"


def _scan_and_cache(
    args: argparse.Namespace,
    catalog: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """Run the remote scan, write cache (best-effort), return:

    ``(payload, failure_class_or_None, extra_data)``

    On ssh failure: ``payload=None``, fclass='ssh_batch_failed'.
    On NDJSON corruption: cache still written (partial), fclass=
    'tool_probe_corrupted'.
    On cache_write_failed: payload still returned, fclass set.
    """
    payload, corrupted, exit_code, stderr = _do_scan(args, catalog)
    extra: dict[str, Any] = {"raw_stderr": stderr}

    if exit_code == 124:  # noqa: PLR2004 — GNU timeout(1) convention; see error-handling.md
        return None, "_timeout", extra
    if exit_code != 0:
        extra["ssh_exit_code"] = exit_code
        return None, FAIL_SSH_BATCH, extra

    # Persist before signalling NDJSON corruption — partial cache is
    # better than no cache (design §10 D8 last paragraph).
    try:
        _cache_write(args.alias, payload)
    except OSError as e:
        extra["cache_write_error"] = str(e)
        return payload, FAIL_CACHE_WRITE, extra

    if corrupted:
        extra["corrupted_lines"] = corrupted
        return payload, FAIL_TOOL_PROBE, extra

    return payload, None, extra


def _resolve_cache_payload(  # noqa: PLR0911 — distinct cache-state paths
    args: argparse.Namespace,
    catalog: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool, str | None, dict[str, Any]]:
    """Apply the design §8 D6 decision matrix.

    Returns ``(payload, cache_stale, failure_class, extra_data)``.
    ``failure_class=None`` means success (may still be stale).
    ``payload=None`` means we have nothing to show the user.
    """
    # --refresh forces rescan.
    if args.refresh:
        payload, fclass, extra = _scan_and_cache(args, catalog)
        return payload, False, fclass, extra

    try:
        cached = _cache_read(args.alias)
    except CacheCorruptError as e:
        # Per design §10 D8: report cache_corrupt, then auto-rescan.
        # The corrupt cache itself is preserved on disk until the new
        # write succeeds (we don't unlink).
        rescan_payload, rescan_fclass, rescan_extra = _scan_and_cache(
            args, catalog
        )
        rescan_extra["replaced_corrupt_cache"] = str(e.path) if e.path else ""
        if rescan_payload is None:
            # Rescan also failed; surface cache_corrupt as the primary
            # cause since it pre-dated the network try.
            rescan_extra["rescan_failure_class"] = rescan_fclass
            return None, False, FAIL_CACHE_CORRUPT, rescan_extra
        return rescan_payload, False, rescan_fclass, rescan_extra

    if cached is None:
        # Missing cache: scan (regardless of --no-refresh-stale).
        payload, fclass, extra = _scan_and_cache(args, catalog)
        return payload, False, fclass, extra

    if not _is_stale(cached):
        return cached, False, None, {}

    # Stale.
    if args.no_refresh_stale:
        return cached, True, None, {"warning": "cache stale; --no-refresh-stale set"}

    payload, fclass, extra = _scan_and_cache(args, catalog)
    if payload is None:
        # Rescan failed; fall back to stale cache, exit 0, warn.
        extra["fallback_to_stale"] = True
        extra["warning"] = "cache stale and refresh failed; using stale cache"
        return cached, True, None, extra
    return payload, False, fclass, extra


def _dispatch_full(
    args: argparse.Namespace, catalog: dict[str, dict[str, Any]]
) -> int:
    payload, cache_stale, fclass, extra = _resolve_cache_payload(args, catalog)

    if payload is None:
        if fclass == "_timeout":
            timeout_data = {"alias": args.alias, "failure_class": "timeout", **extra}
            result = _envelope(
                success=False, exit_code=124, data=timeout_data,
                stderr="ssh_execute timed out.\n",
            )
            emit(result, as_json=args.json)
            return 124
        return _emit_failure(
            args, fclass or FAIL_SSH_BATCH, data=extra,
            extra_stderr=extra.get("raw_stderr", ""),
        )

    data: dict[str, Any] = {
        "alias": args.alias,
        "tools": payload["tools"],
        "scanned_at": payload["scanned_at"],
        "cache_stale": cache_stale,
        "failure_class": fclass,
    }
    if fclass == FAIL_TOOL_PROBE:
        data["corrupted_lines"] = extra.get("corrupted_lines", [])
    if extra.get("warning"):
        data["warning"] = extra["warning"]

    success = fclass is None
    exit_code = 0 if success or fclass == FAIL_TOOL_PROBE else _EXIT_CODES[fclass]
    # tool_probe_corrupted is a "warning" in the partial-cache sense
    # (design §10 D8): we still surface it via failure_class but the
    # exit code follows the namespace value (79). cache_stale alone is
    # exit 0 with success=true.
    if fclass == FAIL_TOOL_PROBE:
        success = False
        exit_code = _EXIT_CODES[FAIL_TOOL_PROBE]

    stdout_text = "" if args.json else _render_human_summary(
        payload, cache_stale=cache_stale
    )
    stderr_text = ""
    if cache_stale:
        stderr_text = "warning: cache is stale (TTL exceeded).\n"
    if extra.get("warning"):
        stderr_text += extra["warning"].rstrip() + "\n"

    result = _envelope(
        success=success,
        exit_code=exit_code,
        data=data,
        stdout=stdout_text,
        stderr=stderr_text,
    )
    emit(result, as_json=args.json)
    return exit_code


def _dispatch_name(
    args: argparse.Namespace, catalog: dict[str, dict[str, Any]]
) -> int:
    name = args.name
    if name not in catalog:
        result = _envelope(
            success=False, exit_code=1,
            data={"alias": args.alias, "tool": name,
                  "failure_class": "unknown_tool"},
            stderr=f"unknown tool '{name}' (not in catalog)\n",
        )
        emit(result, as_json=args.json)
        return 1

    payload, cache_stale, fclass, extra = _resolve_cache_payload(args, catalog)
    if payload is None:
        if fclass == "_timeout":
            data = {"alias": args.alias, "tool": name,
                    "failure_class": "timeout", **extra}
            result = _envelope(
                success=False, exit_code=124, data=data,
                stderr="ssh_execute timed out.\n",
            )
            emit(result, as_json=args.json)
            return 124
        return _emit_failure(
            args, fclass or FAIL_SSH_BATCH, data={**extra, "tool": name},
            extra_stderr=extra.get("raw_stderr", ""),
        )

    record = payload["tools"].get(name, {
        "present": False,
        "category": catalog[name]["category"],
        "scan_method": catalog[name].get("scan_method", "command_v"),
    })
    data = {
        "alias": args.alias,
        "tools": {name: record},
        "scanned_at": payload["scanned_at"],
        "cache_stale": cache_stale,
        "failure_class": fclass,
    }
    if record.get("present"):
        stdout_text = "" if args.json else (
            f"{name}: {record.get('version', '?')} ({record.get('category', '?')})\n"
        )
    else:
        stdout_text = "" if args.json else f"{name}: absent\n"
    stderr_text = "warning: cache is stale.\n" if cache_stale else ""

    success = fclass is None
    exit_code = 0 if success or fclass == FAIL_TOOL_PROBE else _EXIT_CODES[fclass]
    if fclass == FAIL_TOOL_PROBE:
        success = False
        exit_code = _EXIT_CODES[FAIL_TOOL_PROBE]

    result = _envelope(
        success=success, exit_code=exit_code, data=data,
        stdout=stdout_text, stderr=stderr_text,
    )
    emit(result, as_json=args.json)
    return exit_code


def _dispatch_has(
    args: argparse.Namespace, catalog: dict[str, dict[str, Any]]
) -> int:
    name = args.has
    if name not in catalog:
        result = _envelope(
            success=False, exit_code=2,
            data={"alias": args.alias, "tool": name,
                  "failure_class": "unknown_tool"},
            stderr=f"unknown tool '{name}' (not in catalog)\n",
        )
        emit(result, as_json=args.json)
        return 2

    payload, cache_stale, fclass, extra = _resolve_cache_payload(args, catalog)
    if payload is None:
        if fclass == "_timeout":
            return _emit_failure(
                args, FAIL_SSH_BATCH, data={"tool": name, **extra},
                extra_stderr="ssh_execute timed out.",
            )
        return _emit_failure(
            args, fclass or FAIL_SSH_BATCH, data={**extra, "tool": name},
            extra_stderr=extra.get("raw_stderr", ""),
        )

    record = payload["tools"].get(name)
    present = bool(record and record.get("present"))
    data = {
        "alias": args.alias,
        "tools": {name: record or {"present": False,
                                    "category": catalog[name]["category"]}},
        "cache_stale": cache_stale,
        "failure_class": fclass,
    }
    result = _envelope(
        success=present,
        exit_code=0 if present else 1,
        data=data,
        stdout="",  # design §9 D7: --has is silent in non-JSON mode
        stderr="warning: cache is stale.\n" if cache_stale else "",
    )
    emit(result, as_json=args.json)
    return 0 if present else 1


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    early = handle_version("scan_software.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    try:
        catalog = _load_catalog(args.catalog)
    except CatalogLoadError as e:
        return _emit_failure(
            args, FAIL_CATALOG_LOAD,
            data={"path": str(e.path) if e.path else None},
            extra_stderr=str(e),
        )

    if args.has:
        return _dispatch_has(args, catalog)
    if args.name:
        return _dispatch_name(args, catalog)
    return _dispatch_full(args, catalog)


if __name__ == "__main__":
    sys.exit(main())
