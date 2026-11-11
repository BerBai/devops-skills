"""Regression tests for the log-aggregator local route (LG-01) and the
Windows terminate race (DEVOPS-BUG-004).

``file://local/<path>`` used to run ``["tail", "-n", "10000", path]`` directly.
On Windows that raises ``FileNotFoundError`` and surfaced as

    rc=127 [WinError 2] 系统找不到指定的文件。

— which names no binary, reads like a permissions problem, and gives no hint
that the *route* is the bug. Two defects, both fixed in ``lib/fetchers.py``:

1. ``local`` now reads the file with Python instead of shelling out to ``tail``.
2. the ``source_spec`` normalisation that prepends ``/`` to every ``file://``
   payload (correct on POSIX) produced ``/C:/Windows/win.ini`` on Windows and
   raised ``WinError 123``. A drive-letter payload now drops the leading slash.

The remote route still runs ``tail``: ``tail -n <--lines>`` for a snapshot and
``tail -F`` for ``--follow``.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "log-aggregator"
    / "skills"
    / "log-aggregator"
    / "scripts"
)

# Same import dance as test_tail_multi_unit.py: several plugins ship a
# top-level ``lib/`` and ``from lib import ...`` caches ``sys.modules['lib']``.
# Evict any prior one, import tail_multi (which pulls its own lib), grab the
# symbols, then restore so the next test module is unaffected.
_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))

import tail_multi  # noqa: E402, I001

fetchers = tail_multi.fetchers
source_spec = tail_multi.source_spec

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]


def _source(tmp_path: Path, text: str) -> tuple[source_spec.ResolvedSource, Path]:
    """Build the ``file://local/<path>`` spec an operator would type."""
    target = tmp_path / "app.log"
    target.write_text(text, encoding="utf-8")
    # as_posix() on Windows is ``C:/...``; source_spec prepends the leading
    # slash, and the local route strips it back off for a drive letter.
    spec = f"file://local/{target.as_posix()}"
    return source_spec.resolve([spec])[0], target


class TestLocalPathNormalisation:
    def test_windows_drive_letter_loses_the_leading_slash(self) -> None:
        if fetchers.os.name != "nt":
            pytest.skip("drive-letter normalisation is Windows-specific")
        assert fetchers._local_path_payload("/C:/Windows/win.ini") == (
            "C:/Windows/win.ini"
        )

    def test_posix_absolute_path_is_untouched(self) -> None:
        assert fetchers._local_path_payload("/var/log/syslog") == "/var/log/syslog"

    def test_relative_path_is_untouched(self) -> None:
        assert fetchers._local_path_payload("logs/app.log") == "logs/app.log"

    def test_a_short_spec_is_untouched(self) -> None:
        # "/C" has no colon and must not be mangled.
        assert fetchers._local_path_payload("/C") == "/C"


class TestLocalFileFetcher:
    def test_reads_the_file_without_tail(self, tmp_path: Path, monkeypatch) -> None:
        """The local route must not spawn `tail` at all."""
        src, _target = _source(tmp_path, "alpha\nbeta\ngamma\n")

        def fail_if_called(*_a, **_k):
            raise AssertionError("local file fetch shelled out to tail")

        monkeypatch.setattr(fetchers.subprocess, "Popen", fail_if_called)
        lines = [ln.raw_body for ln in fetchers.iter_lines(
            src, since="5min", follow=False, timeout=5
        )]
        assert lines == ["alpha", "beta", "gamma"]

    def test_returns_raw_lines_without_timestamps(
        self, tmp_path: Path
    ) -> None:
        src, _target = _source(tmp_path, "one\ntwo\n")
        rows = list(fetchers.iter_lines(
            src, since="5min", follow=False, timeout=5
        ))
        assert [r.raw_body for r in rows] == ["one", "two"]
        assert all(r.raw_ts is None for r in rows)

    def test_missing_file_raises_fetcher_error_naming_the_path(
        self, tmp_path: Path
    ) -> None:
        spec = f"file://local/{(tmp_path / 'gone.log').as_posix()}"
        src = source_spec.resolve([spec])[0]
        with pytest.raises(fetchers.FetcherError) as excinfo:
            list(fetchers.iter_lines(src, since="5min", follow=False, timeout=5))
        assert "gone.log" in excinfo.value.stderr_tail

    def test_follow_picks_up_new_lines(self, tmp_path: Path) -> None:
        src, target = _source(tmp_path, "before\n")
        with target.open("a", encoding="utf-8") as handle:
            handle.write("after\n")
        lines = [
            ln.raw_body
            for ln in fetchers.iter_lines(
                src, since="5min", follow=True, timeout=1
            )
        ]
        assert "before" in lines and "after" in lines


class TestMissingBinaryHint:
    def test_message_names_the_binary_and_the_argv(self) -> None:
        text = fetchers._missing_binary_hint(
            ["tail", "-n", "10000", "/var/log/x"],
            FileNotFoundError(2, "No such file"),
        )
        assert "'tail'" in text
        assert "/var/log/x" in text

    def test_empty_argv_does_not_raise(self) -> None:
        text = fetchers._missing_binary_hint([], FileNotFoundError(2, "x"))
        assert "unknown" in text


class TestRemoteRouteUnchanged:
    def test_remote_file_still_uses_tail(self, monkeypatch) -> None:
        captured = _capture_popen(monkeypatch)
        src = source_spec.resolve(["file://pai/var/log/messages"])[0]
        list(fetchers.iter_lines(src, since="5min", follow=False, timeout=5))
        joined = " ".join(captured["argv"])
        assert "tail -n 10000 /var/log/messages" in joined
        assert captured["argv"][0] == "ssh"

    def test_remote_follow_uses_tail_F(self, monkeypatch) -> None:
        captured = _capture_popen(monkeypatch)
        src = source_spec.resolve(["file://pai/var/log/messages"])[0]
        list(fetchers.iter_lines(src, since="5min", follow=True, timeout=5))
        assert "tail -F /var/log/messages" in " ".join(captured["argv"])


class _FinishedProc:
    """A Popen stand-in that has already exited cleanly."""

    returncode = 0
    stderr = None

    def __init__(self) -> None:
        self.stdout = iter([])

    def poll(self):
        return 0


def _capture_popen(monkeypatch, proc_factory=_FinishedProc) -> dict:
    captured: dict = {}

    def fake_popen(argv, **_kwargs):
        captured["argv"] = argv
        return proc_factory()

    monkeypatch.setattr(fetchers.subprocess, "Popen", fake_popen)
    return captured


# ---------------------------------------------------------------------------
# DEVOPS-GAP-007: --lines threads through to every fetcher
# ---------------------------------------------------------------------------


class TestLinesOption:
    def test_default_is_still_10000(self, monkeypatch) -> None:
        captured = _capture_popen(monkeypatch)
        src = source_spec.resolve(["file://pai/var/log/messages"])[0]
        list(fetchers.iter_lines(src, since="5min", follow=False, timeout=5))
        assert "tail -n 10000 /var/log/messages" in " ".join(captured["argv"])

    def test_remote_file_tail_n_uses_the_requested_count(self, monkeypatch) -> None:
        captured = _capture_popen(monkeypatch)
        src = source_spec.resolve(["file://pai/var/log/messages"])[0]
        list(fetchers.iter_lines(
            src, since="5min", follow=False, timeout=5, lines=10
        ))
        assert "tail -n 10 /var/log/messages" in " ".join(captured["argv"])

    def test_docker_maps_lines_to_tail(self, monkeypatch) -> None:
        captured = _capture_popen(monkeypatch)
        src = source_spec.resolve(["docker://pai/redis"])[0]
        list(fetchers.iter_lines(
            src, since="1h", follow=False, timeout=5, lines=10
        ))
        remote = captured["argv"][-1]
        assert "--tail 10" in remote
        assert "--follow" not in remote

    def test_kube_maps_lines_to_tail(self, monkeypatch) -> None:
        captured = _capture_popen(monkeypatch)
        # An exact pod name skips the `kubectl get pods` enumeration.
        src = source_spec.resolve(["kube://pai/ns1/payment-svc-abc"])[0]
        list(fetchers.iter_lines(
            src, since="1h", follow=False, timeout=5, lines=10
        ))
        assert "--tail=10" in captured["argv"][-1]

    def test_follow_streams_are_not_bounded(self, monkeypatch) -> None:
        captured = _capture_popen(monkeypatch)
        src = source_spec.resolve(["docker://pai/redis"])[0]
        list(fetchers.iter_lines(
            src, since="1h", follow=True, timeout=1, lines=10
        ))
        remote = captured["argv"][-1]
        assert "--tail" not in remote
        assert "--follow" in remote

    def test_local_file_returns_only_the_last_n_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "big.log"
        target.write_text(
            "".join(f"line-{i}\n" for i in range(50)), encoding="utf-8"
        )
        src = source_spec.resolve([f"file://local/{target.as_posix()}"])[0]
        rows = [
            row.raw_body
            for row in fetchers.iter_lines(
                src, since="5min", follow=False, timeout=5, lines=10
            )
        ]
        assert rows == [f"line-{i}" for i in range(40, 50)]

    def test_local_file_with_fewer_lines_than_requested_returns_all(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "small.log"
        target.write_text("a\nb\nc\n", encoding="utf-8")
        src = source_spec.resolve([f"file://local/{target.as_posix()}"])[0]
        rows = [
            row.raw_body
            for row in fetchers.iter_lines(
                src, since="5min", follow=False, timeout=5, lines=10
            )
        ]
        assert rows == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# DEVOPS-BUG-004: the terminate race
# ---------------------------------------------------------------------------


class _FinishingProc:
    """stdout is at EOF while the child is still finishing (the ssh case).

    ``poll()`` answers ``None`` until ``wait()`` reaps it — that is exactly the
    window that used to be misread on Windows: the old code called
    ``terminate()`` there and got ``returncode == 1``.
    """

    def __init__(self, lines, *, final_rc=0, stderr_text="") -> None:
        self.stdout = iter(lines)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = None
        self.killed = False
        self._final_rc = final_rc

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._final_rc
        return self.returncode

    def kill(self):  # pragma: no cover - must not be reached
        self.killed = True
        self.returncode = 1


class _WedgedProc(_FinishingProc):
    """Never exits on its own: only ``kill()`` ends it."""

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=timeout or 0)

    def kill(self):
        self.killed = True
        # Windows: TerminateProcess leaves rc == 1, not -SIGKILL.
        self.returncode = 1


class TestTerminateRace:
    def test_process_still_alive_after_stdout_eof_is_not_a_failure(
        self, monkeypatch
    ) -> None:
        procs: list[_FinishingProc] = []

        def factory():
            proc = _FinishingProc(
                ["localhost.localdomain\n"],
                final_rc=0,
                stderr_text="\nAuthorized users only. All activities may be "
                            "monitored and reported.\n",
            )
            procs.append(proc)
            return proc

        _capture_popen(monkeypatch, factory)
        src = source_spec.resolve(["file://pai/etc/hostname"])[0]
        lines = list(fetchers.iter_lines(
            src, since="5min", follow=False, timeout=5
        ))
        assert [row.raw_body for row in lines] == ["localhost.localdomain"]
        # It exited during the grace period, so it was never killed.
        assert procs[0].killed is False

    def test_process_we_had_to_kill_is_not_a_failure(self, monkeypatch) -> None:
        """Windows terminate() -> rc 1 must not be read as a fetch error."""

        def factory():
            return _WedgedProc(["line\n"], final_rc=0)

        _capture_popen(monkeypatch, factory)
        src = source_spec.resolve(["file://pai/etc/hostname"])[0]
        assert [
            row.raw_body
            for row in fetchers.iter_lines(
                src, since="5min", follow=False, timeout=5
            )
        ] == ["line"]

    def test_real_non_zero_exit_still_raises(self) -> None:
        """A genuinely failing command is still a fetch failure."""
        with pytest.raises(fetchers.FetcherError) as excinfo:
            list(fetchers._run_cmd(
                "local",
                [sys.executable, "-c", "import sys; sys.exit(3)"],
                timeout=10,
            ))
        assert excinfo.value.returncode == 3

    def test_false_command_still_raises(self) -> None:
        with pytest.raises(fetchers.FetcherError) as excinfo:
            list(fetchers._run_cmd(
                "local", [sys.executable, "-c", "raise SystemExit(1)"], timeout=10
            ))
        assert excinfo.value.returncode == 1

    def test_real_successful_command_yields_lines(self) -> None:
        out = list(fetchers._run_cmd(
            "local",
            [sys.executable, "-c", "print('one'); print('two')"],
            timeout=10,
        ))
        assert out == ["one", "two"]

    def test_failure_surfaces_the_real_error_not_the_login_banner(
        self, monkeypatch
    ) -> None:
        stderr_text = (
            "\nAuthorized users only. All activities may be monitored and "
            "reported.\ntail: cannot open '/nope.log' for reading: "
            "No such file or directory\n"
        )
        _capture_popen(
            monkeypatch, lambda: _FinishingProc([], final_rc=1, stderr_text=stderr_text)
        )
        with pytest.raises(fetchers.FetcherError) as excinfo:
            list(fetchers._run_cmd(
                "pai", ["tail", "-n", "10", "/nope.log"], timeout=5
            ))
        tail = excinfo.value.stderr_tail
        assert "cannot open '/nope.log'" in tail
        assert "Authorized users only" not in tail

    def test_closing_the_stream_early_is_not_a_failure(self, monkeypatch) -> None:
        """A caller that stops reading (bounded drain) must not get an error.

        ``_run_cmd`` is a generator; closing it raises GeneratorExit inside
        the body. The ``finally`` block kills the child, and the kill must not
        be converted into a FetcherError — a partially consumed stream that we
        ourselves ended is exactly the normal ``--follow`` shutdown path.
        """
        _capture_popen(
            monkeypatch, lambda: _WedgedProc(["one\n", "two\n", "three\n"])
        )
        stream = fetchers._run_cmd("pai", ["tail", "-F", "/var/log/x"], timeout=5)
        assert next(stream) == "one"
        stream.close()  # must not raise


class TestBannerStripping:
    def test_banner_only_stderr_becomes_empty(self) -> None:
        cleaned = fetchers._strip_login_banner(
            "\nAuthorized users only. All activities may be monitored and reported.\n"
        )
        assert cleaned == ""

    def test_genuine_error_survives(self) -> None:
        cleaned = fetchers._strip_login_banner(
            "Authorized users only. All activities may be monitored and reported.\n"
            "docker: Error response from daemon: No such container: redis.\n"
        )
        assert cleaned == "docker: Error response from daemon: No such container: redis."

    def test_last_login_banner_is_noise_too(self) -> None:
        cleaned = fetchers._strip_login_banner(
            "Last failed login: Mon Nov  5 10:00:00 CST 2026 from 10.0.0.1\n"
            "tail: cannot open '/x' for reading: Permission denied\n"
        )
        assert cleaned == "tail: cannot open '/x' for reading: Permission denied"

    def test_tail_cap_keeps_the_end_of_a_long_stream(self) -> None:
        noisy = "\n".join(f"warning {i}" for i in range(500))
        noisy += "\nthe real failure\n"
        cleaned = fetchers._stderr_tail(noisy)
        assert len(cleaned) <= fetchers._STDERR_TAIL_BYTES
        assert cleaned.endswith("the real failure")
