"""Manifest + scaffolding sanity tests.

Run with: pytest tests/

These are deliberately cheap. They check the things that, if broken, would
make `/plugin marketplace add` fail before a user ever sees the skill.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# Auto-discover plugins from marketplace.json so this test file does not need
# to be edited every time a new plugin is added. Marketplace → filesystem
# alignment is enforced by `test_marketplace_plugin_sources_resolve`.
_MARKETPLACE_DATA = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
PLUGINS = sorted(p["name"] for p in _MARKETPLACE_DATA["plugins"])


def test_marketplace_json_is_valid() -> None:
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    assert data["name"] == "devops-skills"
    assert isinstance(data["plugins"], list) and len(data["plugins"]) >= 3
    names = {p["name"] for p in data["plugins"]}
    # Sanity: the v0.1 plugins must always be present.
    assert {"ssh-core", "ssh-guarded", "remote-debug"}.issubset(names)
    # And the discovered plugin set must match what we test against.
    assert names == set(PLUGINS)


@pytest.mark.parametrize("plugin", PLUGINS)
def test_plugin_json_exists_and_parses(plugin: str) -> None:
    path = REPO_ROOT / "plugins" / plugin / ".claude-plugin" / "plugin.json"
    assert path.exists(), f"missing {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == plugin
    assert "version" in data
    assert isinstance(data["description"], str) and len(data["description"]) >= 40


@pytest.mark.parametrize("plugin", PLUGINS)
def test_skill_md_has_frontmatter(plugin: str) -> None:
    skill = REPO_ROOT / "plugins" / plugin / "skills" / plugin / "SKILL.md"
    assert skill.exists()
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill} missing YAML frontmatter"
    head, _, _ = text[4:].partition("\n---\n")
    assert "name:" in head and "description:" in head


@pytest.mark.parametrize("plugin", PLUGINS)
def test_references_directory_populated(plugin: str) -> None:
    refs = REPO_ROOT / "plugins" / plugin / "skills" / plugin / "references"
    assert refs.exists() and refs.is_dir()
    mds = list(refs.glob("*.md"))
    assert mds, f"{refs} has no reference files"


def test_marketplace_plugin_sources_resolve() -> None:
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    for entry in data["plugins"]:
        source = entry["source"]
        resolved = (REPO_ROOT / source).resolve()
        assert resolved.exists(), f"{entry['name']}.source '{source}' missing"
        assert (resolved / ".claude-plugin" / "plugin.json").exists()


@pytest.mark.parametrize("plugin", PLUGINS)
def test_marketplace_entry_version_matches_plugin_json(plugin: str) -> None:
    """DEVOPS-GAP-028: every marketplace entry carries the manifest version.

    Claude Code's marketplace decides "update needed?" from the entry's
    ``version``. Seven of the eight entries used to omit it, so an update could
    not be detected -- and nothing failed, because no test read the field.
    """
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    entry = next(p for p in data["plugins"] if p["name"] == plugin)
    manifest = json.loads(
        (
            REPO_ROOT / "plugins" / plugin / ".claude-plugin" / "plugin.json"
        ).read_text(encoding="utf-8")
    )
    assert entry.get("version") == manifest["version"], (
        f"{plugin}: marketplace says {entry.get('version')!r}, "
        f"plugin.json says {manifest['version']!r}"
    )


# Smoke test: every script accepts --help without exploding.
# Private modules (``_name.py``) are internal helpers, not CLIs: they carry no
# ``__main__`` block and are imported by the scripts next to them. The locator
# that resolves ssh-core is the first of those.
_SCRIPTS: list[Path] = [
    p
    for plugin in PLUGINS
    for p in (REPO_ROOT / "plugins" / plugin / "skills" / plugin / "scripts").glob("*.py")
    if p.name != "__init__.py" and not p.name.startswith("_")
]


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_script_help(script: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"--help failed: {result.stderr}"
    assert "usage:" in result.stdout.lower()


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_script_version(script: Path) -> None:
    """DEVOPS-GAP-004: every public script answers --version with x.y.z.

    Half the marketplace used to raise ``error: the following arguments are
    required`` for ``--version``: the flag was handled after argparse demanded
    positionals, or never defined. "Which build is installed here" is the first
    question in most triage sessions.
    """
    result = subprocess.run(
        [sys.executable, str(script), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        f"--version failed ({result.returncode}): {result.stderr}"
    )
    assert re.search(r"\d+\.\d+\.\d+", result.stdout), (
        f"--version did not print a semver: {result.stdout!r}"
    )
    assert "required" not in result.stderr.lower(), (
        f"--version was eaten by argparse: {result.stderr}"
    )


def test_script_version_is_consistent_with_plugin_manifest() -> None:
    """The version a script prints comes from its plugin.json, not a copy."""
    for plugin in PLUGINS:
        manifest = json.loads(
            (
                REPO_ROOT / "plugins" / plugin / ".claude-plugin" / "plugin.json"
            ).read_text(encoding="utf-8")
        )
        declared = manifest["version"]
        script = next(
            (
                p
                for p in (REPO_ROOT / "plugins" / plugin / "skills" / plugin / "scripts").glob("*.py")
                if p.name != "__init__.py" and not p.name.startswith("_")
            ),
            None,
        )
        assert script is not None, f"{plugin} has no public script to check"
        result = subprocess.run(
            [sys.executable, str(script), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        assert declared in result.stdout, (
            f"{plugin}: plugin.json says {declared}, --version printed "
            f"{result.stdout!r}"
        )


def test_script_version_json_envelope() -> None:
    """``--version --json`` must satisfy the shared envelope contract."""
    for script in _SCRIPTS:
        result = subprocess.run(
            [sys.executable, str(script), "--version", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, f"{script.name} --version --json failed"
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        assert payload["exit_code"] == 0
        assert re.fullmatch(r"\d+\.\d+\.\d+", payload["data"]["version"])


def test_every_skill_md_documents_a_workflow() -> None:
    """CONTRIBUTING.md promises a `When to use` + `Workflow` section per skill.

    The skill docs drifted: 7 of 8 used `## Decision tree` (or
    `## Entry decision tree`) while the documented lint rule demanded
    `Workflow`. `When to use` is mandatory; the flow section may be either
    `Workflow` or a decision tree, which is the same thing under a different
    name.
    """
    flow_headings = ("## workflow", "## decision tree", "## entry decision tree")
    for plugin in PLUGINS:
        skill = REPO_ROOT / "plugins" / plugin / "skills" / plugin / "SKILL.md"
        lowered = skill.read_text(encoding="utf-8").lower()
        assert "## when to use" in lowered, f"{plugin}/SKILL.md missing '## When to use'"
        assert any(h in lowered for h in flow_headings), (
            f"{plugin}/SKILL.md needs '## Workflow' or '## Decision tree'"
        )


def test_skill_copies_are_identical() -> None:
    """The ssh-core locator is vendored per plugin; the copies must not drift."""
    copies = sorted(
        p
        for p in REPO_ROOT.glob("plugins/*/skills/*/scripts/**/_ssh_core_locator.py")
        if "__pycache__" not in p.parts
    )
    assert copies, "the ssh-core locator is missing from every plugin"
    reference = copies[0].read_text(encoding="utf-8")
    for copy in copies[1:]:
        assert copy.read_text(encoding="utf-8") == reference, (
            f"{copy.relative_to(REPO_ROOT)} drifted from "
            f"{copies[0].relative_to(REPO_ROOT)}"
        )


def test_vendored_locator_matches_template() -> None:
    """DEVOPS-OPT-002: the seven copies are generated, not hand-synced.

    Editing one copy in place used to be the only way to "fix" a drift, which
    is how a real fix gets reverted. The template under ``scripts/templates/``
    is now the source of truth and ``scripts/sync_vendored.py`` renders the
    copies; this test is the CI half of that contract.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import sync_vendored
    finally:
        sys.path.pop(0)

    problems = [p for p in sync_vendored.check_all() if "_ssh_core_locator" in p]
    assert not problems, (
        "the vendored ssh-core locator is out of date:\n  "
        + "\n  ".join(problems)
        + "\nRun `python scripts/sync_vendored.py` and commit the result."
    )


def test_vendored_version_helper_matches_template() -> None:
    """DEVOPS-GAP-004: every plugin reads its version from one helper."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import sync_vendored
    finally:
        sys.path.pop(0)

    problems = [p for p in sync_vendored.check_all() if "_version.py" in p]
    assert not problems, (
        "the vendored _version.py helper is out of date:\n  "
        + "\n  ".join(problems)
        + "\nRun `python scripts/sync_vendored.py` and commit the result."
    )

