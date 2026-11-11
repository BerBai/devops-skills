"""Runtime state directory for ssh-guarded (GAP-006).

State used to live in ``plugins/ssh-guarded/skills/ssh-guarded/reports/`` — the
installed skill directory. That is read-only on most installs, is wiped by an
upgrade (losing pending approvals), and mixes projects together.

The tests here pin the new resolution rules:

* ``$SSH_GUARDED_HOME`` decides the location; the default is
  ``~/.devops/ssh-guarded``.
* ``--reports-dir`` replaces the ``.../reports`` level.
* the layout is ``reports/<project_id>/{requests,jobs}``.
* the plugin tree is never written to, and leftovers there produce a one-time
  note instead of being deleted.
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "scripts"
)
PLUGIN_REPORTS = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "reports"
)

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import request_command as _rc_mod  # noqa: E402
from lib import request_artifact as _ra  # noqa: E402
from lib import state  # noqa: E402

request_command = _rc_mod

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _stable_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_GUARDED_PROJECT_ID", "state-proj")


# --- resolution -------------------------------------------------------------


def test_state_home_default_is_under_the_user_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSH_GUARDED_HOME", raising=False)
    assert state.state_home() == Path.home() / ".devops" / "ssh-guarded"


def test_state_home_reads_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "custom"))
    assert state.state_home() == tmp_path / "custom"


def test_reports_root_is_state_home_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    assert state.reports_root() == tmp_path / "state" / "reports"


def test_reports_dir_override_beats_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    override = tmp_path / "ci"
    assert state.reports_root(reports_dir=override) == override


def test_layout_is_per_project(tmp_path: Path) -> None:
    root = tmp_path / "reports"
    assert state.requests_dir("proj-a", reports_dir=root) == (
        root / "proj-a" / "requests"
    )
    assert state.jobs_dir("proj-a", reports_dir=root) == root / "proj-a" / "jobs"
    assert state.requests_dir("proj-b", reports_dir=root) != (
        state.requests_dir("proj-a", reports_dir=root)
    )


def test_two_projects_do_not_see_each_other(tmp_path: Path) -> None:
    root = tmp_path / "reports"
    a = state.requests_dir("proj-a", reports_dir=root)
    b = state.requests_dir("proj-b", reports_dir=root)
    a.mkdir(parents=True)
    (a / "command-aaaaaaaa.json").write_text("{}", encoding="utf-8")
    b.mkdir(parents=True)
    assert [p.name for p in b.glob("*.json")] == []
    assert state.project_ids(root) == ["proj-a", "proj-b"]


def test_empty_project_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        state.requests_dir("", reports_dir=tmp_path)


def test_missing_root_lists_no_projects(tmp_path: Path) -> None:
    assert state.project_ids(tmp_path / "nope") == []


# --- legacy in-tree artifacts ----------------------------------------------


def test_legacy_reports_root_points_at_the_plugin_tree() -> None:
    assert state.legacy_reports_root() == PLUGIN_REPORTS
    assert state.legacy_reports_root().name == "reports"


def test_legacy_note_is_silent_when_the_old_tree_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))

    class _Sink:
        text = ""

        def write(self, chunk: str) -> None:
            self.text += chunk

    sink = _Sink()
    assert state.warn_legacy_reports(stream=sink) is None
    assert sink.text == ""
    assert not (tmp_path / "state" / state.LEGACY_NOTE_MARKER).exists()


def test_legacy_artifact_count_reads_the_old_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_reports = tmp_path / "fake-skill" / "reports"
    (fake_reports / "requests").mkdir(parents=True)
    (fake_reports / "jobs").mkdir(parents=True)
    (fake_reports / "requests" / "command-aaaaaaaa.json").write_text(
        "{}", encoding="utf-8"
    )
    (fake_reports / "jobs" / "job-bbbbbbbb.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(state, "legacy_reports_root", lambda: fake_reports)
    assert state.legacy_artifact_count() == 2


def test_legacy_note_prints_once_per_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_reports = tmp_path / "fake-skill" / "reports"
    (fake_reports / "requests").mkdir(parents=True)
    (fake_reports / "requests" / "command-aaaaaaaa.json").write_text(
        "{}", encoding="utf-8"
    )
    monkeypatch.setattr(state, "legacy_reports_root", lambda: fake_reports)
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))

    class _Sink:
        def __init__(self) -> None:
            self.text = ""

        def write(self, chunk: str) -> None:
            self.text += chunk

    first = _Sink()
    message = state.warn_legacy_reports(stream=first)
    assert message is not None
    assert str(fake_reports) in message
    assert "Nothing was moved or deleted" in message
    assert (tmp_path / "state" / state.LEGACY_NOTE_MARKER).exists()
    # The old files are untouched.
    assert (fake_reports / "requests" / "command-aaaaaaaa.json").exists()

    second = _Sink()
    assert state.warn_legacy_reports(stream=second) is None
    assert second.text == ""


# --- drafters write outside the plugin tree --------------------------------


def test_drafter_writes_under_ssh_guarded_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a real ``main()`` run lands in ``$SSH_GUARDED_HOME``."""
    state_home = tmp_path / "state"
    monkeypatch.setenv("SSH_GUARDED_HOME", str(state_home))
    rc = request_command.main(["pai", "echo hi", "--reason", "x", "--json"])
    assert rc == 0
    expected = state_home / "reports" / "state-proj" / "requests"
    written = list(expected.glob("command-*.json"))
    assert len(written) == 1
    body = json.loads(written[0].read_text())
    assert body["operation"] == "command"


def test_drafter_reports_dir_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    override = tmp_path / "ci-reports"
    rc = request_command.main(
        [
            "pai", "echo hi", "--reason", "x",
            "--reports-dir", str(override), "--json",
        ]
    )
    assert rc == 0
    assert list((override / "state-proj" / "requests").glob("command-*.json"))
    assert not (tmp_path / "state" / "reports" / "state-proj" / "requests").exists()


def test_drafter_never_writes_into_the_plugin_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    before = sorted(str(p) for p in PLUGIN_REPORTS.rglob("*") if p.is_file())
    assert request_command.main(["pai", "echo hi", "--reason", "x", "--json"]) == 0
    after = sorted(str(p) for p in PLUGIN_REPORTS.rglob("*") if p.is_file())
    assert before == after


def test_allocate_request_id_creates_dirs_on_demand(tmp_path: Path) -> None:
    """First-ever draft must work with no pre-created state home."""
    target = tmp_path / "brand-new" / "reports" / "p" / "requests"
    assert not target.exists()
    rid = _ra.allocate_request_id("command", target)
    assert (target / f"{rid}.json").exists()


def test_state_home_env_is_honoured_when_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SSH_GUARDED_HOME", "relative-state")
    resolved = state.state_home()
    assert resolved == Path("relative-state")
    assert state.reports_root() == Path("relative-state") / "reports"


def test_no_bytecode_or_state_left_in_plugin_tree_after_imports() -> None:
    """Importing the scripts must not create runtime state next to them."""
    stray = [
        p
        for p in PLUGIN_REPORTS.rglob("*")
        if p.is_file() and p.name != "README.md"
    ]
    assert stray == []
