#!/usr/bin/env python3
"""gen_cli_reference.py - generate the ssh-core CLI reference from argparse.

Hand-maintained option tables rot: a flag is renamed, a default changes, and the
prose keeps advertising the old behaviour. This generator reads the real
``build_parser()`` of every public ssh-core script plus
``scripts/lib/exit_codes.py`` and renders one reference page:

    plugins/ssh-core/skills/ssh-core/references/cli-reference.md

Usage::

    python scripts/gen_cli_reference.py --plugin ssh-core --write
    python scripts/gen_cli_reference.py --plugin ssh-core --check
    python scripts/gen_cli_reference.py --check --all

``--check`` is the CI gate: it fails when the committed page differs from what
the parsers produce, and (for every plugin) when a documented
``scripts/<name>.py`` does not exist. For ssh-core it additionally checks every
``--flag`` that appears on a documented command line against that script's real
argparse options.

The other seven plugins deliberately have no generated tables yet: their
``--check`` is the inventory/flag placeholder described above, so this script
does not become an eight-plugin table generator by accident.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import importlib.util
import io
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable, Sequence

# This generator is a dispatcher: the branch count and return count are the
# surface it documents. Percent formatting keeps the generated text readable
# and 3.8-compatible.
# ruff: noqa: PLR0911, PLR0912, PLR2004, PLW2901, UP031

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_ROOT = REPO_ROOT / "plugins"
SSH_CORE_SCRIPTS = PLUGINS_ROOT / "ssh-core" / "skills" / "ssh-core" / "scripts"
SSH_CORE_SKILL = PLUGINS_ROOT / "ssh-core" / "skills" / "ssh-core" / "SKILL.md"
OUTPUT = (
    PLUGINS_ROOT
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "references"
    / "cli-reference.md"
)
PLUGIN_NAMES = (
    "ssh-core",
    "ssh-guarded",
    "remote-debug",
    "k8s-debug",
    "docker-quick",
    "log-aggregator",
    "iac-state",
    "cicd-debug",
)

_SCRIPT_REF_RE = re.compile(r"(?:scripts[/\\])?([A-Za-z_][\w-]*\.py)\b")
_FLAG_RE = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
#: A line that actually invokes a script: `python x.py ...`, `scripts/x.py ...`,
#: or a code-block line that starts with `x.py ...`. Prose backticks such as
#: "`doctor.py` probes ..." are deliberately not commands.
_COMMAND_LINE_RE = re.compile(
    r"^(?:\$\s+)?(?:python3?\s+)?(?:[\w./\\-]+[/\\])?"
    r"(?P<script>[A-Za-z_][\w-]*\.py)\b(?!`)"
)


class ScriptParser:
    """The argparse surface of one public script."""

    def __init__(self, path: Path, parser: argparse.ArgumentParser) -> None:
        self.path = path
        self.parser = parser
        self.positionals = [
            a
            for a in parser._actions
            if not a.option_strings and not isinstance(a, argparse._HelpAction)
        ]
        self.options = [a for a in parser._actions if a.option_strings]
        self.option_names: set[str] = set()
        for action in parser._actions:
            self._collect(action)

    def _collect(self, action: argparse.Action) -> None:
        if action.option_strings:
            self.option_names.update(action.option_strings)
        subparsers = getattr(action, "choices", None)
        if isinstance(subparsers, dict):
            for subparser in subparsers.values():
                for child in subparser._actions:
                    self._collect(child)

    @property
    def usage(self) -> str:
        text = self.parser.format_usage().strip()
        return re.sub(r"^usage:\s*", "", text)

    @property
    def summary(self) -> str:
        description = self.parser.description or ""
        for line in description.splitlines():
            line = line.strip()
            if line:
                return line
        return ""


def _public_scripts(plugin: str) -> list[Path]:
    scripts = PLUGINS_ROOT / plugin / "skills" / plugin / "scripts"
    return sorted(
        path
        for path in scripts.glob("*.py")
        if path.name != "__init__.py" and not path.name.startswith("_")
    )


def _load_module(path: Path) -> ModuleType:
    name = f"_gen_{path.stem}_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def _capture_parser(module: ModuleType) -> argparse.ArgumentParser:
    """Return build_parser(), or capture the parser main() constructs."""
    builder = getattr(module, "build_parser", None)
    if callable(builder):
        return builder()

    parser_class = getattr(module, "SSHArgumentParser", argparse.ArgumentParser)
    created: list[argparse.ArgumentParser] = []

    class Recording(parser_class):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    module.SSHArgumentParser = Recording  # type: ignore[attr-defined]
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            module.main(["--help"])  # type: ignore[attr-defined]
    except SystemExit:
        pass
    finally:
        if hasattr(module, "SSHArgumentParser"):
            module.SSHArgumentParser = parser_class  # type: ignore[attr-defined]
    if not created:
        raise RuntimeError("no ArgumentParser was constructed by main()")
    return created[0]


def parsers_for(plugin: str) -> list[ScriptParser]:
    result: list[ScriptParser] = []
    for script in _public_scripts(plugin):
        module = _load_module(script)
        result.append(ScriptParser(script, _capture_parser(module)))
    return result


def _escape(value: object) -> str:
    text = str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if text else "-"


def _action_label(action: argparse.Action) -> str:
    if action.option_strings:
        return ", ".join(f"`{flag}`" for flag in action.option_strings)
    return "`%s`" % (action.metavar or action.dest)


def _action_default(action: argparse.Action) -> str:
    if action.default in (None, argparse.SUPPRESS, ""):
        return "-"
    if isinstance(action.default, bool):
        return "true" if action.default else "false"
    return _escape(action.default)


def _action_choices(action: argparse.Action) -> str:
    if not action.choices:
        return "-"
    return ", ".join(f"`{choice}`" for choice in action.choices)


def _render_options(parsers: Sequence[ScriptParser]) -> list[str]:
    lines: list[str] = []
    for script in parsers:
        lines.append(f"## `{script.path.name}`")
        lines.append("")
        if script.summary:
            lines.append(script.summary)
            lines.append("")
        lines.append("```text")
        lines.append(script.usage)
        lines.append("```")
        lines.append("")
        if script.positionals:
            lines.append("| Positional | Required | Help |")
            lines.append("|---|---|---|")
            for action in script.positionals:
                required = "yes" if action.required else "no"
                lines.append(
                    "| {} | {} | {} |".format(
                        _action_label(action),
                        required,
                        _escape(action.help or "-"),
                    )
                )
            lines.append("")
        lines.append("| Option | Default | Choices | Help |")
        lines.append("|---|---|---|---|")
        for action in script.options:
            lines.append(
                "| {} | {} | {} | {} |".format(
                    _action_label(action),
                    _action_default(action),
                    _action_choices(action),
                    _escape(action.help or "-"),
                )
            )
        lines.append("")
    return lines


def _exit_tables(scripts_dir: Path) -> tuple[list[str], list[str]]:
    module = _load_module(scripts_dir / "lib" / "exit_codes.py")
    codes: dict[str, int] = dict(module.EXIT_CODES)
    aliases: dict[str, str] = dict(getattr(module, "ALIASES", {}))
    constants: dict[int, list[str]] = {}
    for name in dir(module):
        if not name.startswith("EXIT_"):
            continue
        value = getattr(module, name)
        if isinstance(value, int):
            constants.setdefault(value, []).append(name)

    by_code: dict[int, list[str]] = {}
    for failure_class, code in codes.items():
        by_code.setdefault(code, []).append(failure_class)

    table = ["| Code | failure_class | Constant |", "|---|---|---|"]
    for code in sorted(by_code):
        table.append(
            "| %d | %s | %s |"
            % (
                code,
                ", ".join(f"`{name}`" for name in sorted(by_code[code])),
                ", ".join(f"`{name}`" for name in constants.get(code, [])),
            )
        )
    table.append("")

    alias_table = ["| Alias | Canonical class |", "|---|---|"]
    for name in sorted(aliases):
        alias_table.append(f"| `{name}` | `{aliases[name]}` |")
    alias_table.append("")
    return table, alias_table


def render(plugin: str) -> str:
    if plugin != "ssh-core":
        raise SystemExit("only ssh-core has a generated reference; use --check")
    parsers = parsers_for(plugin)
    exit_table, alias_table = _exit_tables(SSH_CORE_SCRIPTS)

    lines: list[str] = [
        "# ssh-core CLI reference",
        "",
        "<!-- Generated by `python scripts/gen_cli_reference.py --plugin ssh-core --write`.",
        "     Do not edit by hand: run the generator and commit the result. -->",
        "",
        "This page is generated from the scripts' own `argparse` parsers and from",
        "`scripts/lib/exit_codes.py`. It is the machine-checkable half of the CLI",
        "contract; `SKILL.md` keeps the triage text and points here for the full",
        "option and failure-class tables.",
        "",
        "## Commands",
        "",
        "| Script | Purpose | Positionals | Options |",
        "|---|---|---|---|",
    ]
    for script in parsers:
        lines.append(
            "| `%s` | %s | %d | %d |"
            % (
                script.path.name,
                _escape(script.summary or "-"),
                len(script.positionals),
                len(script.options),
            )
        )
    lines.extend(["", "## Options", ""])
    lines.extend(_render_options(parsers))
    lines.extend(
        [
            "## Exit codes",
            "",
            "`EXIT_CODES` is the only failure_class -> exit_code mapping. A",
            "script must call `exit_code_for()` instead of hardcoding a number.",
            "`255` is OpenSSH's own client-failure code and is passed through",
            "unchanged; it is not part of this table.",
            "",
        ]
    )
    lines.extend(exit_table)
    lines.extend(
        [
            "## Failure-class aliases",
            "",
            "Aliases share a code because a script had already shipped that",
            "spelling. New code must use the canonical class.",
            "",
        ]
    )
    lines.extend(alias_table)
    return "\n".join(lines).rstrip() + "\n"


def _flags_outside_quotes(text: str) -> Iterable[str]:
    """Yield long options that are *not* inside a quoted remote command."""
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char == "#":
            return
        if text.startswith("--", index):
            match = re.match(r"--[A-Za-z][\w-]*", text[index:])
            if match:
                flag = match.group(0)
                if flag == "--":
                    return
                yield flag
                index += len(flag)
                continue
        index += 1


def _command_invocations(lines: Sequence[str]) -> Iterable[tuple[int, str, str]]:
    """Yield (line number, script, remaining command text) for real invocations."""
    fence: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        start = index + 1
        joined = line
        while joined.rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            joined = joined.rstrip()[:-1] + " " + lines[index]
        index += 1

        fence_match = _FENCE_RE.match(joined)
        if fence is None and fence_match:
            fence = fence_match.group(1)[:1]
            continue
        if fence is not None and fence_match and fence_match.group(1).startswith(fence):
            fence = None
            continue
        if joined.lstrip().startswith("#"):
            continue

        stripped = joined.strip()
        candidates = re.findall(r"`([^`\n]+)`", stripped) if "`" in stripped else [stripped]
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate.startswith("$ "):
                candidate = candidate[2:].strip()
            match = _COMMAND_LINE_RE.match(candidate)
            if not match:
                match = re.search(
                    r"(?:python3?\s+|run\.(?:cmd|sh)\s+)"
                    r"(?:[\w./\\-]+[/\\])?(?P<script>[A-Za-z_][\w-]*\.py)\b",
                    candidate,
                )
            if not match:
                continue
            script = match.group("script")
            rest = candidate[match.end() :]
            if script.endswith((".cmd", ".sh")):
                nested = re.search(r"([A-Za-z_][\w-]*\.py)\b", rest)
                if not nested:
                    continue
                script = nested.group(1)
                rest = rest[nested.end() :]
            yield start, script, rest


def _documented_flags(text: str) -> Iterable[tuple[int, str, str]]:
    """Yield (line, script, flag) for flags on documented script commands."""
    for line, script, rest in _command_invocations(text.splitlines()):
        for flag in _flags_outside_quotes(rest):
            yield line, script, flag


def check_plugin(plugin: str, verbose: bool = False) -> list[str]:
    problems: list[str] = []
    skill = PLUGINS_ROOT / plugin / "skills" / plugin / "SKILL.md"
    if not skill.is_file():
        return [f"{plugin}: missing SKILL.md"]
    docs = [skill]
    references = PLUGINS_ROOT / plugin / "skills" / plugin / "references"
    docs.extend(
        path
        for path in sorted(references.glob("*.md"))
        if path.name != "cli-reference.md"
    )
    available = {path.name: path for path in _public_scripts(plugin)}
    helper_dir = PLUGINS_ROOT / plugin / "skills" / plugin / "scripts" / "lib"
    helpers = {path.name for path in helper_dir.glob("*.py")} if helper_dir.is_dir() else set()
    parser_cache: dict[str, ScriptParser] = {}
    for document in docs:
        text = document.read_text(encoding="utf-8")
        relative = document.relative_to(REPO_ROOT)
        for line, script, flag in _documented_flags(text):
            if script not in available:
                if script in helpers or script.startswith("_"):
                    continue
                problems.append(
                    "%s:%d: documented script does not exist: %s"
                    % (relative, line, script)
                )
                continue
            if plugin == "ssh-core":
                if script not in parser_cache:
                    parser_cache[script] = next(
                        item for item in parsers_for(plugin) if item.path.name == script
                    )
                parser = parser_cache[script]
                if flag not in parser.option_names:
                    problems.append(
                        "%s:%d: %s has no option %s"
                        % (relative, line, script, flag)
                    )
    if verbose and not problems:
        print("%s: inventory OK (%d scripts, %d documents)" % (plugin, len(available), len(docs)))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin", choices=PLUGIN_NAMES, default="ssh-core")
    parser.add_argument(
        "--all",
        action="store_true",
        help="--check every plugin's documented CLI inventory",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite the reference page")
    mode.add_argument("--check", action="store_true", help="fail when the page is stale")
    mode.add_argument(
        "--examples",
        action="store_true",
        help="run the whitelisted doc examples and annotate every skipped one",
    )
    parser.add_argument("--list", action="store_true", help="with --examples, print every command")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.examples:
        return check_examples(list_all=args.list)

    if args.write:
        if args.all or args.plugin != "ssh-core":
            parser.error("--write is only implemented for --plugin ssh-core")
        OUTPUT.write_text(render("ssh-core"), encoding="utf-8")
        print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
        return 0

    problems: list[str] = []
    plugins = PLUGIN_NAMES if args.all else (args.plugin,)
    for plugin in plugins:
        problems.extend(check_plugin(plugin, verbose=args.verbose))
    if "ssh-core" in plugins:
        expected = render("ssh-core")
        if not OUTPUT.is_file():
            problems.append(f"missing generated reference: {OUTPUT.relative_to(REPO_ROOT)}")
        else:
            actual = OUTPUT.read_text(encoding="utf-8")
            if actual != expected:
                diff = "".join(
                    difflib.unified_diff(
                        actual.splitlines(True),
                        expected.splitlines(True),
                        fromfile=str(OUTPUT.relative_to(REPO_ROOT)),
                        tofile="generated",
                        n=2,
                    )
                )
                problems.append(
                    "generated reference is stale; run "
                    "`python scripts/gen_cli_reference.py --write`:\n" + diff[:4000]
                )
        if not problems:
            print("ssh-core CLI reference: OK (%d bytes)" % len(expected))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print("%d CLI-reference problem(s)" % len(problems), file=sys.stderr)
        return 1
    if args.all:
        print("CLI inventory: OK (8 plugins, ssh-core reference current)")
    return 0


# ---------------------------------------------------------------------------
# Example whitelist
# ---------------------------------------------------------------------------

_EXAMPLE_DOCS = (
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING.zh-CN.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "docs/**/*.md",
    "plugins/*/skills/*/SKILL.md",
    "plugins/*/skills/*/references/*.md",
)

_MUTATING = re.compile(
    r"--(?:delete|overwrite|apply|execute|promote-auth|yes|resume)\b|"
    r"\b(?:restart|save|create|update|delete|deploy|start|stop|kill|"
    r"upload|download|rm|mv)\b",
    re.IGNORECASE,
)
_FOREIGN_HOST = re.compile(
    r"<(?:alias|host|cmd|command|repo|run-id|namespace|pod|release|project|"
    r"module-path|image|container|target-os|ports|provider|scope|run-id)>|"
    r"\b(?:prod|web|db|bastion|lab|sw|old|legacy|builder|backup-host|"
    r"db-host|dns-1|cache-1|payment-svc|worker|api)-?\d*\b",
    re.IGNORECASE,
)


def _example_docs() -> list[Path]:
    files: list[Path] = []
    for pattern in _EXAMPLE_DOCS:
        if any(ch in pattern for ch in "*?["):
            files.extend(path for path in REPO_ROOT.glob(pattern) if path.is_file())
        else:
            candidate = REPO_ROOT / pattern
            if candidate.is_file():
                files.append(candidate)
    return sorted(set(files))


def _resolve_example_argv(command: str) -> list[str] | None:
    """Turn a documented command into an argv list, or None when unsupported."""
    text = command.strip()
    if text.startswith("$ "):
        text = text[2:].strip()
    if text.startswith("scripts\\") or "scripts\\" in text:
        return None  # Windows launcher; POSIX CI cannot execute it
    if re.search(r"[|<>]|&&|;", text):
        return None  # shell pipeline/redirect; not a single argv
    try:
        argv = shlex.split(text, posix=True, comments=True)
    except ValueError:
        return None
    if not argv:
        return None
    # Drop a trailing shell comment.
    argv = [token for token in argv if token != "#"]
    if not argv:
        return None
    if argv[0] in ("python", "python3"):
        argv[0] = sys.executable
        if len(argv) > 1 and any(sep in argv[1] for sep in ("/", "\\")):
            candidate = (REPO_ROOT / argv[1]).resolve()
            if candidate.is_file():
                argv[1] = str(candidate)
            else:
                found = _find_script(Path(argv[1]).name)
                if found is None:
                    return None
                argv[1] = str(found)
        elif len(argv) > 1:
            found = _find_script(argv[1])
            if found is None:
                return None
            argv[1] = str(found)
    elif argv[0].endswith(".py"):
        found = _find_script(Path(argv[0]).name)
        if found is None:
            return None
        argv = [sys.executable, str(found)] + argv[1:]
    elif argv[0].endswith((".sh", ".cmd")):
        candidate = (REPO_ROOT / argv[0]).resolve()
        if not candidate.is_file():
            return None
        argv[0] = str(candidate)
    else:
        return None
    return argv


def _find_script(name: str) -> Path | None:
    matches = sorted(
        path
        for path in PLUGINS_ROOT.glob(f"*/skills/*/scripts/{name}")
        if path.is_file()
    )
    return matches[0] if matches else None


def _classify_example(command: str, script: str) -> tuple[str, str]:
    """Return (RUN|SKIP, reason)."""
    lowered = command.lower()
    try:
        tokens = shlex.split(command, posix=True, comments=True)
    except ValueError:
        tokens = command.split()
    help_flags = {"--help", "-h", "--version", "-V"}
    if (
        len(tokens) >= 2
        and tokens[0].endswith(".py")
        and all(token.startswith("-") for token in tokens[1:])
        and any(token in help_flags for token in tokens[1:])
    ):
        return "RUN", "help/version is local and side-effect free"
    if script == "selfcheck.py" and "--write-matrix" not in lowered:
        return "RUN", "local dependency probe, no host"
    if script == "doctor.py" and "--host" not in lowered and "--require" not in lowered:
        return "RUN", "local capability probe, no host"
    if _MUTATING.search(lowered):
        return "SKIP", "mutating or state-changing command"
    if _FOREIGN_HOST.search(lowered):
        return "SKIP", "needs a host/credential that is not this machine"
    plugin = _plugin_for_script(script)
    if plugin in ("k8s-debug", "docker-quick", "iac-state", "cicd-debug"):
        return "SKIP", "needs a cluster, container runtime or cloud credentials"
    return "SKIP", "needs a live host/state not available in CI"


def _plugin_for_script(script: str) -> str:
    found = _find_script(script)
    if found is None:
        return ""
    parts = found.relative_to(PLUGINS_ROOT).parts
    return parts[0] if parts else ""


def check_examples(list_all: bool = False) -> int:
    run = 0
    skipped = 0
    failures: list[str] = []
    seen: set[tuple[str, str]] = set()
    for document in _example_docs():
        if document.name == "cli-reference.md":
            continue
        text = document.read_text(encoding="utf-8")
        for line, script, rest in _command_invocations(text.splitlines()):
            command = f"{script} {rest.strip()}"
            key = (str(document), command)
            if key in seen:
                continue
            seen.add(key)
            action, reason = _classify_example(command, script)
            if action == "RUN":
                argv = _resolve_example_argv(command)
                if argv is None:
                    skipped += 1
                    if list_all:
                        print("SKIP %s:%d  %s  [unresolvable command]" % (document.relative_to(REPO_ROOT), line, command))
                    continue
                result = subprocess.run(
                    argv,
                    check=False,
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                run += 1
                if list_all:
                    print("RUN  %s:%d  %s  [exit %d]" % (document.relative_to(REPO_ROOT), line, command, result.returncode))
                if result.returncode != 0:
                    failures.append(
                        "%s:%d: expected exit 0, got %d: %s"
                        % (document.relative_to(REPO_ROOT), line, result.returncode, command)
                    )
            else:
                skipped += 1
                if list_all:
                    print("SKIP %s:%d  %s  [%s]" % (document.relative_to(REPO_ROOT), line, command, reason))
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        return 1
    print(
        "examples: %d run (all exit 0), %d skipped with a reason"
        % (run, skipped)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
