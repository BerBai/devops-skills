"""Manifest + scaffolding sanity tests.

Run with: pytest tests/

These are deliberately cheap. They check the things that, if broken, would
make `/plugin marketplace add` fail before a user ever sees the skill.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# Auto-discover plugins from marketplace.json so this test file does not need
# to be edited every time a new plugin is added. Marketplace ↔ filesystem
# alignment is enforced by `test_marketplace_plugin_sources_resolve`.
_MARKETPLACE_DATA = json.loads(MARKETPLACE.read_text())
PLUGINS = sorted(p["name"] for p in _MARKETPLACE_DATA["plugins"])


def test_marketplace_json_is_valid() -> None:
    data = json.loads(MARKETPLACE.read_text())
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
    data = json.loads(path.read_text())
    assert data["name"] == plugin
    assert "version" in data
    assert isinstance(data["description"], str) and len(data["description"]) >= 40


@pytest.mark.parametrize("plugin", PLUGINS)
def test_skill_md_has_frontmatter(plugin: str) -> None:
    skill = REPO_ROOT / "plugins" / plugin / "skills" / plugin / "SKILL.md"
    assert skill.exists()
    text = skill.read_text()
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
    data = json.loads(MARKETPLACE.read_text())
    for entry in data["plugins"]:
        source = entry["source"]
        resolved = (REPO_ROOT / source).resolve()
        assert resolved.exists(), f"{entry['name']}.source '{source}' missing"
        assert (resolved / ".claude-plugin" / "plugin.json").exists()


# Smoke test: every script accepts --help without exploding.
_SCRIPTS: list[Path] = [
    p
    for plugin in PLUGINS
    for p in (REPO_ROOT / "plugins" / plugin / "skills" / plugin / "scripts").glob("*.py")
    if p.name != "__init__.py"
]


@pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_script_help(script: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"--help failed: {result.stderr}"
    assert "usage:" in result.stdout.lower()
