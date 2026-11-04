"""DV-14 / DV-17: sudo wrapping and bounded retry with backoff."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.helpers import load_script  # noqa: E402
from lib import sudo  # noqa: E402
from lib.retry import (  # noqa: E402
    MAX_TOTAL_SLEEP,
    RetryPolicy,
    is_retryable,
    policy_from_args,
    retrying,
)


class SudoWrapping(unittest.TestCase):
    """DV-14: `sudo -n` is what keeps a tty-less exec channel from hanging."""

    def test_default_is_non_interactive_root(self) -> None:
        spec = sudo.parse_sudo("")
        assert spec is not None
        wrapped = sudo.wrap("systemctl status nginx", spec)
        self.assertEqual(wrapped, "sudo -n -u root -- sh -c 'systemctl status nginx'")
        self.assertIn("-n", wrapped.split())
        self.assertNotIn("-S", wrapped.split())

    def test_explicit_user(self) -> None:
        spec = sudo.parse_sudo("deploy")
        assert spec is not None
        self.assertIn("-u deploy", sudo.wrap("id -u", spec))

    def test_no_flag_means_no_sudo(self) -> None:
        self.assertIsNone(sudo.parse_sudo(None))

    def test_command_is_single_quoted_for_the_remote_shell(self) -> None:
        """The wrapped command must survive the remote shell as ONE argument.

        The exact escape spelling is ``shlex.quote``'s business; what matters
        is that a POSIX shell re-parsing the wrapper hands ``sh -c`` the
        original command verbatim, with ``&&`` untouched by the outer shell.
        """
        import shlex

        spec = sudo.parse_sudo("root")
        assert spec is not None
        for command in (
            "echo 'a b' && ls",
            "grep -r foo /var/log/*.log | head -20",
            "systemctl restart nginx",
        ):
            wrapped = sudo.wrap(command, spec)
            tokens = shlex.split(wrapped)
            self.assertEqual(tokens[:6], ["sudo", "-n", "-u", "root", "--", "sh"])
            self.assertEqual(tokens[6], "-c")
            self.assertEqual(tokens[7], command)

    def test_password_mode_uses_dash_s_not_dash_n(self) -> None:
        spec = sudo.SudoSpec(user="root", password_env="SSH_SUDO_PASSWORD")
        wrapped = sudo.wrap("id", spec, use_stdin_password=True)
        self.assertIn("-S", wrapped.split())
        self.assertNotIn("-n", wrapped.split())

    def test_preflight_never_runs_the_real_command(self) -> None:
        spec = sudo.parse_sudo("")
        assert spec is not None
        self.assertEqual(
            sudo.probe_command(spec), "sudo -n -u root -- sh -c true"
        )

    def test_classify_maps_the_documented_messages(self) -> None:
        self.assertEqual(
            sudo.classify("sudo: a password is required"), "sudo_required"
        )
        self.assertEqual(
            sudo.classify("sudo: no tty present and no askpass program specified"),
            "sudo_required",
        )
        self.assertEqual(
            sudo.classify("Sorry, try again.\nsudo: 1 incorrect password attempt"),
            "sudo_auth_failed",
        )
        self.assertIsNone(sudo.classify("bash: nginx: command not found"))

    def test_remediation_mentions_nopasswd_and_the_env_flag(self) -> None:
        spec = sudo.parse_sudo("")
        assert spec is not None
        text = sudo.remediation(spec)
        self.assertIn("NOPASSWD", text)
        self.assertIn("--sudo-password-env", text)

    def test_exit_codes_are_registered(self) -> None:
        from lib.exit_codes import exit_code_for

        self.assertEqual(exit_code_for("sudo_required"), 112)
        self.assertEqual(exit_code_for("sudo_auth_failed"), 113)


class RetryPolicyTests(unittest.TestCase):
    """DV-17: retry only what is transient, and never sleep unbounded."""

    def test_only_network_and_timeout_are_retryable(self) -> None:
        def env(failure_class: str, success: bool = False) -> dict:
            return {"success": success, "data": {"failure_class": failure_class}}

        self.assertTrue(is_retryable(env("network")))
        self.assertTrue(is_retryable(env("timeout")))
        for never in ("auth", "host_key", "kex", "permission", "invalid_arguments"):
            self.assertFalse(is_retryable(env(never)), never)
        self.assertFalse(is_retryable(env("network", success=True)))
        self.assertFalse(is_retryable(None))
        self.assertFalse(is_retryable({"success": False}))

    def test_backoff_grows_and_is_capped(self) -> None:
        policy = RetryPolicy(attempts=5, delay=2.0, backoff=2.0, max_delay=10.0)
        self.assertEqual([policy.sleep_for(n) for n in (1, 2, 3, 4)], [2.0, 4.0, 8.0, 10.0])

    def test_attempts_are_counted_and_history_recorded(self) -> None:
        sleeps: list[float] = []
        calls: list[int] = []

        def call(attempt: int) -> dict:
            calls.append(attempt)
            if attempt < 3:
                return {"success": False, "exit_code": 102,
                        "data": {"failure_class": "network"}}
            return {"success": True, "exit_code": 0, "data": {}}

        result = retrying(
            call, RetryPolicy(attempts=3, delay=1.0, backoff=2.0),
            sleep=sleeps.append,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["attempts"], 3)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(len(result["data"]["retry_history"]), 2)

    def test_auth_failure_is_not_retried(self) -> None:
        calls: list[int] = []

        def call(attempt: int) -> dict:
            calls.append(attempt)
            return {"success": False, "exit_code": 100,
                    "data": {"failure_class": "auth"}}

        result = retrying(call, RetryPolicy(attempts=5), sleep=lambda _s: None)
        self.assertEqual(calls, [1])
        self.assertEqual(result["data"]["attempts"], 1)
        self.assertNotIn("retry_history", result["data"])

    def test_total_sleep_budget_is_enforced(self) -> None:
        sleeps: list[float] = []

        def call(_attempt: int) -> dict:
            return {"success": False, "exit_code": 102,
                    "data": {"failure_class": "network"}}

        policy = RetryPolicy(attempts=100, delay=MAX_TOTAL_SLEEP / 2, backoff=1.0,
                             max_delay=MAX_TOTAL_SLEEP)
        result = retrying(call, policy, sleep=sleeps.append)
        self.assertLessEqual(sum(sleeps), MAX_TOTAL_SLEEP)
        self.assertLess(result["data"]["attempts"], 100)

    def test_policy_from_args_clamps_nonsense(self) -> None:
        from types import SimpleNamespace

        policy = policy_from_args(SimpleNamespace(retry=0, retry_delay=-5, retry_backoff=0.1))
        self.assertEqual(policy.attempts, 1)
        self.assertEqual(policy.delay, 0.0)
        self.assertEqual(policy.backoff, 1.0)
        self.assertFalse(policy.enabled)

    def test_invalid_policy_raises(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(attempts=0)
        with self.assertRaises(ValueError):
            RetryPolicy(backoff=0.5)


class SudoCliSurface(unittest.TestCase):
    """DV-14: the flags exist, are documented, and never touch the local route."""

    def setUp(self) -> None:
        self.module = load_script("ssh_execute")
        self.parser = self.module.build_parser()

    def test_flags_are_registered(self) -> None:
        help_text = self.parser.format_help()
        for flag in ("--sudo", "--sudo-user", "--sudo-password-env",
                     "--sudo-preserve-env", "--follow", "--tail", "--retry"):
            self.assertIn(flag, help_text, flag)

    def test_sudo_is_refused_on_the_local_route(self) -> None:
        rc = self.module.main(["local", "echo hi", "--sudo", "--json"])
        self.assertEqual(rc, 2)

    def test_sudo_spec_reads_the_cli(self) -> None:
        args = self.parser.parse_args(["h", "true", "--sudo", "--sudo-user", "deploy"])
        spec = self.module.parse_sudo_root(args)
        assert spec is not None
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.user, "deploy")

    def test_missing_sudo_password_env_fails_before_connecting(self) -> None:
        # No network is touched: the env check happens first, which is the
        # difference between a fast failure and a hang.
        rc = self.module.main([
            "h", "true", "--json", "--sudo-password-env", "SSH_CORE_NO_SUCH_VAR",
        ])
        self.assertEqual(rc, 112)


if __name__ == "__main__":
    unittest.main()
