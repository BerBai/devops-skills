"""Unit tests for ssh-core/scripts/ssh_config_manager.py v1.0.

All tests use ``tmp_path`` fixtures so the real ``~/.ssh/config`` is
never touched. The live counterpart at
``tests/test_ssh_config_manager_integration.py`` (marker ``live_ssh``)
exercises read-only paths against the real config.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ssh_config_manager as scm  # noqa: E402, I001

# Exit codes (named so PLR2004 doesn't complain about magic numbers).
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_NOT_FOUND = 4
EXIT_ALREADY_EXISTS = 5
EXIT_CONFIG_MISSING = 6
EXIT_PARSE_ERROR = 7
EXIT_PERMISSION = 8


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _write_config(tmp_path: Path, text: str, name: str = "config") -> Path:
    """Write ``text`` to ``tmp_path/name`` and chmod 0o600 (mirrors real ssh)."""
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    p.chmod(0o600)
    return p


def _ns(**kwargs: object) -> object:
    """Build a lightweight argparse-like namespace.

    Defaults match the create/update subparsers (None for every optional
    field, so handlers can use ``if args.foo is not None`` checks safely).
    """
    defaults: dict[str, object | None] = {
        "alias": None,
        "host": None,
        "user": None,
        "port": None,
        "key": None,
        "proxy_jump": None,
        "environment": None,
        "tags": None,
        "description": None,
        "location": None,
    }
    defaults.update(kwargs)
    return type("Args", (), defaults)()


# -----------------------------------------------------------------------------
# Parser happy-path tests
# -----------------------------------------------------------------------------


def test_parser_minimal_host(tmp_path: Path) -> None:
    text = "Host A\n    HostName 1.1.1.1\n"
    doc = scm.parse_config(text, tmp_path / "config")
    assert len(doc.hosts) == 1
    h = doc.hosts[0]
    assert h.alias == "A"
    assert h.host == "1.1.1.1"
    assert h.user is None
    assert h.port is None
    assert h.identity_file is None
    assert h.proxy_jump is None
    assert h.tags == []


def test_parser_full_fields(tmp_path: Path) -> None:
    text = (
        "Host prod-web-01\n"
        "    HostName 10.0.1.11\n"
        "    User deploy\n"
        "    Port 2222\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "    ProxyJump bastion\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    assert len(doc.hosts) == 1
    h = doc.hosts[0]
    assert h.alias == "prod-web-01"
    assert h.host == "10.0.1.11"
    assert h.user == "deploy"
    assert h.port == 2222  # noqa: PLR2004
    assert h.identity_file == "~/.ssh/id_ed25519"
    assert h.proxy_jump == "bastion"


def test_parser_multi_host_preserves_order(tmp_path: Path) -> None:
    text = (
        "Host A\n    HostName 1.1.1.1\n\n"
        "Host B\n    HostName 2.2.2.2\n\n"
        "Host C\n    HostName 3.3.3.3\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    assert [h.alias for h in doc.hosts] == ["A", "B", "C"]


def test_parser_consumes_metadata_comments(tmp_path: Path) -> None:
    text = (
        "# description: prod web\n"
        "# environment: production\n"
        "# tags: web,nginx,prod\n"
        "# location: aliyun-beijing\n"
        "Host prod-web-01\n"
        "    HostName 10.0.1.11\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    h = doc.hosts[0]
    assert h.description == "prod web"
    assert h.environment == "production"
    assert h.tags == ["web", "nginx", "prod"]
    assert h.location == "aliyun-beijing"
    # The metadata comments are CONSUMED; preceding_comments only holds
    # user-written non-metadata context.
    assert h.preceding_comments == []


def test_parser_preserves_user_comments(tmp_path: Path) -> None:
    text = (
        "# my own free-form comment\n"
        "# description: prod web\n"
        "# another user note\n"
        "Host prod\n"
        "    HostName 10.0.1.11\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    h = doc.hosts[0]
    # Metadata extracted...
    assert h.description == "prod web"
    # ...and the non-metadata comments survived in original order.
    assert h.preceding_comments == [
        "# my own free-form comment",
        "# another user note",
    ]


def test_parser_handles_blank_lines_between_hosts(tmp_path: Path) -> None:
    text = (
        "Host A\n    HostName 1.1.1.1\n"
        "\n\n"   # double-blank separator
        "Host B\n    HostName 2.2.2.2\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    assert [h.alias for h in doc.hosts] == ["A", "B"]
    # B's preceding_comments captures the blank lines so render can
    # re-emit them verbatim.
    assert doc.hosts[1].preceding_comments == ["", ""]


# -----------------------------------------------------------------------------
# Parser edge-case tests
# -----------------------------------------------------------------------------


def test_parser_match_block_preserved_to_epilogue(tmp_path: Path) -> None:
    text = (
        "Host A\n    HostName 1.1.1.1\n"
        "\n"
        "Match user root\n"
        "    User admin\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    assert len(doc.hosts) == 1
    assert doc.hosts[0].alias == "A"
    # The Match directive and everything after lives in epilogue verbatim.
    assert "Match user root" in doc.epilogue
    assert "    User admin" in doc.epilogue


def test_parser_handles_mixed_indentation(tmp_path: Path) -> None:
    """OpenSSH tolerates 0/2/4/tab indent; our parser strips whitespace
    before keying off the directive name."""
    text = (
        "Host nopad\n"
        "HostName a.example.com\n"
        "User u1\n"
        "\n"
        "Host twopad\n"
        "  HostName b.example.com\n"
        "  User u2\n"
        "\n"
        "Host tabbed\n"
        "\tHostName c.example.com\n"
        "\tUser u3\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    assert {h.alias: (h.host, h.user) for h in doc.hosts} == {
        "nopad": ("a.example.com", "u1"),
        "twopad": ("b.example.com", "u2"),
        "tabbed": ("c.example.com", "u3"),
    }


def test_parser_case_insensitive_keys(tmp_path: Path) -> None:
    """OpenSSH directive names are case-insensitive."""
    text = (
        "host A\n"
        "    HOSTNAME 1.1.1.1\n"
        "    user u\n"
        "    PORT 22\n"
        "    identityFile ~/.ssh/key\n"
        "    proxyjump bastion\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    h = doc.hosts[0]
    assert h.alias == "A"
    assert h.host == "1.1.1.1"
    assert h.user == "u"
    assert h.port == 22  # noqa: PLR2004
    assert h.identity_file == "~/.ssh/key"
    assert h.proxy_jump == "bastion"


def test_parser_duplicate_alias(tmp_path: Path) -> None:
    """OpenSSH allows duplicate Host entries (each contributes settings).
    v1.0 doesn't merge -- it keeps both, find() returns the first."""
    text = (
        "Host dup\n    HostName 1.1.1.1\n"
        "\n"
        "Host dup\n    HostName 2.2.2.2\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    assert [h.alias for h in doc.hosts] == ["dup", "dup"]
    # find() returns the first occurrence.
    assert doc.find("dup").host == "1.1.1.1"


def test_parser_prologue_only_no_hosts(tmp_path: Path) -> None:
    text = "# global header comment\n# unused metadata: hi\n"
    doc = scm.parse_config(text, tmp_path / "config")
    assert doc.hosts == []
    # Comments survive in prologue (no Host means no consumption).
    assert "# global header comment" in doc.prologue
    assert "# unused metadata: hi" in doc.prologue


def test_parser_empty_file(tmp_path: Path) -> None:
    doc = scm.parse_config("", tmp_path / "config")
    assert doc.hosts == []
    assert doc.prologue == []
    assert doc.epilogue == []
    # Rendering an empty doc must NOT add a phantom newline.
    assert scm.render(doc) == ""


def test_parser_keeps_unknown_directives_verbatim(tmp_path: Path) -> None:
    """User-defined options we don't model land in extra_directives, with
    the original key casing preserved."""
    text = (
        "Host A\n"
        "    HostName 1.1.1.1\n"
        "    UseKeychain yes\n"
        "    AddKeysToAgent yes\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    h = doc.hosts[0]
    assert h.extra_directives == [
        ("UseKeychain", "yes"),
        ("AddKeysToAgent", "yes"),
    ]


def test_parser_equals_form_directive(tmp_path: Path) -> None:
    """OpenSSH allows Key=Value (no space). Verify our split handles it."""
    text = "Host A\n    HostName=1.1.1.1\n    User=u\n"
    doc = scm.parse_config(text, tmp_path / "config")
    h = doc.hosts[0]
    assert h.host == "1.1.1.1"
    assert h.user == "u"


# -----------------------------------------------------------------------------
# Render tests
# -----------------------------------------------------------------------------


def test_render_round_trip_byte_equal(tmp_path: Path) -> None:
    """Normalized input (matching our render order) round-trips byte-equal."""
    text = (
        "# description: prod web\n"
        "# environment: production\n"
        "# tags: web,nginx\n"
        "# location: aliyun-beijing\n"
        "Host prod-web-01\n"
        "    HostName 10.0.1.11\n"
        "    User deploy\n"
        "    Port 22\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "    ProxyJump bastion\n"
        "    IdentitiesOnly yes\n"
        "\n"
        "Host backup-db\n"
        "    HostName 10.0.2.5\n"
        "    User postgres\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    out = scm.render(doc)
    assert out == text


def test_render_preserves_user_comments(tmp_path: Path) -> None:
    """User-authored non-metadata comments survive render."""
    text = (
        "# a free-form user note\n"
        "# description: prod\n"
        "Host A\n"
        "    HostName 1.1.1.1\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    out = scm.render(doc)
    assert "# a free-form user note" in out
    assert "# description: prod" in out


def test_render_metadata_emitted_in_stable_order(tmp_path: Path) -> None:
    """Metadata comments always emit description -> environment -> tags ->
    location, no matter what order the input had them in."""
    text = (
        "# location: dc-east\n"
        "# tags: a,b\n"
        "# environment: dev\n"
        "# description: scrambled order\n"
        "Host A\n"
        "    HostName 1.1.1.1\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    out = scm.render(doc)
    expected_meta_order = [
        "# description: scrambled order",
        "# environment: dev",
        "# tags: a,b",
        "# location: dc-east",
    ]
    lines = out.splitlines()
    # Find where each metadata line appears.
    positions = [lines.index(ln) for ln in expected_meta_order]
    assert positions == sorted(positions), (
        f"metadata not in stable order, got positions {positions}"
    )


def test_render_auto_emits_identities_only(tmp_path: Path) -> None:
    """A Host with IdentityFile but no explicit IdentitiesOnly gets
    `IdentitiesOnly yes` automatically (per troubleshooting.md)."""
    text = (
        "Host A\n"
        "    HostName 1.1.1.1\n"
        "    User u\n"
        "    IdentityFile ~/.ssh/key\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    out = scm.render(doc)
    assert "    IdentitiesOnly yes" in out


def test_render_respects_explicit_identities_only_no(tmp_path: Path) -> None:
    """If the user wrote `IdentitiesOnly no` themselves, the auto-rule
    must NOT clobber it -- we only emit when none is set."""
    text = (
        "Host A\n"
        "    HostName 1.1.1.1\n"
        "    User u\n"
        "    IdentityFile ~/.ssh/key\n"
        "    IdentitiesOnly no\n"
    )
    doc = scm.parse_config(text, tmp_path / "config")
    out = scm.render(doc)
    # The user's explicit "no" is preserved (lives in extra_directives).
    assert "    IdentitiesOnly no" in out
    # And we did NOT add a contradictory "yes" line.
    assert "    IdentitiesOnly yes" not in out


def test_render_no_identityfile_no_identities_only(tmp_path: Path) -> None:
    """Without IdentityFile we shouldn't add IdentitiesOnly."""
    text = "Host A\n    HostName 1.1.1.1\n    User u\n"
    doc = scm.parse_config(text, tmp_path / "config")
    out = scm.render(doc)
    assert "IdentitiesOnly" not in out


def test_render_indents_with_four_spaces(tmp_path: Path) -> None:
    """Even when input uses 2-space indent, we normalize to 4-space."""
    text = "Host A\n  HostName 1.1.1.1\n  User u\n"
    doc = scm.parse_config(text, tmp_path / "config")
    out = scm.render(doc)
    assert "    HostName 1.1.1.1" in out
    assert "    User u" in out


# -----------------------------------------------------------------------------
# cmd_list tests
# -----------------------------------------------------------------------------


def test_cmd_list_no_filters_returns_all(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        (
            "# environment: prod\n"
            "Host A\n    HostName 1.1.1.1\n    User u\n\n"
            "# environment: dev\n"
            "Host B\n    HostName 2.2.2.2\n    User u\n"
        ),
    )
    result = scm.cmd_list(_ns(environment=None, tags=None), cfg)
    assert result["success"] is True
    assert result["exit_code"] == EXIT_OK
    aliases = [h["alias"] for h in result["data"]["hosts"]]
    assert aliases == ["A", "B"]


def test_cmd_list_filters_by_environment(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        (
            "# environment: prod\n"
            "Host A\n    HostName 1.1.1.1\n    User u\n\n"
            "# environment: dev\n"
            "Host B\n    HostName 2.2.2.2\n    User u\n\n"
            "# environment: prod\n"
            "Host C\n    HostName 3.3.3.3\n    User u\n"
        ),
    )
    result = scm.cmd_list(_ns(environment="prod", tags=None), cfg)
    aliases = [h["alias"] for h in result["data"]["hosts"]]
    assert aliases == ["A", "C"]


def test_cmd_list_filters_by_tags_and_semantics(tmp_path: Path) -> None:
    """The --tags filter uses AND, not OR: a host must have EVERY tag."""
    cfg = _write_config(
        tmp_path,
        (
            "# tags: web,prod\n"
            "Host A\n    HostName 1.1.1.1\n    User u\n\n"
            "# tags: web,staging\n"
            "Host B\n    HostName 2.2.2.2\n    User u\n\n"
            "# tags: db,prod\n"
            "Host C\n    HostName 3.3.3.3\n    User u\n"
        ),
    )
    result = scm.cmd_list(_ns(environment=None, tags="web,prod"), cfg)
    aliases = [h["alias"] for h in result["data"]["hosts"]]
    # Only A has BOTH `web` AND `prod`.
    assert aliases == ["A"]


# -----------------------------------------------------------------------------
# cmd_find tests
# -----------------------------------------------------------------------------


def test_cmd_find_hit_returns_full_record(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        (
            "Host A\n"
            "    HostName 1.1.1.1\n"
            "    User u\n"
            "    Port 2222\n"
            "    IdentityFile ~/.ssh/key\n"
            "    ProxyJump bastion\n"
        ),
    )
    result = scm.cmd_find(_ns(alias="A"), cfg)
    assert result["success"] is True
    assert result["exit_code"] == EXIT_OK
    data = result["data"]
    # Field rename: prd output is `key`, not `identity_file`.
    assert data["alias"] == "A"
    assert data["host"] == "1.1.1.1"
    assert data["user"] == "u"
    assert data["port"] == 2222  # noqa: PLR2004
    assert data["key"] == "~/.ssh/key"
    assert data["proxy_jump"] == "bastion"
    assert isinstance(data["raw_lines"], list) and data["raw_lines"]


def test_cmd_find_not_found_returns_exit_4(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, "Host A\n    HostName 1.1.1.1\n    User u\n")
    result = scm.cmd_find(_ns(alias="missing"), cfg)
    assert result["success"] is False
    assert result["exit_code"] == EXIT_NOT_FOUND
    assert result["data"]["failure_class"] == "not_found"
    assert "missing" in result["stderr"]


def test_cmd_find_includes_metadata(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        (
            "# description: prod web\n"
            "# environment: production\n"
            "# tags: web,nginx\n"
            "# location: aliyun-beijing\n"
            "Host A\n"
            "    HostName 1.1.1.1\n"
            "    User u\n"
        ),
    )
    result = scm.cmd_find(_ns(alias="A"), cfg)
    data = result["data"]
    assert data["description"] == "prod web"
    assert data["environment"] == "production"
    assert data["tags"] == ["web", "nginx"]
    assert data["location"] == "aliyun-beijing"


# -----------------------------------------------------------------------------
# cmd_create tests
# -----------------------------------------------------------------------------


def test_cmd_create_happy_writes_and_backs_up(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path, "Host existing\n    HostName 1.1.1.1\n    User u\n"
    )
    pre = cfg.read_text()
    args = _ns(
        alias="new", host="2.2.2.2", user="deploy",
        port=22, key="~/.ssh/key", proxy_jump=None,
        environment="prod", tags="web,nginx", description="d", location=None,
    )
    result = scm.cmd_create(args, cfg)
    assert result["success"] is True
    assert result["exit_code"] == EXIT_OK
    assert result["data"]["action"] == "created"
    # New host visible after create.
    doc = scm.parse_config(cfg.read_text(), cfg)
    assert doc.find("new") is not None
    # Backup file exists and is byte-equal to pre-mutate content.
    backup_path = Path(result["data"]["backup_path"])
    assert backup_path.exists()
    assert backup_path.read_text() == pre


def test_cmd_create_duplicate_returns_exit_5(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path, "Host A\n    HostName 1.1.1.1\n    User u\n"
    )
    pre = cfg.read_text()
    args = _ns(alias="A", host="2.2.2.2", user="dup")
    result = scm.cmd_create(args, cfg)
    assert result["success"] is False
    assert result["exit_code"] == EXIT_ALREADY_EXISTS
    assert result["data"]["failure_class"] == "already_exists"
    # File MUST NOT have been mutated.
    assert cfg.read_text() == pre


def test_cmd_create_minimal_required_only(tmp_path: Path) -> None:
    """create with only the required args (alias/host/user) succeeds."""
    cfg = _write_config(tmp_path, "")  # empty (but file exists)
    args = _ns(alias="minimal", host="1.2.3.4", user="root")
    result = scm.cmd_create(args, cfg)
    assert result["success"] is True
    doc = scm.parse_config(cfg.read_text(), cfg)
    h = doc.find("minimal")
    assert h is not None
    assert h.host == "1.2.3.4"
    assert h.user == "root"
    assert h.port is None  # not set, ssh default applies
    assert h.identity_file is None


def test_cmd_create_with_key_auto_emits_identities_only(tmp_path: Path) -> None:
    """A new Host that declares IdentityFile must emit `IdentitiesOnly yes`
    automatically -- this is the canonical reason the auto-rule exists."""
    cfg = _write_config(tmp_path, "")
    args = _ns(alias="A", host="1.1.1.1", user="u", key="~/.ssh/my-key")
    result = scm.cmd_create(args, cfg)
    assert result["success"] is True
    rendered = cfg.read_text()
    assert "IdentityFile ~/.ssh/my-key" in rendered
    assert "IdentitiesOnly yes" in rendered


# -----------------------------------------------------------------------------
# cmd_update tests
# -----------------------------------------------------------------------------


def test_cmd_update_happy_records_changed_fields(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        "Host A\n    HostName 1.1.1.1\n    User u\n    Port 22\n",
    )
    args = _ns(alias="A", host=None, user="newuser", port=2222, key=None)
    result = scm.cmd_update(args, cfg)
    assert result["success"] is True
    assert result["exit_code"] == EXIT_OK
    assert set(result["data"]["changed_fields"]) == {"user", "port"}
    # Confirm the update actually landed.
    doc = scm.parse_config(cfg.read_text(), cfg)
    h = doc.find("A")
    assert h.user == "newuser"
    assert h.port == 2222  # noqa: PLR2004


def test_cmd_update_not_found_returns_exit_4(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, "Host A\n    HostName 1.1.1.1\n    User u\n")
    pre = cfg.read_text()
    args = _ns(alias="missing", user="x")
    result = scm.cmd_update(args, cfg)
    assert result["success"] is False
    assert result["exit_code"] == EXIT_NOT_FOUND
    assert result["data"]["failure_class"] == "not_found"
    # File untouched on miss.
    assert cfg.read_text() == pre


def test_cmd_update_changed_fields_count_is_accurate(tmp_path: Path) -> None:
    """When the user re-sets a field to the same value, it doesn't appear
    in changed_fields. Same for fields they didn't pass at all."""
    cfg = _write_config(
        tmp_path,
        "Host A\n    HostName 1.1.1.1\n    User u\n    Port 22\n",
    )
    # Re-set user to the same value u; pass port=2222 (genuinely new).
    args = _ns(alias="A", host=None, user="u", port=2222)
    result = scm.cmd_update(args, cfg)
    assert result["data"]["changed_fields"] == ["port"]


# -----------------------------------------------------------------------------
# cmd_delete tests
# -----------------------------------------------------------------------------


def test_cmd_delete_happy_removes_host(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        "Host A\n    HostName 1.1.1.1\n    User u\n\nHost B\n    HostName 2.2.2.2\n    User u\n",
    )
    args = _ns(alias="A")
    result = scm.cmd_delete(args, cfg)
    assert result["success"] is True
    assert result["exit_code"] == EXIT_OK
    doc = scm.parse_config(cfg.read_text(), cfg)
    assert doc.find("A") is None
    assert doc.find("B") is not None


def test_cmd_delete_not_found_returns_exit_4(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, "Host A\n    HostName 1.1.1.1\n    User u\n")
    pre = cfg.read_text()
    args = _ns(alias="ghost")
    result = scm.cmd_delete(args, cfg)
    assert result["success"] is False
    assert result["exit_code"] == EXIT_NOT_FOUND
    assert result["data"]["failure_class"] == "not_found"
    # File untouched on miss.
    assert cfg.read_text() == pre


# -----------------------------------------------------------------------------
# atomic_write_with_backup tests
# -----------------------------------------------------------------------------


def test_atomic_write_with_backup_byte_equal(tmp_path: Path) -> None:
    """The backup file must be byte-equal to ``path`` before the write."""
    path = tmp_path / "config"
    pre = "Host A\n    HostName 1.1.1.1\n"
    path.write_text(pre, encoding="utf-8")
    backup = scm.atomic_write_with_backup(path, "Host B\n    HostName 2.2.2.2\n")
    assert backup.exists()
    assert backup.read_text() == pre
    # And the working copy now has the new content.
    assert path.read_text() == "Host B\n    HostName 2.2.2.2\n"


def test_atomic_write_with_backup_preserves_mode(tmp_path: Path) -> None:
    """The new file inherits the original's permissions (typically 0o600)."""
    path = tmp_path / "config"
    path.write_text("Host A\n", encoding="utf-8")
    path.chmod(0o600)
    pre_mode = stat.S_IMODE(path.stat().st_mode)
    scm.atomic_write_with_backup(path, "Host B\n")
    post_mode = stat.S_IMODE(path.stat().st_mode)
    assert post_mode == pre_mode, f"mode drifted from {oct(pre_mode)} to {oct(post_mode)}"


def test_atomic_write_with_backup_cleans_tmp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``os.replace`` fails mid-flight we must clean up the .tmp file
    so the working copy at ``path`` is never half-written."""
    path = tmp_path / "config"
    path.write_text("PRE", encoding="utf-8")
    pre_text = path.read_text()

    def broken_replace(_src: str, _dst: str) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(scm.os, "replace", broken_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        scm.atomic_write_with_backup(path, "POST")

    # Working copy must be unchanged.
    assert path.read_text() == pre_text
    # .tmp must NOT be left behind.
    tmp = Path(f"{path}.tmp")
    assert not tmp.exists(), "atomic_write_with_backup left a stray .tmp"


# -----------------------------------------------------------------------------
# Failure-class coverage
# -----------------------------------------------------------------------------


def test_failure_class_config_missing(tmp_path: Path) -> None:
    """Pointing --config-path at a non-existent file -> exit 6 / config_missing."""
    missing = tmp_path / "does-not-exist"
    result = scm.cmd_list(_ns(environment=None, tags=None), missing)
    assert result["exit_code"] == EXIT_CONFIG_MISSING
    assert result["data"]["failure_class"] == "config_missing"


def test_failure_class_parse_error(tmp_path: Path) -> None:
    """Malformed config (bad Port value) -> exit 7 / parse_error."""
    cfg = _write_config(tmp_path, "Host A\n    Port not-an-integer\n")
    result = scm.cmd_list(_ns(environment=None, tags=None), cfg)
    assert result["exit_code"] == EXIT_PARSE_ERROR
    assert result["data"]["failure_class"] == "parse_error"


def test_failure_class_permission(tmp_path: Path) -> None:
    """Unreadable config file -> exit 8 / permission.

    We chmod the config to 0o000 and confirm the read attempt raises
    PermissionError that our handler maps to FAIL_PERMISSION. The test
    is skipped when run as root (chmod is meaningless then).
    """
    if os.geteuid() == 0:
        pytest.skip("permission test is meaningless as root (chmod ignored)")
    cfg = _write_config(tmp_path, "Host A\n    HostName 1.1.1.1\n")
    cfg.chmod(0o000)
    try:
        result = scm.cmd_list(_ns(environment=None, tags=None), cfg)
        assert result["exit_code"] == EXIT_PERMISSION
        assert result["data"]["failure_class"] == "permission"
    finally:
        cfg.chmod(0o600)  # so tmp_path teardown can clean it up


def test_failure_class_internal_on_unexpected_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A surprise exception from inside the parser bubbles up as exit 1 /
    internal (the catch-all in main())."""
    cfg = _write_config(tmp_path, "Host A\n    HostName 1.1.1.1\n")

    def explode(_text: str, _path: Path) -> None:
        raise RuntimeError("simulated unexpected blowup")

    monkeypatch.setattr(scm, "parse_config", explode)
    rc = scm.main(["--config-path", str(cfg), "list", "--json"])
    assert rc == EXIT_INTERNAL


# -----------------------------------------------------------------------------
# CLI surface
# -----------------------------------------------------------------------------


def test_help_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse --help exits 0 and lists the subcommand surface."""
    with pytest.raises(SystemExit) as exc_info:
        scm.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    for sub in ("create", "update", "delete", "list", "find"):
        assert sub in out


def test_main_exit_code_propagation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() returns the same exit_code the envelope carries."""
    cfg = _write_config(tmp_path, "Host A\n    HostName 1.1.1.1\n    User u\n")
    rc = scm.main(["--config-path", str(cfg), "find", "missing-alias", "--json"])
    assert rc == EXIT_NOT_FOUND
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == EXIT_NOT_FOUND
    assert payload["data"]["failure_class"] == "not_found"


def test_main_json_flag_either_position(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json works before AND after the subcommand (regression for the
    `parents=[common]` SUPPRESS fix)."""
    cfg = _write_config(tmp_path, "Host A\n    HostName 1.1.1.1\n    User u\n")
    # before
    rc1 = scm.main(["--config-path", str(cfg), "--json", "list"])
    out_before = capsys.readouterr().out
    # after
    rc2 = scm.main(["--config-path", str(cfg), "list", "--json"])
    out_after = capsys.readouterr().out
    assert rc1 == EXIT_OK
    assert rc2 == EXIT_OK
    # Both produce JSON (must parse cleanly).
    json.loads(out_before)
    json.loads(out_after)


# -----------------------------------------------------------------------------
# End-to-end glue via main()
# -----------------------------------------------------------------------------


def test_create_find_update_delete_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: create -> find -> update -> delete on a tmp config.

    Verifies the round-trip works through the actual CLI (main()) and
    that every write produces a .bak.<unix-ts>. We sleep 1s between
    writes so the per-second backup timestamps don't collide.
    """
    cfg = _write_config(
        tmp_path, "Host existing\n    HostName 1.1.1.1\n    User root\n"
    )

    # create
    rc = scm.main([
        "--config-path", str(cfg),
        "create", "--alias", "newhost",
        "--host", "2.2.2.2", "--user", "deploy",
        "--tags", "web,prod",
        "--json",
    ])
    capsys.readouterr()
    assert rc == EXIT_OK
    # find (should hit the just-created host)
    time.sleep(1.1)
    rc = scm.main([
        "--config-path", str(cfg),
        "find", "newhost", "--json",
    ])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == EXIT_OK
    assert payload["data"]["host"] == "2.2.2.2"
    assert payload["data"]["tags"] == ["web", "prod"]
    # update
    time.sleep(1.1)
    rc = scm.main([
        "--config-path", str(cfg),
        "update", "--alias", "newhost", "--port", "2222",
        "--json",
    ])
    capsys.readouterr()
    assert rc == EXIT_OK
    # delete
    time.sleep(1.1)
    rc = scm.main([
        "--config-path", str(cfg),
        "delete", "--alias", "newhost",
        "--json",
    ])
    capsys.readouterr()
    assert rc == EXIT_OK

    # Final config equals the original (we created then deleted newhost).
    final_text = cfg.read_text()
    doc = scm.parse_config(final_text, cfg)
    assert [h.alias for h in doc.hosts] == ["existing"]
    # And we got at least 3 backup files (one per mutation).
    backups = sorted(tmp_path.glob("config.bak.*"))
    assert len(backups) >= 3  # noqa: PLR2004
