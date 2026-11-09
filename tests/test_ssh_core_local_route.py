"""SSH-01 / SSH-24 / SSH-26 / GAP-008: the local route and command splitting."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

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


class WindowsEscapeHatches(unittest.TestCase):
    """GAP-008: `cmd /c ...` / `powershell -Command ...` are recognised.

    They already worked; they were neither documented nor tested, so the refusal
    message kept recommending `bash`, which a stock Windows box does not have.
    Recognition is deliberately *not* a bypass: an inline wrapper carrying a
    metacharacter is still refused (SSH-01), and only an explicit `--shell`
    declaration opts out of that guard.
    """

    def test_shell_invocation_names_the_inline_wrapper(self) -> None:
        self.assertEqual(
            ssh_execute._shell_invocation(["cmd", "/c", "echo", "hello"]), "cmd"
        )
        self.assertEqual(
            ssh_execute._shell_invocation([r"C:\Windows\System32\CMD.EXE", "/c", "x"]),
            "cmd",
        )
        self.assertEqual(
            ssh_execute._shell_invocation(["powershell", "-Command", "Get-Date"]),
            "powershell",
        )
        self.assertEqual(
            ssh_execute._shell_invocation(["pwsh", "-command", "Get-Date"]), "pwsh"
        )
        self.assertEqual(
            ssh_execute._shell_invocation(["bash", "-c", "echo one"]), "bash"
        )

    def test_shell_invocation_ignores_plain_commands(self) -> None:
        for argv in ([], ["echo", "hi"], ["cmd"], ["cmd", "/k", "x"],
                     ["powershell"], ["python", "-c", "print(1)"]):
            with self.subTest(argv=argv):
                self.assertIsNone(ssh_execute._shell_invocation(argv))

    def test_inline_cmd_with_a_metacharacter_is_still_refused(self) -> None:
        result = ssh_execute._run_local("cmd /c echo a & del b", timeout=5)
        self.assertFalse(result["success"], result)
        self.assertEqual(result["data"]["refused_metachar"], "&")

    def test_declared_shell_is_the_documented_opt_out(self) -> None:
        argv = ssh_execute._local_shell_argv("echo one && echo two", "sh")
        self.assertEqual(argv, ["sh", "-c", "echo one && echo two"])
        self.assertEqual(
            ssh_execute._local_shell_argv("echo hi", "cmd"), ["cmd", "/c", "echo hi"]
        )
        self.assertEqual(
            ssh_execute._local_shell_argv("Write-Output hi", "powershell")[1:],
            ["-Command", "Write-Output hi"],
        )

    def test_shell_choice_is_rejected_away_from_the_local_route(self) -> None:
        captured: dict = {}

        def fake_emit(result, **_kwargs):  # noqa: ANN001
            captured.update(result)

        with mock.patch.object(ssh_execute, "emit", fake_emit):
            rc = ssh_execute.main(
                ["example-host", "true", "--shell", "cmd", "--json"]
            )
        self.assertEqual(rc, 2)
        self.assertFalse(captured["success"])
        self.assertEqual(captured["data"]["failure_class"], "invalid_arguments")
        self.assertEqual(captured["data"]["unsupported_option"], "--shell")

    def test_default_shell_is_none(self) -> None:
        args = ssh_execute.build_parser().parse_args(["local", "echo hi"])
        self.assertEqual(args.shell, "none")

    def test_windows_only_shells_are_refused_on_posix(self) -> None:
        from lib.platform import set_platform_for_tests

        set_platform_for_tests("posix")
        try:
            for shell in ("cmd", "powershell"):
                with self.subTest(shell=shell):
                    result = ssh_execute._run_local(
                        "echo hi", timeout=5, shell=shell
                    )
                    self.assertFalse(result["success"], result)
                    self.assertEqual(
                        result["data"]["failure_class"], "invalid_arguments"
                    )
        finally:
            set_platform_for_tests(None)


class MissingBinaryHint(unittest.TestCase):
    """GAP-008 acceptance criterion: the error names the command + platform."""

    def test_posix_hint_names_the_binary_and_a_way_forward(self) -> None:
        hint = ssh_execute._missing_binary_hint(
            ["no-such-binary-xyz", "--flag"],
            FileNotFoundError(2, "No such file or directory"),
            windows=False,
        )
        self.assertIn("no-such-binary-xyz", hint)
        self.assertIn("PATH", hint)
        self.assertIn("sh -c", hint)

    def test_windows_cmd_builtin_hint_offers_cmd_c(self) -> None:
        hint = ssh_execute._missing_binary_hint(
            ["echo", "hello"], FileNotFoundError(2, "not found"), windows=True
        )
        self.assertIn("'echo'", hint)
        self.assertIn("cmd.exe builtin", hint)
        self.assertIn("cmd /c echo hello", hint)
        self.assertIn("--shell cmd", hint)

    def test_windows_powershell_cmdlet_hint_offers_powershell(self) -> None:
        hint = ssh_execute._missing_binary_hint(
            ["Get-Process"], FileNotFoundError(2, "not found"), windows=True
        )
        self.assertIn("PowerShell cmdlet", hint)
        self.assertIn("powershell -Command Get-Process", hint)

    def test_windows_unknown_binary_hint_still_offers_both_shells(self) -> None:
        hint = ssh_execute._missing_binary_hint(
            ["frobnicate", "-x"], FileNotFoundError(2, "not found"), windows=True
        )
        self.assertIn("frobnicate", hint)
        self.assertIn("cmd /c frobnicate -x", hint)
        self.assertIn("powershell -Command frobnicate -x", hint)

    def test_a_missing_declared_shell_names_the_flag(self) -> None:
        """`--shell sh` on a box without `sh` must not suggest wrapping it."""
        hint = ssh_execute._missing_binary_hint(
            ["sh", "-c", "echo hi"], FileNotFoundError(2, "not found"),
            windows=True, declared_shell="sh",
        )
        self.assertIn("--shell sh", hint)
        self.assertIn("--shell cmd", hint)
        self.assertNotIn('cmd /c "sh -c', hint)

    def test_hint_quotes_a_token_that_contains_a_double_quote(self) -> None:
        hint = ssh_execute._missing_binary_hint(
            ["bash", "-c", 'echo "hi there"'],
            FileNotFoundError(2, "not found"), windows=True,
        )
        self.assertIn("""'echo "hi there"'""", hint)
        self.assertIn("cmd /c bash -c", hint)

    def test_missing_cmd_builtin_envelope_is_actionable(self) -> None:
        """The end-to-end shape: no bare WinError, no bare "command not found"."""
        result = ssh_execute._run_local("echo hello", timeout=5)
        if result["success"]:
            self.skipTest("'echo' is an executable on this platform")
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("echo", result["stderr"])
        self.assertIn("cmd /c echo hello", result["stderr"])
        self.assertNotEqual(result["stderr"].strip(), "command not found: "
                            "[WinError 2] 系统找不到指定的文件。")
        self.assertEqual(result["data"]["missing_binary"], "echo")


class LocalShellOptionEndToEnd(unittest.TestCase):
    """`--shell` is wired to the local route (GAP-008 option 4)."""

    def test_shell_flag_reaches_run_local(self) -> None:
        seen: dict = {}

        def fake_run_local(command, timeout, **kwargs):  # noqa: ANN001
            seen["command"] = command
            seen["shell"] = kwargs.get("shell")
            return ssh_execute.json_result(True, 0, "ok", data={"route": "local"})

        with mock.patch.object(ssh_execute, "_run_local", fake_run_local), \
                mock.patch.object(ssh_execute, "emit", lambda *a, **k: None):
            rc = ssh_execute.main(["local", "echo hi", "--shell", "sh", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["command"], "echo hi")
        self.assertEqual(seen["shell"], "sh")


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
