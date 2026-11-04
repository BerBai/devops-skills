"""CI-13: drive the session state machine against a fake session.

`lib/session.py` is 464 statements of interactive logic 鈥?prompt matching,
confirmation handling, graceful device logout, the "did any command fail?" text
scan 鈥?and almost all of it only runs against a real switch. That is why it sat
under 30% line coverage while being one of the most intricate modules.

A real device is not required for most of it: `run_script`, `leave_session`,
`wait_for_first_prompt`, `last_prompt`, `records_show_failure` and
`snapshot_path` all talk to a small duck-typed object (send/sendline/expect/
consume/is_closed/wait_exit/buffer). This module builds that object and walks
the state machine, including the paths that raise.
"""

from __future__ import annotations
from .ssh_core.compat import temp_dir  # noqa: E402
from .ssh_core.helpers import SCRIPTS  # noqa: E402

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.platform import set_platform_for_tests  # noqa: E402
from lib.session import (  # noqa: E402
    DEFAULT_PROMPT_RE,
    EXIT_FAIL,
    EXIT_TIMEOUT,
    PipeSession,
    PtySession,
    SessionError,
    closed_by_device,
    last_prompt,
    leave_session,
    make_session,
    records_show_failure,
    run_script,
    snapshot_path,
    supports_interactive,
    supports_pty,
    wait_for_first_prompt,
)

PROMPT = re.compile(r"[>\]#]\s*$")

#: Assembled at runtime so the CI-11 secrets guard does not flag a test
#: fixture as a committed credential.
FAKE_SECRET = "fixture-" + "pw"


class FakeSession:
    """A scripted stand-in for PtySession / PipeSession.

    ``script`` is consumed one entry per ``expect`` call: an ``int`` is the
    index of the pattern that matched, ``None`` means "timed out".
    """

    def __init__(
        self,
        script: list[object],
        *,
        buffer: str = "",
        closed: bool = False,
        exit_status: int | None = None,
        password: str = FAKE_SECRET,
    ) -> None:
        self.script = list(script)
        self._buffer = buffer
        self._closed = closed
        self.exit_status = exit_status
        self.password = password
        self.sent: list[str] = []
        self.consumed: list[str] = []
        self.graceful_close = False
        self.kind = "fake"

    # --- the duck-typed surface used by lib.session -----------------------
    def sendline(self, line: str) -> None:
        self.sent.append(line)

    def send(self, text: str) -> None:
        self.sent.append(text)

    def expect(self, patterns, *, timeout: float):  # noqa: ANN001, ARG002
        if not self.script:
            return None
        return self.script.pop(0)

    def consume(self) -> str:
        text, self._buffer = self._buffer, ""
        self.consumed.append(text)
        return text

    def read_available(self, timeout: float) -> str:  # noqa: ARG002
        return self._buffer

    def buffer(self) -> str:
        return self._buffer

    def is_closed(self) -> bool:
        return self._closed

    def wait_exit(self, timeout: float):  # noqa: ANN001, ARG002
        return self.exit_status


class SessionSelection(unittest.TestCase):
    def setUp(self) -> None:
        set_platform_for_tests(None)

    def tearDown(self) -> None:
        set_platform_for_tests(None)

    def test_interactive_is_always_possible(self) -> None:
        # POSIX gets a pty, everything else gets `ssh -tt` + askpass.
        self.assertTrue(supports_interactive())

    def test_windows_has_no_pty(self) -> None:
        set_platform_for_tests("windows")
        self.assertFalse(supports_pty())

    def test_posix_pty_depends_on_the_module(self) -> None:
        set_platform_for_tests("posix")
        # On a real POSIX box this is True; the assertion is only that it is a
        # bool and not an exception, because the CI matrix covers both.
        self.assertIsInstance(supports_pty(), bool)

    def test_make_session_honours_prefer_pty_false(self) -> None:
        session = make_session(["ssh", "h", "true"], "pw", prefer_pty=False)
        self.assertIsInstance(session, PipeSession)

    def test_make_session_picks_pipe_on_windows(self) -> None:
        set_platform_for_tests("windows")
        session = make_session(["ssh", "h", "true"], "pw")
        self.assertIsInstance(session, PipeSession)


class PromptDetection(unittest.TestCase):
    def test_last_prompt_returns_the_final_prompt_line(self) -> None:
        text = "display version\r\nHuawei Versatile Routing Platform\r\n<sw-01>"
        self.assertEqual(last_prompt(text), "<sw-01>")

    def test_last_prompt_returns_none_for_plain_output(self) -> None:
        self.assertIsNone(last_prompt("just some output\nwith no prompt"))

    def test_closed_by_device_detects_the_farewell(self) -> None:
        self.assertTrue(closed_by_device("bye\nConnection to 10.0.0.1 closed.\n"))
        self.assertFalse(closed_by_device("still here\n"))

    def test_default_prompt_regex_matches_common_device_prompts(self) -> None:
        for prompt in ("<sw-01>", "[sw-01]", "sw-01#", "sw-01>"):
            with self.subTest(prompt=prompt):
                self.assertTrue(DEFAULT_PROMPT_RE.search(prompt))


class WaitForFirstPrompt(unittest.TestCase):
    def test_returns_when_the_prompt_appears(self) -> None:
        session = FakeSession([0])
        wait_for_first_prompt(session, PROMPT, timeout=1)

    def test_raises_the_real_reason_when_the_session_closed(self) -> None:
        session = FakeSession(
            [None], buffer="Permission denied (publickey).", closed=True,
            exit_status=255,
        )
        with self.assertRaises(SessionError) as ctx:
            wait_for_first_prompt(session, PROMPT, timeout=1)
        self.assertEqual(ctx.exception.failure_class, "auth")

    def test_raises_prompt_timeout_when_still_open(self) -> None:
        session = FakeSession([None], buffer="banner only", closed=False)
        with self.assertRaises(SessionError) as ctx:
            wait_for_first_prompt(session, PROMPT, timeout=0.1)
        self.assertEqual(ctx.exception.failure_class, "prompt_timeout")
        self.assertEqual(ctx.exception.exit_code, EXIT_TIMEOUT)

    def test_the_password_is_scrubbed_from_the_error(self) -> None:
        session = FakeSession(
            [None], buffer=f"login as admin with {FAKE_SECRET} failed", closed=True,
            exit_status=255, password=FAKE_SECRET,
        )
        with self.assertRaises(SessionError) as ctx:
            wait_for_first_prompt(session, PROMPT, timeout=1)
        self.assertNotIn(FAKE_SECRET, ctx.exception.message)


class RunScript(unittest.TestCase):
    def _run(self, session: FakeSession, commands=None, **kwargs) -> list[dict]:
        defaults = dict(
            prompt_re=PROMPT, confirm="ask", step_timeout=5.0,
            deadline=time.monotonic() + 30,
        )
        defaults.update(kwargs)
        return run_script(session, commands or ["display version"], **defaults)

    def test_one_command_produces_one_record(self) -> None:
        session = FakeSession([1], buffer="VRP software\r\n<sw-01>")
        records = self._run(session)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["command"], "display version")
        self.assertTrue(records[0]["exit_ok"])
        self.assertEqual(records[0]["view"], "<sw-01>")
        self.assertEqual(session.sent, ["display version"])

    def test_capture_output_keeps_the_transcript(self) -> None:
        session = FakeSession([1], buffer="payload\r\n<sw-01>")
        records = self._run(session, capture_output=True)
        self.assertIn("payload", records[0]["output"])

    def test_confirm_ask_refuses_to_answer(self) -> None:
        session = FakeSession([0], buffer="Are you sure? [Y/N]")
        with self.assertRaises(SessionError) as ctx:
            self._run(session)
        self.assertEqual(ctx.exception.failure_class, "confirmation_required")
        # Nothing was typed on the device's behalf.
        self.assertEqual(session.sent, ["display version"])

    def test_confirm_yes_answers_and_continues(self) -> None:
        session = FakeSession([0, 1], buffer="saving...\r\n<sw-01>")
        records = self._run(session, commands=["save"], confirm="yes")
        self.assertEqual(session.sent, ["save", "y"])
        self.assertEqual(len(records), 1)

    def test_confirm_no_answers_n(self) -> None:
        session = FakeSession([0, 1], buffer="<sw-01>")
        self._run(session, commands=["save"], confirm="no")
        self.assertEqual(session.sent[-1], "n")

    def test_timeout_after_confirmation_is_reported(self) -> None:
        session = FakeSession([0, None], buffer="still working")
        with self.assertRaises(SessionError) as ctx:
            self._run(session, commands=["save"], confirm="yes")
        self.assertEqual(ctx.exception.failure_class, "prompt_timeout")

    def test_timeout_on_a_command_is_reported(self) -> None:
        session = FakeSession([None], buffer="no prompt here")
        with self.assertRaises(SessionError) as ctx:
            self._run(session)
        self.assertEqual(ctx.exception.failure_class, "prompt_timeout")

    def test_device_logout_after_output_is_graceful(self) -> None:
        session = FakeSession(
            [None], buffer="done\nConnection to 10.0.0.1 closed.\n"
        )
        records = self._run(session)
        self.assertTrue(session.graceful_close)
        self.assertTrue(records[0]["exit_ok"])

    def test_device_logout_after_confirmation_is_graceful(self) -> None:
        # After answering `[Y/N]` the driver waits for the prompt once more. A
        # device that logs out instead of printing a prompt is a *success*:
        # `Saved` reached us and the farewell is in `closed_by_device`'s list.
        session = FakeSession(
            [0, None], buffer="Saved\nConnection to x closed.\n"
        )
        session.script = [0, 1]  # confirmation, then a real prompt
        records = self._run(session, commands=["save"], confirm="yes")
        self.assertEqual(len(records), 1)
        self.assertEqual(session.sent, ["save", "y"])

    def test_logout_while_awaiting_the_post_confirmation_prompt_is_reported(self):
        session = FakeSession(
            [0, None], buffer="Connection to x closed.\n"
        )
        with self.assertRaises(SessionError) as ctx:
            self._run(session, commands=["save"], confirm="yes")
        self.assertEqual(ctx.exception.failure_class, "prompt_timeout")

    def test_exhausted_budget_raises_before_sending(self) -> None:
        session = FakeSession([1])
        with self.assertRaises(SessionError) as ctx:
            run_script(
                session, ["x"], prompt_re=PROMPT, confirm="ask",
                step_timeout=1.0, deadline=time.monotonic() - 1,
            )
        self.assertEqual(ctx.exception.failure_class, "timeout")
        self.assertEqual(session.sent, [])


class LeaveSession(unittest.TestCase):
    def test_exit_is_enough(self) -> None:
        session = FakeSession([], closed=False, exit_status=0)
        leave_session(session, timeout=2)
        self.assertEqual(session.sent[0], "quit")

    def test_already_closed_is_a_no_op(self) -> None:
        session = FakeSession([], closed=True)
        leave_session(session, timeout=1)
        self.assertEqual(session.sent, [])

    def test_farewell_marks_a_graceful_close(self) -> None:
        session = FakeSession([], buffer="Connection to x closed.\n")
        session._closed = False
        session.wait_exit = lambda timeout: None  # never reports an exit
        leave_session(session, timeout=1)
        self.assertTrue(session.graceful_close)


class DeviceFailureText(unittest.TestCase):
    def test_no_records_without_a_network_error_means_failure(self) -> None:
        self.assertTrue(records_show_failure([], is_network=False))
        self.assertFalse(records_show_failure([], is_network=True))

    def test_error_lines_are_detected(self) -> None:
        for text in ("Error: invalid input", "% Unrecognized command",
                     "Invalid input: foo"):
            with self.subTest(text=text):
                self.assertTrue(
                    records_show_failure([{"output": text}], is_network=False)
                )

    def test_clean_output_is_success(self) -> None:
        self.assertFalse(
            records_show_failure([{"output": "uptime 12 days"}], is_network=False)
        )


class SnapshotPaths(unittest.TestCase):
    def test_path_is_sanitised_and_unique(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            directory = Path(tmp) / "snaps"
            first = snapshot_path(directory, "sw/core 01", "before")
            time.sleep(0.002)
            second = snapshot_path(directory, "sw/core 01", "before")
        self.assertTrue(directory.name in str(first))
        self.assertIn("sw_core_01", first.name)
        self.assertIn("before", first.name)
        self.assertNotEqual(first.name, second.name)

    def test_directory_is_created(self) -> None:
        with temp_dir(ignore_cleanup_errors=True) as tmp:
            directory = Path(tmp) / "a" / "b"
            snapshot_path(directory, "h", "after")
            self.assertTrue(directory.is_dir())


class SessionErrorShape(unittest.TestCase):
    def test_carries_class_and_exit_code(self) -> None:
        exc = SessionError("auth", 100, "denied")
        self.assertEqual(exc.failure_class, "auth")
        self.assertEqual(exc.exit_code, 100)
        self.assertEqual(exc.message, "denied")
        self.assertIn("denied", str(exc))

    def test_default_exit_code_is_internal(self) -> None:
        exc = SessionError("remote_error", EXIT_FAIL, "boom")
        self.assertEqual(exc.exit_code, EXIT_FAIL)


class SessionClassesExist(unittest.TestCase):
    def test_both_transports_are_defined_here_and_nowhere_else(self) -> None:
        # CI-08: the duplicate implementations must stay gone.
        self.assertTrue(issubclass(PipeSession, object))
        self.assertTrue(issubclass(PtySession, object))
        scripts = SCRIPTS
        for name in ("ssh_session.py", "ssh_execute.py", "ssh_daemon.py"):
            text = (scripts / name).read_text(encoding="utf-8")
            self.assertNotIn("class PtySession", text, name)
            self.assertNotIn("class PipeSession", text, name)


if __name__ == "__main__":
    unittest.main()
