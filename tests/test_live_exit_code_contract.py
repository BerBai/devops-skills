"""The live suite's exit-code assertions must come from the product's tables.

DEVOPS-GAP-002's third acceptance criterion: "a unit test asserting that the set
of test constants is a subset of ``exit_codes``". The drift it guards against is
subtle but expensive — the transfer family moved to 160-168 several releases
ago, while the live modules kept asserting a local ``EXIT_GENERIC_FAILURE = 1``
and reported nine false failures on every clean machine.

The check is static: it reads the live modules' ASTs, it never runs them.

What counts as "the product's tables":

* ``plugins/ssh-core/.../scripts/lib/exit_codes.py`` — ``EXIT_CODES``, the
  shared table every ssh-core script maps a ``failure_class`` through;
* every other ``EXIT_CODES``/``_EXIT_CODES`` dict declared by a shipped script
  (log-aggregator's 40-99 block, ssh-guarded's 30-65 and 75-79 blocks). Those
  plugins own their namespaces on purpose — ``anchor_not_found`` is 90 there and
  is not in ssh-core's table — so the registry is the union, discovered by AST
  instead of copied into this file.

Remote-command statuses (1 for ``false``, 42, 127, ...) are *not* registry
codes: ssh passes a command's own status through verbatim. They are allowed, but
only through a constant whose name says what it is (see
:data:`ALLOWED_LITERAL_NAMES`), never as a bare number in an assertion.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from lib import exit_codes as codes

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
PLUGINS_DIR = REPO_ROOT / "plugins"

#: Code names that may legitimately hold a literal, because the number is the
#: remote command's status (or a per-script code whose meaning the name spells
#: out). The *name* is the documentation; a bare ``EXIT_* = 1`` is not.
ALLOWED_LITERAL_NAMES = frozenset(
    {
        # Remote-command statuses passed through verbatim (never ssh's own).
        "EXIT_REMOTE_COMMAND_FAILED",
        "EXIT_CUSTOM",
        "EXIT_MIXED_IO",
        "EXIT_NO_COMMAND",
        # diagnose_host's aggregate "some probe failed" envelope code.
        "EXIT_FAIL",
        # ssh-guarded / log-aggregator / scan_software per-script namespaces.
        "EXIT_SANDBOX_REFUSAL",
        "EXIT_DENYLIST_REFUSAL",
        "EXIT_REQUEST_NOT_EXECUTABLE",
        "EXIT_HAS_ABSENT",
        "EXIT_HAS_NOT_IN_CATALOG",
    }
)

#: The exact local redefinition DEVOPS-GAP-002 removed. Banned by name so the
#: old pattern cannot quietly come back with a plausible value.
BANNED_NAMES = frozenset({"EXIT_GENERIC_FAILURE", "EXIT_SSH_CLIENT_FAILURE"})


def _live_module_paths() -> list[Path]:
    """Return the modules that carry the ``live_ssh`` marker."""
    candidates = sorted(
        {
            *TESTS_DIR.glob("test_*_integration.py"),
            *TESTS_DIR.glob("test_*_live.py"),
        }
    )
    return [
        path
        for path in candidates
        if "live_ssh" in path.read_text(encoding="utf-8")
    ]


def _module_exit_assignments(path: Path) -> dict[str, int]:
    """``{name: int}`` for every module-level ``EXIT_*`` integer assignment."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, int)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("EXIT_"):
                found[target.id] = value.value
    return found


def _dict_int_values(node: ast.AST) -> set[int]:
    """The integer values of a dict literal (annotated or plain)."""
    if not isinstance(node, ast.Dict):
        return set()
    values: set[int] = set()
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            values.add(value.value)
    return values


def _plugin_exit_tables() -> dict[str, set[int]]:
    """Every ``EXIT_CODES``/``_EXIT_CODES`` dict a shipped script declares.

    Discovered by AST so this test cannot go stale the way the assertions it
    guards did: adding a class to ``log-aggregator/lib/envelope.py`` registers
    its code here without editing this file.
    """
    tables: dict[str, set[int]] = {}
    for path in PLUGINS_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            target_names: list[str] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                target_names = [
                    t.id for t in node.targets if isinstance(t, ast.Name)
                ]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name
            ):
                target_names = [node.target.id]
                value = node.value
            if value is None:
                continue
            for name in target_names:
                if name.endswith("EXIT_CODES"):
                    found = _dict_int_values(value)
                    if found:
                        tables[f"{path.relative_to(REPO_ROOT)}:{name}"] = found
    return tables


def _registered_codes() -> set[int]:
    """Union of the shared table and every plugin-owned table."""
    registered = set(codes.EXIT_CODES.values())
    for values in _plugin_exit_tables().values():
        registered |= values
    registered.add(codes.EXIT_OK)
    return registered


# -----------------------------------------------------------------------------
# The registry itself
# -----------------------------------------------------------------------------


def test_live_modules_exist() -> None:
    """A silently empty glob would make every check below vacuous."""
    assert len(_live_module_paths()) >= 15


def test_plugin_tables_are_discovered() -> None:
    """The AST discovery must find the plugin-owned tables, not just ssh-core's.

    ssh-core's table is imported directly (its values are named constants, so it
    is the import that matters); the extra discovery exists for the per-script
    namespaces, and ``log-aggregator``'s is the one a live case asserts (90
    ``anchor_not_found``).
    """
    tables = _plugin_exit_tables()
    assert any("log-aggregator" in key for key in tables), sorted(tables)


def test_exit_code_table_is_the_single_source() -> None:
    """Sanity-check the table itself moved as the tests now assume."""
    expected = {
        "local_path_missing": 160,
        "local_path_is_directory": 161,
        "remote_path_missing": 162,
        "transfer_io": 164,
        "mode_deferred": 166,
        "remote_path_exists": 168,
        "alias_not_found": 4,
        "network": 102,
        "invalid_arguments": 2,
        "timeout": 124,
        "auth": 100,
    }
    actual = {name: codes.exit_code_for(name) for name in expected}
    assert actual == expected
    # And the historical mistake is not what any of those classes map to.
    assert 1 not in {
        codes.exit_code_for("local_path_missing"),
        codes.exit_code_for("remote_path_missing"),
        codes.exit_code_for("alias_not_found"),
        codes.exit_code_for("network"),
    }


# -----------------------------------------------------------------------------
# The live modules
# -----------------------------------------------------------------------------


def test_no_live_module_uses_the_old_generic_failure_constant() -> None:
    """``EXIT_GENERIC_FAILURE`` is the name that caused GAP-002; it stays gone."""
    offenders = [
        f"{path.name}: {name}"
        for path in _live_module_paths()
        for name in _module_exit_assignments(path)
        if name in BANNED_NAMES
    ]
    assert not offenders, (
        "these live modules re-declared a code the product stopped returning:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_live_module_constants_are_registered() -> None:
    """Every ``EXIT_*`` constant a live module declares is a real code.

    That is the "test constant set is a subset of exit_codes" criterion. A
    module may still name its own constant when the value comes from a
    per-script namespace or is a remote command's status; what it may not do is
    invent a number the product never emits.
    """
    registered = _registered_codes()
    offenders: list[str] = []
    for path in _live_module_paths():
        for name, value in _module_exit_assignments(path).items():
            if name in ALLOWED_LITERAL_NAMES or value == codes.EXIT_OK:
                continue
            if value not in registered:
                offenders.append(f"{path.name}: {name} = {value}")
    assert not offenders, (
        "these live-module constants are not codes any shipped script "
        "returns:\n  " + "\n  ".join(sorted(offenders))
    )


def test_asserted_exit_code_literals_are_registered() -> None:
    """No live module may compare an exit code against an invented number.

    Walks every ``<name holding an exit code> == <int>`` comparison and checks
    the literal against the registry. Zero is always allowed (success is 0 in
    every table) and so is any name in :data:`ALLOWED_LITERAL_NAMES`.
    """
    registered = _registered_codes()
    offenders: list[str] = []
    for path in _live_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            left = node.left
            if isinstance(left, ast.Name):
                label = left.id
            elif isinstance(left, ast.Subscript):
                label = ast.unparse(left)
            else:
                continue
            if label in ALLOWED_LITERAL_NAMES:
                continue
            if not any(
                token in label for token in ("exit_code", "returncode", "rc", "code")
            ):
                continue
            for comparator in node.comparators:
                if not (
                    isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, int)
                ):
                    continue
                if comparator.value == codes.EXIT_OK:
                    continue
                if comparator.value in registered:
                    continue
                offenders.append(
                    f"{path.name}:{node.lineno}: {label} == {comparator.value}"
                )
    assert not offenders, (
        "live modules assert exit codes the product never returns "
        "(DEVOPS-GAP-002):\n  " + "\n  ".join(sorted(offenders))
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EXIT_LOCAL_PATH_MISSING", 160),
        ("EXIT_LOCAL_PATH_IS_DIRECTORY", 161),
        ("EXIT_REMOTE_PATH_MISSING", 162),
        ("EXIT_TRANSFER_IO", 164),
        ("EXIT_MODE_UNSUPPORTED", 166),
        ("EXIT_REMOTE_PATH_EXISTS", 168),
        ("EXIT_INVALID_ARGUMENTS", 2),
        ("EXIT_TIMEOUT", 124),
        ("EXIT_NETWORK", 102),
        ("EXIT_AUTH", 100),
        ("EXIT_VERIFY_FAILED", 9),
        ("EXIT_TUNNEL_NOT_FOUND", 126),
        # `alias_not_found` has no own constant: it is a deliberate alias of
        # `not_found` (4) in EXIT_CODES, which is how the live module reads it.
        ("EXIT_CODES:alias_not_found", 4),
    ],
)
def test_named_constants_are_importable(name: str, value: int) -> None:
    """The live modules import these by name; a rename must fail here first."""
    if name.startswith("EXIT_CODES:"):
        assert codes.EXIT_CODES[name.split(":", 1)[1]] == value
    else:
        assert getattr(codes, name) == value
