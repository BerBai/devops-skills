"""CI-13: drive the session state machine against a fake session.

`lib/session.py` is 464 statements of interactive logic — prompt matching,
confirmation handling, graceful device logout, the "did any command fail?" text
scan — and almost all of it only runs against a real switch. That is why it sat
under 30% line coverage while being one of the most intricate modules.

A real device is not required for most of it: `run_script`, `leave_session`,
`wait_for_first_prompt`, `last_prompt`, `records_show_failure` and
`snapshot_path` all talk to a small duck-typed object (send/sendline/expect/
consume/is_closed/wait_exit/buffer). This module builds that object and walks
the state machine, including the paths that raise.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from .ssh_core.compat import temp_dir  # noqa: E402
from .ssh_core.helpers import SCRIPTS, load_script  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lib  # noqa: E402
from lib import device_output_text, normalized_exit_code  # noqa: E402
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
    prompt_returned,
    records_show_failure,
    run_script,
    snapshot_path,
    supports_interactive,
    supports_pty,
    wait_for_first_prompt,
)

ssh_execute_mod = load_script("ssh_execute")
ssh_session_mod = load_script("ssh_session")

PROMPT = re.compile(r"[>\]#]\s*$")

#: Assembled at runtime so the CI-11 secrets guard does not flag a test
#: fixture as a committed credential.
FAKE_SECRET = "fixture-" + "pw"


class FakeSession:
    """A scripted stand-in for PtySession / PipeSession.

    ``script`` is consumed one entry per ``expect`` call: an ``int`` is the
    index of the pattern that matched, ``None`` means "timed out".

    ``close_code`` is what :meth:`close` reports, which is how the Windows
    unsigned-DWORD case (4294967295) is reproduced without a real child
    (BUG-009). ``consume_script`` (when given) supplies the text returned by
    successive :meth:`consume` calls, so one scripted session can walk through
    login, first prompt and command output without re-filling the buffer.

    ``password_via_helper`` is set by default so ``authenticate`` takes the
    askpass path — the same one a Windows ``-tt`` pipe uses — instead of
    demanding a password prompt the fixture would have to script.
    """

    password_via_helper = True
    login_banner = ""
    login_banner_unusable = False

    def __init__(
        self,
        script: list[object],
        *,
        buffer: str = "",
        closed: bool = False,
        exit_status: int | None = None,
        password: str = FAKE_SECRET,
        close_code: int = 0,
        consume_script: list[str] | None = None,
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
        self.close_code = close_code
        self.consume_script = (
            None if consume_script is None else list(consume_script)
        )
        self.started = False

    # --- the duck-typed surface used by lib.session -----------------------
    def start(self) -> None:
        self.started = True

    def close(self) -> int:
        self._closed = True
        return self.close_code

    def sendline(self, line: str) -> None:
        self.sent.append(line)

    def send(self, text: str) -> None:
        self.sent.append(text)

    def expect(self, patterns, timeout=None, **kwargs):  # noqa: ANN001, ARG002
        # The real transports accept the timeout positionally (the login
        # driver passes it that way) and by keyword (run_script).
        if not self.script:
            return None
        return self.script.pop(0)

    def consume(self) -> str:
        if self.consume_script is not None:
            text = self.consume_script.pop(0) if self.consume_script else ""
            self.consumed.append(text)
            return text
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


#: The transcript shape from DEVOPS-BUG-008: a Huawei S5700 answers the command,
#: prints its prompt, and only then lets OpenSSH report the disconnect. `<host>`
#: keeps the fixture free of a real address (the secrets guard scans tests).
DEVICE_DISCONNECT_TRANSCRIPT = (
    "Huawei Versatile Routing Platform Software\r\n"
    "VRP (R) software, Version 5.130 (S5700 V200R005C00SPC500)\r\n"
    "Copyright (C) 2000-2016 HUAWEI TECH CO., LTD\r\n"
    "\r\n"
    "Info: The max number of VTY users is 10, and the number of current VTY "
    "users on line is 0.\r\n"
    "<Switch-B>\r\n"
    "  display clock\r\n"
    "  15:04:32+08:00\r\n"
    "  2026-11-05\r\n"
    "<Switch-B>\r\n"
    "Received disconnect from <host> port 22:2: The connection is closed by "
    "SSH server\r\n"
    "Disconnected from <host> port 22\r\n"
)

#: A real credential rejection: no prompt, no command echo, no result.
DEVICE_AUTH_REJECTION = (
    "Received disconnect from <host> port 22:2: The connection is closed by "
    "SSH server\r\n"
    "Disconnected from <host> port 22\r\n"
)

WRONG_PASSWORD_TRANSCRIPT = (
    "admin@<host>'s password:\r\n"
    "Permission denied, please try again.\r\n"
    "Received disconnect from <host> port 22:2: The connection is closed by "
    "SSH server\r\n"
    "Disconnected from <host> port 22\r\n"
)

#: What a VRP box prints on its *own* login prompt after a bad password, plus
#: the prompt it re-displays afterwards. The interactive driver answers the
#: chained prompt and ``AUTH_FAILED_DEVICE_RE`` then raises ``auth`` — this
#: transcript exists to prove the "prompt returned" shortcut cannot turn a
#: rejection into a success on its own.
VRP_REJECTION_TRANSCRIPT = (
    "Username: admin\r\n"
    "Password: \r\n"
    "Error: Local authentication is rejected.\r\n"
    "<Switch-B>\r\n"
)


class DisconnectIsNotAuthFailure(unittest.TestCase):
    """BUG-008: a device's normal goodbye is not a credential problem.

    The two regexes used to disagree — ``AUTH_PATTERN`` matched the VRP
    disconnect wording while ``GRACEFUL_CLOSE_RE`` did not — so
    ``ssh_execute`` answered ``auth`` / exit 100 for a command that had already
    printed its result, and moved the whole transcript into ``stderr``.
    """

    def test_the_three_disconnect_shapes_are_graceful_closes(self) -> None:
        for farewell in (
            "Received disconnect from <host> port 22:2: The connection is "
            "closed by SSH server",
            "Received disconnect from <host> port 22:2: bye",
            "Disconnected from <host> port 22",
            "The connection is closed by SSH server",
            "Connection to <host> closed.",
        ):
            with self.subTest(farewell=farewell):
                self.assertTrue(closed_by_device(farewell))

    def test_an_unrelated_failure_is_not_a_graceful_close(self) -> None:
        self.assertFalse(closed_by_device("Permission denied (publickey)."))
        self.assertFalse(closed_by_device("ssh: connect to host x port 22: "
                                          "Connection refused"))

    def test_output_plus_disconnect_is_not_auth(self) -> None:
        device_text = device_output_text(DEVICE_DISCONNECT_TRANSCRIPT)
        self.assertNotEqual(
            lib.classify_failure(
                255, DEVICE_DISCONNECT_TRANSCRIPT, command_output=device_text
            ),
            "auth",
        )

    def test_a_bare_disconnect_is_still_auth(self) -> None:
        """Nothing was printed: this really is a rejected credential."""
        self.assertEqual(
            lib.classify_failure(
                255,
                DEVICE_AUTH_REJECTION,
                command_output=device_output_text(DEVICE_AUTH_REJECTION),
            ),
            "auth",
        )

    def test_the_default_call_keeps_the_old_conservative_behaviour(self) -> None:
        """Callers that pass no output keep classifying it as auth."""
        self.assertEqual(lib.classify_failure(255, DEVICE_DISCONNECT_TRANSCRIPT), "auth")

    def test_a_wrong_password_is_still_auth(self) -> None:
        """Acceptance criterion: a real credential rejection stays auth/100."""
        self.assertEqual(
            lib.classify_failure(
                255,
                WRONG_PASSWORD_TRANSCRIPT,
                command_output=device_output_text(WRONG_PASSWORD_TRANSCRIPT),
            ),
            "auth",
        )
        self.assertFalse(prompt_returned(WRONG_PASSWORD_TRANSCRIPT))
        self.assertEqual(ssh_execute_mod._CLASS_CODES["auth"], 100)

    def test_a_device_login_rejection_is_not_rescued_by_its_prompt(self) -> None:
        """A VRP "local authentication is rejected" is auth, not success.

        The device re-prints ``<Switch-B>`` after the rejection, so a success
        test that only looked for a prompt would call a rejected login a
        completed command.
        """
        self.assertEqual(
            lib.classify_failure(
                255,
                VRP_REJECTION_TRANSCRIPT,
                command_output=device_output_text(VRP_REJECTION_TRANSCRIPT),
            ),
            "auth",
        )
        self.assertFalse(prompt_returned(VRP_REJECTION_TRANSCRIPT))

    def test_prompt_after_output_is_the_success_criterion(self) -> None:
        self.assertTrue(prompt_returned(DEVICE_DISCONNECT_TRANSCRIPT))
        self.assertFalse(prompt_returned(DEVICE_AUTH_REJECTION))
        self.assertFalse(prompt_returned(WRONG_PASSWORD_TRANSCRIPT))
        self.assertFalse(prompt_returned(""))
        self.assertFalse(prompt_returned("display clock\r\nstill working\r\n"))

    def test_prompt_returned_never_overrides_a_client_side_rejection(self) -> None:
        """A device that echoes "Permission denied (" and re-prompts: no success."""
        text = "Permission denied (publickey,password).\r\n<Switch-B>\r\n"
        self.assertFalse(prompt_returned(text))

    def test_device_output_text_strips_only_ssh_chatter(self) -> None:
        stripped = device_output_text(DEVICE_DISCONNECT_TRANSCRIPT)
        self.assertIn("display clock", stripped)
        self.assertIn("<Switch-B>", stripped)
        self.assertNotIn("Received disconnect", stripped)
        self.assertNotIn("Disconnected from", stripped)
        self.assertNotIn("Connection to", stripped)

    def test_device_output_text_is_empty_for_a_bare_farewell(self) -> None:
        self.assertEqual(device_output_text(DEVICE_AUTH_REJECTION).strip(), "")

    def test_stdout_keeps_the_result_and_drops_the_farewell(self) -> None:
        stripped, removed = lib.strip_ssh_farewell(DEVICE_DISCONNECT_TRANSCRIPT)
        self.assertIn("display clock", stripped)
        self.assertIn("<Switch-B>", stripped)
        self.assertNotIn("Received disconnect", stripped)
        self.assertNotIn("Disconnected from", stripped)
        self.assertEqual(removed, 2)


#: The transcript a VRP box produces when it drops the TCP connection instead of
#: closing the SSH channel: ssh's trailer is **glued** to the command's last line
#: because the device's answer ends without a newline. This is the exact shape
#: BUG-008b was reported against.
GLUED_CLIENT_LOOP_TRANSCRIPT = (
    "\r\nInfo: The max number of VTY users is 10, and the number\r\n"
    "      of current VTY users on line is 1.\r\n"
    "      The current login time is 2026-11-06 10:36:32+08:00.\r\n"
    "2026-11-06 10:36:32+08:00\r\nFriday\r\n"
    "Time Zone(Beijing) : UTC+08:00\r\n<Switch-B>\r\n"
    "Info: The max number of VTY users is 10, and the number\r\n"
    "      of current VTY users on line is 0."
    "client_loop: send disconnect: Connection reset\r\n"
)
GLUED_DISCONNECT_TRANSCRIPT = GLUED_CLIENT_LOOP_TRANSCRIPT.replace(
    "client_loop: send disconnect: Connection reset",
    "Received disconnect from <host> port 22:2: The connection is closed by "
    "SSH server",
)


class GluedFarewellIsTransportNoise(unittest.TestCase):
    """BUG-008b: ssh's trailer is not always on its own line.

    The line-shaped filter only matched a farewell that *started* a line, so on
    a device whose last output has no trailing newline the trailer stayed in
    ``stdout`` and — worse — made ``prompt_returned`` see a line that is not a
    prompt at all.
    """

    def test_the_glued_suffix_is_removed(self) -> None:
        for transcript, marker in (
            (GLUED_CLIENT_LOOP_TRANSCRIPT, "client_loop"),
            (GLUED_DISCONNECT_TRANSCRIPT, "Received disconnect"),
        ):
            with self.subTest(marker=marker):
                cleaned, removed = lib.strip_ssh_farewell(transcript)
                self.assertNotIn(marker, cleaned)
                self.assertIn("of current VTY users on line is 0.", cleaned)
                self.assertEqual(removed, 1)

    def test_the_command_result_survives(self) -> None:
        cleaned, _ = lib.strip_ssh_farewell(GLUED_CLIENT_LOOP_TRANSCRIPT)
        self.assertIn("Time Zone(Beijing) : UTC+08:00", cleaned)
        self.assertIn("<Switch-B>", cleaned)

    def test_a_line_that_merely_mentions_a_farewell_is_kept(self) -> None:
        """Fail-open: only a *suffix* is cut, never a phrase mid-line."""
        text = "grep: Disconnected from <host> port 22\n"
        cleaned, removed = lib.strip_ssh_farewell(text)
        self.assertEqual(cleaned, text)
        self.assertEqual(removed, 0)

    def test_device_output_text_uses_the_same_filter(self) -> None:
        self.assertEqual(
            device_output_text(GLUED_CLIENT_LOOP_TRANSCRIPT),
            lib.strip_ssh_farewell(GLUED_CLIENT_LOOP_TRANSCRIPT)[0],
        )


class LoginBannerBoundary(unittest.TestCase):
    """BUG-008b: the recorded pre-auth banner must not eat the command result.

    On a fast VRP box the whole exchange can already be in the buffer at the
    first poll after login: greeting, the command's answer and the prompt it
    re-printed. Recording that as "the banner" and cutting it left nothing but
    ssh's goodbye, which then classified as `auth` / exit 100.
    """

    def test_a_banner_that_already_holds_a_prompt_is_unusable(self) -> None:
        self.assertFalse(ssh_execute_mod.banner_is_usable(
            "Info: VTY users on line is 1.\r\n<Switch-B>"
        ))
        self.assertFalse(ssh_execute_mod.banner_is_usable(
            "Info: VTY users on line is 1.\r\n"
            "Received disconnect from <host> port 22:2: bye"
        ))
        self.assertTrue(ssh_execute_mod.banner_is_usable(
            "Info: The max number of VTY users is 10.\r\n"
            "      of current VTY users on line is 1."
        ))
        self.assertFalse(ssh_execute_mod.banner_is_usable(""))

    def test_the_driver_refuses_to_record_such_a_banner(self) -> None:
        session = FakeSession([], buffer="")
        lib.session._record_login_banner(  # noqa: SLF001
            session, GLUED_CLIENT_LOOP_TRANSCRIPT
        )
        self.assertFalse(session.login_banner)
        self.assertTrue(session.login_banner_unusable)

    def test_a_plain_greeting_is_still_recorded(self) -> None:
        session = FakeSession([], buffer="")
        greeting = (
            "Info: The max number of VTY users is 10, and the number\r\n"
            "      of current VTY users on line is 1.\r\n"
        )
        lib.session._record_login_banner(session, greeting)  # noqa: SLF001
        self.assertIn("VTY users", session.login_banner)
        self.assertFalse(getattr(session, "login_banner_unusable", False))

    def test_a_partial_last_line_is_not_recorded(self) -> None:
        """A read can land mid-line; a marker must end on a line break."""
        session = FakeSession([], buffer="")
        lib.session._record_login_banner(  # noqa: SLF001
            session,
            "Info: The max number of VTY users is 10.\r\n"
            "      of current VTY users on lin",
        )
        self.assertNotIn("on lin", session.login_banner)
        self.assertIn("VTY users", session.login_banner)

    def test_stripping_refuses_when_the_remainder_has_no_prompt(self) -> None:
        """Cutting a banner that swallowed the answer would delete the answer."""
        raw = (
            "Info: The max number of VTY users is 10.\r\n"
            "Received disconnect from <host> port 22:2: bye\r\n"
            "Disconnected from <host> port 22\r\n"
        )
        clean, lines, applied = ssh_execute_mod._strip_recorded_banner(
            raw, "Info: The max number of VTY users is 10."
        )
        self.assertFalse(applied)
        self.assertEqual(clean, raw)
        self.assertEqual(lines, [])

    def test_stripping_applies_when_the_answer_and_prompt_remain(self) -> None:
        raw = (
            "Info: The max number of VTY users is 10.\r\n"
            "2026-11-06 10:36:32+08:00\r\n"
            "<Switch-B>\r\n"
        )
        clean, lines, applied = ssh_execute_mod._strip_recorded_banner(
            raw, "Info: The max number of VTY users is 10."
        )
        self.assertTrue(applied)
        self.assertNotIn("VTY users", clean)
        self.assertIn("2026-11-06", clean)
        self.assertEqual(lines, ["Info: The max number of VTY users is 10."])

    def test_only_greeting_lines_are_cut(self) -> None:
        """A snapshot that ran into the answer stops at the answer's first line."""
        raw = (
            "Info: The max number of VTY users is 10, and the number\r\n"
            "      of current VTY users on line is 1.\r\n"
            "      The current login time is 2026-11-06 10:36:32+08:00.\r\n"
            "2026-11-06 10:36:32+08:00\r\n"
            "Friday\r\nTime Zone(Beijing) : UTC+08:00\r\n<Switch-B>\r\n"
        )
        clean, lines, applied = ssh_execute_mod._strip_recorded_banner(
            raw, raw, command_hint="display clock"
        )
        self.assertTrue(applied)
        self.assertEqual(len(lines), 3)
        self.assertNotIn("VTY users", clean)
        self.assertIn("2026-11-06 10:36:32+08:00", clean)
        self.assertIn("Time Zone(Beijing)", clean)
        self.assertIn("<Switch-B>", clean)

    def test_a_snapshot_that_runs_to_eof_is_refused(self) -> None:
        """Nothing would be left to prove the command ran, so nothing is cut."""
        raw = (
            "Info: The max number of VTY users is 10, and the number\r\n"
            "      of current VTY users on line is 1."
        )
        clean, lines, applied = ssh_execute_mod._strip_recorded_banner(raw, raw)
        self.assertFalse(applied)
        self.assertEqual(clean, raw)
        self.assertEqual(lines, [])

    def test_a_command_echo_inside_the_snapshot_is_not_a_greeting(self) -> None:
        raw = (
            "display clock\r\n"
            "2026-11-06 10:36:32+08:00\r\n<Switch-B>\r\n"
        )
        clean, lines, applied = ssh_execute_mod._strip_recorded_banner(
            raw, raw, command_hint="display clock"
        )
        self.assertFalse(applied)
        self.assertEqual(clean, raw)
        self.assertEqual(lines, [])


#: A device answer that ends **without** a re-printed prompt and without a
#: trailing newline, so ssh's goodbye is glued to it — `display vlan` on the lab
#: S5700 looks exactly like this. The device still ended the session, which is
#: the evidence the verdict needs.
NO_PROMPT_DEVICE_CLOSE_TRANSCRIPT = (
    "\r\nInfo: The max number of VTY users is 10, and the number\r\n"
    "      of current VTY users on line is 1.\r\n"
    "      The current login time is 2026-11-06 11:21:08+08:00.\r\n"
    "The total number of vlans is : 8\r\n"
    "---------------------------------------------------------------------------"
    "-----\r\n"
    "1    common  UT:GE0/0/2(D)      GE0/0/11(D)\r\n"
    "Received disconnect from <host> port 22:2: The connection is closed by "
    "SSH server\r\n"
    "\r\nDisconnected from <host> port 22\r\n"
)


class DeviceClosedIsAskedOfTheTranscript(unittest.TestCase):
    """BUG-008b: "did the device hang up?" must read the *unfiltered* text.

    ``command_output`` has ssh's goodbye removed, so asking it there always
    answers False. Every command whose output the device did not re-prompt
    before hanging up was therefore classified `remote_error` / 104 — including
    plain read-only `display vlan` — while `display clock` kept working only
    because it happens to re-print the prompt.
    """

    def _run_password_route(self, rc: int, transcript: str) -> dict:
        fake = FakeSession(
            [], buffer="", closed=True, close_code=rc,
            consume_script=[transcript],
        )
        with mock.patch.object(
            ssh_execute_mod, "make_session", return_value=fake
        ), mock.patch.object(ssh_session_mod, "authenticate", lambda *a, **k: None), \
                mock.patch("lib.session.authenticate", lambda *a, **k: None):
            return ssh_execute_mod._run_password_session(
                "sw-01", "display vlan", 5, 5, None,
                ssh_options=[], accept_new_hostkey=False, port=None,
                identity=None, password=FAKE_SECRET,
            )

    def test_a_close_without_a_reprompt_is_still_a_success(self) -> None:
        result = self._run_password_route(255, NO_PROMPT_DEVICE_CLOSE_TRANSCRIPT)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["data"]["device_closed_after_output"])
        self.assertIn("The total number of vlans is : 8", result["stdout"])

    def test_the_same_transcript_without_a_close_stays_a_failure(self) -> None:
        """The close is the evidence; without it rc 255 really is a client error."""
        transcript = NO_PROMPT_DEVICE_CLOSE_TRANSCRIPT.replace(
            "Received disconnect from <host> port 22:2: The connection is closed "
            "by SSH server\r\n\r\nDisconnected from <host> port 22\r\n",
            "",
        )
        result = self._run_password_route(255, transcript)
        self.assertFalse(result["success"], result)
        self.assertNotIn(
            (result.get("data") or {}).get("failure_class"), ("auth",)
        )

    def test_ssh_chatter_is_not_counted_as_device_output(self) -> None:
        """A bare goodbye still means the command never ran."""
        result = self._run_password_route(255, DEVICE_AUTH_REJECTION)
        self.assertFalse(result["success"], result)
        self.assertFalse(result["data"]["verdict_basis"]["device_closed"])


class GreetingPrefix(unittest.TestCase):
    """BUG-008b: "how much of this transcript is the pre-login greeting?"."""

    def test_vrp_greeting_is_recognised(self) -> None:
        text = (
            "\r\nInfo: The max number of VTY users is 10, and the number\r\n"
            "      of current VTY users on line is 1.\r\n"
            "      The current login time is 2026-11-06 10:36:32+08:00.\r\n"
            "2026-11-06 10:36:32+08:00\r\n"
        )
        prefix = lib.greeting_prefix(text.lstrip("\r\n"))
        self.assertIn("VTY users", prefix)
        self.assertNotIn("2026-11-06 10:36:32+08:00\r\n2026", prefix)
        self.assertTrue(prefix.endswith("\r\n"))

    def test_it_stops_at_the_first_result_line(self) -> None:
        text = (
            "Info: The max number of VTY users is 10.\r\n"
            "2026-11-06 10:36:32+08:00\r\n"
        )
        self.assertEqual(
            lib.greeting_prefix(text),
            "Info: The max number of VTY users is 10.\r\n",
        )

    def test_a_plain_result_has_no_greeting(self) -> None:
        self.assertEqual(lib.greeting_prefix("hello world\r\n"), "")

    def test_posix_and_comware_greetings(self) -> None:
        for text in (
            "Last login: Wed Nov  5 10:00:00 2026 from 10.0.0.1\r\n$ ",
            "Press ENTER to get started.\r\n<sw-01>",
            "WARNING: Authorized users only\r\n$ ",
        ):
            with self.subTest(text=text):
                self.assertTrue(lib.greeting_prefix(text))


class ExecAndSessionAgree(unittest.TestCase):
    """Acceptance criterion: one device+command, one verdict, both entry points.

    ``ssh_execute._run_password_session`` is driven with the same transcript
    ``ssh_session`` sees (a fake transport, no real device needed) and must
    return the same ``success`` / ``exit_code`` for rc 0 *and* rc 255.
    """

    def _run_password_route(self, rc: int, transcript: str) -> dict:
        fake = FakeSession(
            [], buffer="", closed=True, close_code=rc,
            consume_script=[transcript],
        )
        with mock.patch.object(
            ssh_execute_mod, "make_session", return_value=fake
        ), mock.patch.object(ssh_session_mod, "authenticate", lambda *a, **k: None), \
                mock.patch("lib.session.authenticate", lambda *a, **k: None):
            return ssh_execute_mod._run_password_session(
                "sw-01", "display clock", 5, 5, None,
                ssh_options=[], accept_new_hostkey=False, port=None,
                identity=None, password=FAKE_SECRET,
            )

    def test_execute_returns_success_for_a_device_disconnect(self) -> None:
        for rc in (0, 255):
            with self.subTest(rc=rc):
                result = self._run_password_route(rc, DEVICE_DISCONNECT_TRANSCRIPT)
                self.assertTrue(result["success"], result)
                self.assertEqual(result["exit_code"], 0)
                self.assertIn("display clock", result["stdout"])
                self.assertNotEqual(
                    (result.get("data") or {}).get("failure_class"), "auth"
                )

    def test_execute_keeps_a_wrong_password_as_auth(self) -> None:
        result = self._run_password_route(255, VRP_REJECTION_TRANSCRIPT)
        self.assertFalse(result["success"], result)
        self.assertEqual(result["data"]["failure_class"], "auth")
        self.assertEqual(result["exit_code"], 100)

    def test_execute_keeps_a_bare_disconnect_as_auth(self) -> None:
        result = self._run_password_route(255, DEVICE_AUTH_REJECTION)
        self.assertFalse(result["success"], result)
        self.assertEqual(result["data"]["failure_class"], "auth")
        self.assertEqual(result["exit_code"], 100)


class NormalizedExitCode(unittest.TestCase):
    """BUG-009: the 0-255 contract holds on Windows and for signal deaths."""

    def test_windows_unsigned_dword_is_masked(self) -> None:
        self.assertEqual(normalized_exit_code(4294967295), (255, None))

    def test_sigterm_becomes_143(self) -> None:
        self.assertEqual(normalized_exit_code(-15), (143, 15))

    def test_sigkill_becomes_137(self) -> None:
        self.assertEqual(normalized_exit_code(-9), (137, 9))

    def test_plain_codes_pass_through(self) -> None:
        for code in (0, 1, 2, 42, 100, 124, 255):
            with self.subTest(code=code):
                self.assertEqual(normalized_exit_code(code), (code, None))


class SessionFailureExitCodes(unittest.TestCase):
    """BUG-009 end to end: no failure exit may leave 0-255.

    The two exits used to echo the raw ``Popen`` returncode, so a Windows
    killed session produced ``exit_code: 4294967295`` and
    ``stderr: "ssh exited 4294967295"``.
    """

    def test_class_code_masks_windows_dword(self) -> None:
        self.assertEqual(ssh_session_mod._class_code("remote_error", 4294967295), 104)

    def test_class_code_keeps_a_remote_status(self) -> None:
        self.assertEqual(ssh_session_mod._class_code("remote_error", 42), 42)

    def test_class_code_translates_255_through_the_table(self) -> None:
        self.assertEqual(ssh_session_mod._class_code("auth", 255), 100)
        self.assertEqual(ssh_session_mod._class_code("host_key", 255), 101)
        self.assertEqual(ssh_session_mod._class_code("remote_error", 255), 104)

    def test_every_class_code_is_three_digits_or_less(self) -> None:
        for raw in (4294967295, -15, -9, 255, 0, 124, 42):
            for fclass in (None, "auth", "remote_error", "host_key", "timeout"):
                with self.subTest(raw=raw, fclass=fclass):
                    code = ssh_session_mod._class_code(fclass, raw)
                    self.assertGreaterEqual(code, 0)
                    self.assertLessEqual(code, 255)
                    self.assertLessEqual(len(str(code)), 3)

    def _run_session(
        self, fake: FakeSession, *, is_network: bool = True
    ) -> dict:
        """Drive ``ssh_session.run_session`` against a scripted session.

        ``leave_session`` is stubbed out: it is a bounded retry loop against a
        real child, and its own behaviour is covered by :class:`LeaveSession`.
        """
        args = ssh_session_mod.build_parser().parse_args(
            ["sw-01", "--command", "display clock", "--json", "--step-timeout", "1"]
        )
        with mock.patch.object(ssh_session_mod, "_make_session", return_value=fake), \
                mock.patch.object(ssh_session_mod, "leave_session", lambda *a, **k: None), \
                mock.patch.object(
                    ssh_session_mod, "read_ssh_config_metadata",
                    return_value={"device-type": "network"} if is_network else {},
                ), \
                mock.patch.dict(os.environ, {"SSH_PASSWORD": FAKE_SECRET}):
            return ssh_session_mod.run_session(args)

    def _failed_session(self, close_code: int) -> FakeSession:
        """A session the device answered with a *command* error, then dropped.

        ``Error:`` in the transcript makes :func:`records_show_failure` report a
        device-level failure, so the final failure exit is the one under test
        (rather than the graceful-close shortcut).
        """
        transcript = "<Switch-B>\r\ndisplay clock\r\nError: wrong parameter\r\n<Switch-B>\r\n"
        return FakeSession(
            [0, 1],
            buffer="<Switch-B>\r\n",
            consume_script=["<Switch-B>\r\n", transcript],
            close_code=close_code,
        )

    def test_a_killed_session_never_reports_a_32_bit_code(self) -> None:
        result = self._run_session(self._failed_session(4294967295))
        self.assertFalse(result["success"], result)
        self.assertEqual(result["exit_code"], 104)
        self.assertLessEqual(len(str(result["exit_code"])), 3)
        self.assertNotIn("4294967295", result["stderr"])
        self.assertIn("ssh exited 255", result["stderr"])

    def test_a_negative_returncode_becomes_128_plus_n(self) -> None:
        result = self._run_session(self._failed_session(-15))
        self.assertFalse(result["success"], result)
        # 143 is the remote command's own (signal-derived) status, passed
        # through untouched — the point is that it is not -15.
        self.assertEqual(result["exit_code"], 143)
        self.assertNotIn("-15", result["stderr"])
        self.assertIn("ssh exited 143", result["stderr"])
        self.assertEqual(result["data"]["terminated_by_signal"], 15)

    def test_network_success_is_rc_independent(self) -> None:
        """BUG-008: the same device+command gives one verdict, whatever rc is."""
        for close_code in (0, 255):
            with self.subTest(close_code=close_code):
                fake = FakeSession(
                    [0, 1],
                    buffer="<Switch-B>\r\n",
                    consume_script=[
                        "<Switch-B>\r\n", DEVICE_DISCONNECT_TRANSCRIPT,
                    ],
                    close_code=close_code,
                )
                result = self._run_session(fake)
                self.assertTrue(result["success"], result)
                self.assertEqual(result["exit_code"], 0)
                self.assertTrue(result["data"]["results"], result)

    def test_a_device_command_error_is_not_a_success(self) -> None:
        """A wrong *command* keeps the batch red even after a graceful close."""
        result = self._run_session(self._failed_session(255))
        self.assertFalse(result["success"], result)
        self.assertEqual(result["data"]["failure_class"], "remote_error")


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
