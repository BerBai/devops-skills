"""DV-18: `--follow` streams live output and still ends in one valid envelope."""

from __future__ import annotations

import io
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from .ssh_core.helpers import load_script  # noqa: E402


class _FakeStdout:
    """A text stream that yields the given lines and then EOF."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._lock = threading.Lock()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        with self._lock:
            if not self._lines:
                raise StopIteration
            return self._lines.pop(0)

    def read(self, _size: int = -1) -> str:
        try:
            return self.__next__()
        except StopIteration:
            return ""

    def close(self) -> None:  # pragma: no cover - called by the relay thread
        pass


class _FakeProc:
    """Minimal ``subprocess.Popen`` stand-in for the follow path.

    ``stay_running=True`` models the real case: stdout has produced a line but
    the child has not exited, so the follow loop keeps polling instead of
    returning on the first iteration.
    """

    def __init__(
        self, lines: list[str], returncode: int = 0, *, stay_running: bool = False
    ) -> None:
        self.stdout = _FakeStdout(lines)
        self._returncode = returncode
        self._stay_running = stay_running
        self.killed = False

    def poll(self):
        # Report "still running" until the relay has drained every line, then
        # the real return code. That mirrors a command that exits on its own.
        if self._stay_running and not self.killed:
            return None
        if self.stdout._lines:
            return None
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self.stdout._lines = []


def _args(**overrides):
    module = load_script("ssh_execute")
    parser = module.build_parser()
    base = parser.parse_args(["h", "tail -f /tmp/x", "--follow", "--json"])
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class FollowStreamsOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script("ssh_execute")
        self._real_popen = self.module.subprocess.Popen

    def tearDown(self) -> None:
        self.module.subprocess.Popen = self._real_popen  # type: ignore[assignment]

    def _install(
        self, lines: list[str], returncode: int = 0, *, stay_running: bool = False
    ) -> _FakeProc:
        proc = _FakeProc(lines, returncode=returncode, stay_running=stay_running)
        self.module.subprocess.Popen = lambda *a, **k: proc  # type: ignore[assignment]
        return proc

    def _run(self, args, **kwargs):
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            result = self.module._run_follow("h", "tail -f /tmp/x", args, None)
        return result, buffer.getvalue()

    def test_live_lines_reach_stderr_and_the_envelope_stays_parseable(self) -> None:
        self._install(["line-1\n", "line-2\n"])
        result, live = self._run(_args(json=True))
        self.assertIn("line-1", live)
        self.assertIn("line-2", live)
        self.assertTrue(result["success"])
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["data"]["follow"])
        self.assertEqual(result["data"]["lines"], 2)
        # The transcript is also in the envelope, so nothing is lost.
        self.assertIn("line-1", result["stdout"])
        json.loads(json.dumps(result))  # must be serialisable as-is

    def test_non_json_follow_writes_the_live_stream_to_stdout(self) -> None:
        self._install(["a\n"])
        args = _args(json=False)
        import contextlib

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            result = self.module._run_follow("h", "tail -f /tmp/x", args, None)
        self.assertIn("a", out.getvalue())
        self.assertEqual(result["data"]["lines"], 1)

    def test_remote_failure_is_classified(self) -> None:
        self._install(["boom\n"], returncode=2)
        result, _ = self._run(_args())
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["data"]["failure_class"], "remote_error")

    def test_keyboard_interrupt_yields_exit_130_and_keeps_the_transcript(self) -> None:
        proc = _FakeProc(["partial\n"], stay_running=True)
        self.module.subprocess.Popen = lambda *a, **k: proc  # type: ignore[assignment]

        real_sleep = self.module.time.sleep
        state = {"raised": False}

        def sleep_and_interrupt(_seconds: float) -> None:
            if not state["raised"]:
                state["raised"] = True
                raise KeyboardInterrupt
            real_sleep(0)

        self.module.time.sleep = sleep_and_interrupt  # type: ignore[assignment]
        try:
            args = _args()
            args.timeout = 60
            result, _ = self._run(args)
        finally:
            self.module.time.sleep = real_sleep  # type: ignore[assignment]
        self.assertTrue(proc.killed)
        self.assertTrue(result["data"]["interrupted"])
        self.assertEqual(result["exit_code"], 130)
        self.assertTrue(result["success"])
        self.assertIn("partial", result["stdout"])

    def test_timeout_is_reported_as_a_timeout(self) -> None:
        self._install(["tick\n"], stay_running=True)
        args = _args()
        args.timeout = 0  # deadline already passed on the first poll
        result, _ = self._run(args)
        self.assertEqual(result["exit_code"], 124)
        self.assertEqual(result["data"]["failure_class"], "timeout")

    def test_tail_is_translated_into_a_remote_tail(self) -> None:
        args = _args(tail=25)
        argv = self.module._build_remote_argv(
            "h", "journalctl -fu nginx | tail -n 25 -f", 8, None,
        )
        self.assertIn("journalctl -fu nginx | tail -n 25 -f", argv)


if __name__ == "__main__":
    unittest.main()
