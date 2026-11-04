"""SSH-01 / SSH-24 / SSH-26: the local route and command splitting."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.helpers import load_script  # noqa: E402

ssh_execute = load_script("ssh_execute")


class LocalRouteMetacharacters(unittest.TestCase):
    """Every shell separator must be refused, not interpreted (SSH-01)."""

    def _refused(self, command: str) -> str | None:
        result = ssh_execute._run_local(command, timeout=5)
        self.assertFalse(result["success"], f"{command!r} should be refused")
        return (result.get("data") or {}).get("refused_metachar")

    def test_single_ampersand_is_refused(self) -> None:
        # The exact payload from the issue: it used to run two commands.
        self.assertEqual(
            self._refused('cmd /c echo benign & copy /y nul proof.txt'), "&"
        )

    def test_compound_operators_are_refused(self) -> None:
        for command, meta in (
            ("echo a && echo b", "&&"),
            ("echo a || echo b", "||"),
            ("echo a | more", "|"),
            ("echo a > out.txt", ">"),
            ("echo a >> out.txt", ">>"),
            ("echo a < in.txt", "<"),
            ("echo a ; echo b", ";"),
            ("echo `whoami`", "`"),
            ("echo $(whoami)", "$("),
            ("cmd /c echo a ^& echo b", "&"),
            ("cmd /c echo %PATH%", "%%"),
            # cmd.exe delayed expansion, only live under `cmd /v:on` — but a
            # caller cannot know whether the parent enabled it, so the pattern
            # is refused like %VAR%.
            ("cmd /v:on /c echo !PATH!", "!!"),
        ):
            with self.subTest(command=command):
                self.assertEqual(self._refused(command), meta)

    def test_a_bare_bang_is_not_refused(self) -> None:
        """`!` is ordinary text unless it wraps a name, so `echo done!` is fine."""
        self.assertIsNone(ssh_execute._refused_metachar("echo done!"))
        self.assertIsNone(ssh_execute._refused_metachar("grep -v '!#' file"))

    def test_shell_wrapper_is_allowed(self) -> None:
        # Not refused: the metacharacters are inside an explicit `bash -c`.
        argv = ssh_execute.split_command('bash -c "echo one && echo two"')
        self.assertTrue(ssh_execute._is_shell_wrapper(argv))

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows path normalisation")
    def test_windows_exe_suffix_is_normalised(self) -> None:
        # SSH-26: Path('bash.exe').name is 'bash.exe', not 'bash'.
        argv = [
            r"C:\Program Files\Git\usr\bin\bash.exe",
            "-c",
            "echo one && echo two",
        ]
        self.assertTrue(ssh_execute._is_shell_wrapper(argv))

    def test_plain_command_passes_the_guard(self) -> None:
        result = ssh_execute._run_local(
            f'"{sys.executable}" -c "print(1)"', timeout=30
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["stdout"].strip(), "1")


class CommandSplitting(unittest.TestCase):
    """SSH-24: commands starting with '-' must be reachable."""

    def setUp(self) -> None:
        self.parser = ssh_execute.build_parser()

    def _split(self, argv: list[str]) -> tuple[list[str], str | None]:
        return ssh_execute._separate_command(self.parser, argv)

    def test_bare_dash_command(self) -> None:
        parse_argv, command = self._split(["lab-240", "-v", "--json"])
        self.assertEqual(command, "-v")
        self.assertIn("--json", parse_argv)

    def test_double_dash_command(self) -> None:
        parse_argv, command = self._split(["lab-240", "--", "-v"])
        self.assertEqual(command, "-v")
        self.assertEqual(parse_argv, ["lab-240"])

    def test_trailing_known_flag_is_lifted_from_command(self) -> None:
        _, command = self._split(["lab-240", "--", "-v"])
        self.assertEqual(command, "-v")
        parse_argv, command = self._split(["lab-240", "--", "-v", "--json"])
        self.assertEqual(command, "-v")
        self.assertIn("--json", parse_argv)
        parse_argv, command = self._split(["lab-240", "--json", "uptime"])
        self.assertEqual(command, "uptime")
        self.assertIn("--json", parse_argv)

    def test_normal_command_is_untouched(self) -> None:
        parse_argv, command = self._split(["local", "echo", "hi", "--json"])
        self.assertEqual(command, "echo hi")
        self.assertEqual(parse_argv, ["local", "--json"])

    def test_options_before_host_are_preserved(self) -> None:
        parse_argv, command = self._split(
            ["--timeout", "5", "lab-240", "--", "-v", "--json"]
        )
        # `-v` is the command; the trailing known flag is still honoured.
        self.assertEqual(command, "-v")
        self.assertEqual(parse_argv[:2], ["--timeout", "5"])
        self.assertIn("--json", parse_argv)


class TargetSugar(unittest.TestCase):
    """SSH-23: user@host:port / [v6]:port."""

    def test_user_host_port(self) -> None:
        self.assertEqual(
            ssh_execute.split_target("root@192.168.30.240:2222"),
            ("root@192.168.30.240", 2222),
        )

    def test_ipv6(self) -> None:
        self.assertEqual(ssh_execute.split_target("[::1]:2222"), ("::1", 2222))

    def test_plain_alias(self) -> None:
        self.assertEqual(ssh_execute.split_target("lab-240"), ("lab-240", None))

    def test_local_is_never_split(self) -> None:
        self.assertEqual(ssh_execute.split_target("local"), ("local", None))


if __name__ == "__main__":
    unittest.main()
