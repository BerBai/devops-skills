"""Regression tests for the local-route defects LS-01/02/03 and DQ-01.

The three defects these cover are all the same shape: **a check that could not
run was reported as a check that ran and failed.**

LS-01  ``port_check.py local`` piped a POSIX ``if command -v nc ...`` one-liner
       through ``cmd /c``. ``cmd.exe`` answered ``-v was unexpected at this
       time.``, exited 1, and ``_classify_status`` classified that as
       ``filtered`` -- indistinguishable from a real firewall drop. A port that
       was demonstrably open (``connect_ex == 0``) was reported as filtered.
LS-02  ``tail_log.py local`` ran ``tail -n N <path>`` through the same shell and
       returned ``failure_class: remote_error`` on Windows, with no hint that
       the *route* was the problem rather than the file.
LS-03  ``compare_across_hosts.py local`` reported both sides as "errored" with
       no failure_class at all, so "the command never ran" and "the two hosts
       agree on a failure" looked identical.
DQ-01  ``docker-quick`` reported a missing local ``docker`` binary as
       ``remote_error`` (check the network!) instead of ``dependency_missing``
       (install docker), and had no ``docker`` → ``podman`` autodetection.

The tests are deliberately split by mechanism:

* :class:`TestShellFailureDetection` -- pure unit tests of the classifier that
  decides "the command never ran". Runs on every platform.
* :class:`TestPortProbe` -- the native socket probe, exercised against a real
  listening socket created in the test. Runs on every platform.
* :class:`TestPortCheckLocalRoute` -- the end-to-end fix for LS-01: a local cell
  must never consult ``subprocess`` at all.
* :class:`TestTailFile` / :class:`TestTailLogLocalRoute` -- LS-02.
* :class:`TestDockerRuntime` -- DQ-01.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_DEBUG = (
    REPO_ROOT / "plugins" / "remote-debug" / "skills" / "remote-debug" / "scripts"
)
DOCKER_QUICK = (
    REPO_ROOT / "plugins" / "docker-quick" / "skills" / "docker-quick" / "scripts"
)
for _p in (REMOTE_DEBUG, DOCKER_QUICK):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _local_exec  # noqa: E402, I001
import _runtime  # noqa: E402, I001
import port_check  # noqa: E402, I001
import tail_log  # noqa: E402, I001

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_DEPENDENCY_MISSING = 3


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _completed(returncode=0, stdout="", stderr=""):
    from types import SimpleNamespace

    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def listening_socket():
    """A real TCP listener on an ephemeral port, closed afterwards."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    try:
        yield port
    finally:
        sock.close()


def _closed_port() -> int:
    """A port that is bound and closed, so nothing is listening on it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# -----------------------------------------------------------------------------
# shell_argv / is_windows
# -----------------------------------------------------------------------------


class TestShellSelection:
    def test_windows_uses_cmd_c(self) -> None:
        assert _local_exec.shell_argv("echo hi", windows=True) == [
            "cmd", "/c", "echo hi",
        ]

    def test_posix_uses_sh_c(self) -> None:
        assert _local_exec.shell_argv("echo hi", windows=False) == [
            "sh", "-c", "echo hi",
        ]

    def test_production_detects_the_real_platform(self) -> None:
        argv = _local_exec.shell_argv("echo hi")
        expected = "cmd" if os.name == "nt" else "sh"
        assert argv[0] == expected


# -----------------------------------------------------------------------------
# LS-01/02/03a: "the command never ran" is not an answer
# -----------------------------------------------------------------------------


class TestShellFailureDetection:
    """The classifier that stops a shell error becoming a probe result."""

    def test_cmd_syntax_error_is_a_shell_error(self) -> None:
        # The exact text cmd.exe produced for the old port_check one-liner.
        assert _local_exec.classify_shell_failure(
            1, "", "-v was unexpected at this time.\r\n"
        ) == "local_shell_error"

    def test_cmd_syntax_error_variant(self) -> None:
        assert _local_exec.classify_shell_failure(
            1, "", "The syntax of the command is incorrect.\r\n"
        ) == "local_shell_error"

    def test_sh_syntax_error_is_a_shell_error(self) -> None:
        assert _local_exec.classify_shell_failure(
            2, "", "sh: 1: Syntax error: unexpected end of file\n"
        ) == "local_shell_error"

    def test_cmd_missing_binary_is_dependency_missing(self) -> None:
        assert _local_exec.classify_shell_failure(
            1,
            "",
            "'tail' is not recognized as an internal or external command,\n"
            "operable program or batch file.\n",
        ) == "dependency_missing"

    def test_sh_missing_binary_is_dependency_missing(self) -> None:
        assert _local_exec.classify_shell_failure(
            127, "", "sh: 1: nc: not found\n"
        ) == "dependency_missing"

    def test_exit_127_alone_is_dependency_missing(self) -> None:
        # sh's own exit code for "command not found", even with no message.
        assert _local_exec.classify_shell_failure(
            127, "", ""
        ) == "dependency_missing"

    def test_a_real_answer_is_not_reclassified(self) -> None:
        """The whole point: a probe that ran keeps its exit code."""
        assert _local_exec.classify_shell_failure(
            1, "", "nc: connect to 10.0.0.1 port 5432 (tcp) failed: "
            "Connection refused\n"
        ) is None

    def test_success_is_never_a_failure(self) -> None:
        assert _local_exec.classify_shell_failure(0, "output", "") is None


class TestRunShell:
    def test_syntax_error_envelope(self, monkeypatch) -> None:
        monkeypatch.setattr(
            _local_exec.subprocess,
            "run",
            lambda *_a, **_k: _completed(1, "", "-v was unexpected at this time."),
        )
        env = _local_exec.run_shell("probe", "if command -v nc; then ...", 5)
        assert env["success"] is False
        assert env["data"]["failure_class"] == "local_shell_error"
        assert env["data"]["shell_returncode"] == 1

    def test_missing_binary_envelope(self, monkeypatch) -> None:
        monkeypatch.setattr(
            _local_exec.subprocess,
            "run",
            lambda *_a, **_k: _completed(
                1, "", "'tail' is not recognized as an internal or external command"
            ),
        )
        env = _local_exec.run_shell("tail_log", "tail -n 5 /var/log/x", 5)
        assert env["data"]["failure_class"] == "dependency_missing"

    def test_real_answer_keeps_its_exit_code(self, monkeypatch) -> None:
        monkeypatch.setattr(
            _local_exec.subprocess,
            "run",
            lambda *_a, **_k: _completed(1, "", "Connection refused"),
        )
        env = _local_exec.run_shell("probe", "nc -zv h 1", 5)
        assert env["success"] is False
        assert env["data"]["failure_class"] is None
        assert env["exit_code"] == 1

    def test_timeout_envelope(self, monkeypatch) -> None:
        def raise_timeout(*_a, **_k):
            raise _local_exec.subprocess.TimeoutExpired(cmd="x", timeout=5)

        monkeypatch.setattr(_local_exec.subprocess, "run", raise_timeout)
        env = _local_exec.run_shell("probe", "sleep 99", 5)
        assert env["data"]["failure_class"] == "timeout"
        assert env["exit_code"] == _local_exec.EXIT_TIMEOUT

    def test_missing_shell_itself(self, monkeypatch) -> None:
        def raise_fnf(*_a, **_k):
            raise FileNotFoundError("cmd")

        monkeypatch.setattr(_local_exec.subprocess, "run", raise_fnf)
        env = _local_exec.run_shell("probe", "echo", 5, windows=True)
        assert env["data"]["failure_class"] == "dependency_missing"
        assert env["exit_code"] == EXIT_DEPENDENCY_MISSING

    def test_decodes_utf8_not_the_ansi_codepage(self, monkeypatch) -> None:
        """A Chinese Windows returns UTF-8 from PowerShell once told to.

        Before the fix ``text=True`` decoded with the ANSI code page (GBK),
        which raised ``UnicodeDecodeError`` inside the reader thread and
        produced an empty envelope with a stray traceback on stderr.
        """
        seen: dict = {}

        def fake_run(_argv, **kwargs):
            seen.update(kwargs)
            return _completed(0, "2026年11月4日", "")

        monkeypatch.setattr(_local_exec.subprocess, "run", fake_run)
        _local_exec.run_shell("probe", "date", 5)
        assert seen["encoding"] == "utf-8"
        assert seen["errors"] == "replace"


# -----------------------------------------------------------------------------
# LS-01: native TCP/UDP probes
# -----------------------------------------------------------------------------


class TestPortProbe:
    def test_tcp_open_port_is_open(self, listening_socket) -> None:
        env = _local_exec.tcp_probe("127.0.0.1", listening_socket, 2.0)
        assert env["success"] is True
        assert env["exit_code"] == 0
        assert "succeeded" in env["stdout"]
        assert env["data"]["route"] == "local"

    def test_tcp_closed_port_says_refused(self) -> None:
        """The regression that started all of this.

        On Windows a closed localhost port needs a timeout longer than the
        SYN retry window before the stack reports ECONNREFUSED; with too short
        a timeout the honest answer is ``Operation timed out``.
        """
        env = _local_exec.tcp_probe("127.0.0.1", _closed_port(), 5.0)
        assert env["success"] is False
        assert "Connection refused" in env["stderr"]

    def test_tcp_nc_shaped_output_is_classifiable(self, listening_socket) -> None:
        """The envelope must feed the SHARED classifier unchanged."""
        env = _local_exec.tcp_probe("127.0.0.1", listening_socket, 2.0)
        assert port_check._classify_status(env) == "open"

    def test_tcp_refused_is_classifiable(self) -> None:
        env = _local_exec.tcp_probe("127.0.0.1", _closed_port(), 5.0)
        assert port_check._classify_status(env) == "refused"

    def test_unresolvable_host_is_a_host_error(self, monkeypatch) -> None:
        def _unresolvable(*args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(_local_exec.socket, "getaddrinfo", _unresolvable)
        env = _local_exec.tcp_probe("no-such-host.invalid", 80, 1.0)
        assert env["success"] is False
        assert env["data"]["failure_class"] == "local_host_error"
        # The shared classifier still sees "getaddrinfo" and says host-error.
        assert port_check._classify_status(env) == "host-error"

    def test_udp_silence_is_open_or_filtered(self) -> None:
        """UDP cannot distinguish open from dropped; it must not pretend to."""
        env = _local_exec.udp_probe("127.0.0.1", _closed_port(), 0.4)
        assert port_check._classify_status(env, udp=True) in {
            "open-or-filtered", "refused",
        }

    def test_udp_problem_never_raises(self, monkeypatch) -> None:
        def _unresolvable(*args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(_local_exec.socket, "getaddrinfo", _unresolvable)
        env = _local_exec.udp_probe("no-such-host.invalid", 53, 0.4)
        assert env["success"] is False
        assert env["data"]["failure_class"] == "local_host_error"

    def test_port_probe_dispatches(self, listening_socket) -> None:
        tcp = _local_exec.port_probe("127.0.0.1", listening_socket, 2.0)
        assert tcp["data"]["probe"] == "tcp"
        udp = _local_exec.port_probe("127.0.0.1", listening_socket, 0.4, udp=True)
        assert udp["data"]["probe"] == "udp"


class TestPortCheckLocalRoute:
    """LS-01 end to end: the local route must not shell out at all."""

    def test_local_cell_never_calls_subprocess(
        self, monkeypatch, listening_socket
    ) -> None:
        def fail_if_called(*_a, **_k):
            raise AssertionError(
                "the local route shelled out; it must use the native probe"
            )

        monkeypatch.setattr(port_check.subprocess, "run", fail_if_called)
        rc = port_check.main([
            "local", "--target", "127.0.0.1",
            "--ports", str(listening_socket), "--json",
        ])
        assert rc == EXIT_OK

    def test_local_open_port_reports_open(
        self, monkeypatch, capsys, listening_socket
    ) -> None:
        monkeypatch.setattr(
            port_check.subprocess,
            "run",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no shell")),
        )
        rc = port_check.main([
            "local", "--target", "127.0.0.1",
            "--ports", str(listening_socket), "--json",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_OK
        assert payload["data"]["matrix"][0]["status"] == "open"

    def test_local_closed_port_is_not_silently_filtered(self, monkeypatch, capsys) -> None:
        """The exact LS-01 symptom: "filtered" when nothing was probed.

        A native probe always knows whether it ran, so the answer here is a
        real ``refused`` (or, on a slow stack, ``filtered`` because the connect
        genuinely timed out) -- never ``source-error``, and the envelope must
        carry the errno that proves a probe happened.
        """
        monkeypatch.setattr(
            port_check.subprocess,
            "run",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no shell")),
        )
        port_check.main([
            "local", "--target", "127.0.0.1",
            "--ports", str(_closed_port()), "--timeout", "5", "--json",
        ])
        payload = json.loads(capsys.readouterr().out)
        cell = payload["data"]["matrix"][0]
        assert cell["status"] in {"refused", "filtered"}
        assert cell["status"] != "source-error"

    def test_shell_error_classifies_as_source_error(self) -> None:
        """A shell that never ran the probe is a source error, not "filtered"."""
        env = {
            "success": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "-v was unexpected at this time.",
            "data": {"failure_class": "local_shell_error"},
        }
        assert port_check._classify_status(env) == "source-error"

    def test_dependency_missing_classifies_as_source_error(self) -> None:
        env = {
            "success": False,
            "exit_code": 127,
            "stdout": "",
            "stderr": "nc not found",
            "data": {"failure_class": "dependency_missing"},
        }
        assert port_check._classify_status(env) == "source-error"

    def test_remote_route_still_shells_out(self, monkeypatch, capsys) -> None:
        """The fix is local-only; the remote contract is unchanged."""
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return _completed(0, stdout=json.dumps({
                "success": True, "exit_code": 0,
                "stdout": "Connection to db 5432 port [tcp/*] succeeded!",
                "stderr": "", "data": {},
            }), stderr="")

        monkeypatch.setattr(port_check.subprocess, "run", fake_run)
        rc = port_check.main([
            "pai", "--target", "db", "--ports", "5432", "--json",
        ])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_OK
        assert payload["data"]["matrix"][0]["status"] == "open"
        assert "ssh_execute.py" in " ".join(captured["argv"])


# -----------------------------------------------------------------------------
# LS-02: native tail
# -----------------------------------------------------------------------------


class TestTailFile:
    def test_reads_last_n_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "app.log"
        target.write_text(
            "".join(f"line-{i}\n" for i in range(10)), encoding="utf-8"
        )
        env = _local_exec.tail_file(str(target), 3)
        assert env["success"] is True
        assert env["stdout"] == "line-7\nline-8\nline-9\n"

    def test_missing_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        env = _local_exec.tail_file(str(tmp_path / "nope.log"), 3)
        assert env["success"] is False
        assert env["data"]["failure_class"] == "local_not_found"
        assert "no such file" in env["stderr"].lower()
        # No `tail` runs on this route, so the message must not blame one.
        assert "tail: cannot open" not in env["stderr"]

    def test_whole_file_when_shorter_than_n(self, tmp_path: Path) -> None:
        target = tmp_path / "short.log"
        target.write_text("only\n", encoding="utf-8")
        env = _local_exec.tail_file(str(target), 100)
        assert env["stdout"] == "only\n"

    def test_seek_boundary_does_not_emit_a_partial_line(self, tmp_path: Path) -> None:
        """A bounded seek joins mid-line; the fragment must be dropped."""
        target = tmp_path / "big.log"
        rows = [f"row-{i:04d}-{'x' * 40}\n" for i in range(50)]
        target.write_text("".join(rows), encoding="utf-8")
        env = _local_exec.tail_file(str(target), 2, max_bytes=100)
        first = env["stdout"].splitlines()[0]
        assert first.startswith("row-") and len(first) == len("row-0000-" + "x" * 40)

    def test_follow_picks_up_appended_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "live.log"
        target.write_text("before\n", encoding="utf-8")
        env = _local_exec.tail_file_follow(str(target), 5, 1.2)
        assert env["success"] is True
        assert "before" in env["stdout"]
        assert env["data"]["follow"] is True


class TestTailLogLocalRoute:
    """LS-02 end to end: `local` must not need a `tail` binary."""

    def test_local_path_mode_reads_the_file(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        target = tmp_path / "app.log"
        target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        monkeypatch.setattr(
            tail_log.subprocess,
            "run",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("local path mode shelled out")
            ),
        )
        rc = tail_log.main(["local", str(target), "--lines", "2", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_OK
        assert payload["data"]["lines"] == ["beta", "gamma"]

    def test_local_path_mode_grep_still_applies(self, capsys, tmp_path: Path) -> None:
        target = tmp_path / "app.log"
        target.write_text("a ERROR x\nb info\nc ERROR y\n", encoding="utf-8")
        tail_log.main(["local", str(target), "--grep", "ERROR", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["lines"] == ["a ERROR x", "c ERROR y"]

    def test_local_unit_mode_explains_itself(self, capsys) -> None:
        """journalctl has no portable equivalent; say so, don't say remote_error."""
        rc = tail_log.main(["local", "--unit", "nginx", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_FAIL
        assert payload["data"]["failure_class"] == "local_shell_error"
        assert "journalctl" in payload["stderr"]
        assert "POSIX" in payload["stderr"]

    def test_local_missing_file_is_not_a_shell_error(self, capsys, tmp_path: Path) -> None:
        rc = tail_log.main(["local", str(tmp_path / "gone.log"), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_FAIL
        assert payload["data"]["failure_class"] == "local_not_found"

    def test_remote_route_unchanged(self, monkeypatch, capsys) -> None:
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return _completed(0, stdout=json.dumps({
                "success": True, "exit_code": 0, "stdout": "remote-line\n",
                "stderr": "", "data": {},
            }), stderr="")

        monkeypatch.setattr(tail_log.subprocess, "run", fake_run)
        rc = tail_log.main(["pai", "/var/log/syslog", "--lines", "1", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_OK
        assert payload["data"]["lines"] == ["remote-line"]
        assert "tail -n 1" in " ".join(captured["argv"])


# -----------------------------------------------------------------------------
# LS-03: compare_across_hosts keeps the failure class when both sides fail
# -----------------------------------------------------------------------------


class TestCompareKeepsFailureClass:
    def test_both_sides_failing_carries_the_class(self) -> None:
        """`side: both` with no class made "never ran" look like "they agree"."""
        import compare_across_hosts as cah

        cell = cah._assemble_comparison(
            kind="command",
            target="uname -r",
            baseline_host="local",
            other_host="local",
            base_content=None,
            other_content=None,
            base_info={
                "remote_exit_code": 1,
                "remote_stderr": "'uname' is not recognized...",
                "failure_class": "dependency_missing",
            },
            other_info={
                "remote_exit_code": 1,
                "remote_stderr": "'uname' is not recognized...",
                "failure_class": "dependency_missing",
            },
            context=3,
        )
        assert cell["error"]["side"] == "both"
        assert cell["error"]["failure_class"] == "dependency_missing"
        assert cell["error"]["other_failure_class"] == "dependency_missing"


# -----------------------------------------------------------------------------
# DQ-01: container runtime resolution
# -----------------------------------------------------------------------------


class TestDockerRuntime:
    def test_explicit_runtime_is_never_second_guessed(self) -> None:
        resolved, err = _runtime.resolve_runtime(
            "podman", host="local", finder=lambda: None
        )
        assert resolved == "podman"
        assert err is None

    def test_auto_prefers_docker(self) -> None:
        resolved, err = _runtime.resolve_runtime(
            _runtime.RUNTIME_AUTO, host="local", finder=lambda: "docker"
        )
        assert resolved == "docker" and err is None

    def test_auto_falls_back_to_podman(self) -> None:
        resolved, err = _runtime.resolve_runtime(
            _runtime.RUNTIME_AUTO, host="local", finder=lambda: "podman"
        )
        assert resolved == "podman" and err is None

    def test_auto_with_neither_installed_still_picks_docker(self) -> None:
        """A PATH probe is a hint; the real invocation reports the truth.

        Falling back to ``docker`` keeps the error message specific
        ("no 'docker' executable") instead of pre-empting the call.
        """
        resolved, err = _runtime.resolve_runtime(
            _runtime.RUNTIME_AUTO, host="local", finder=lambda: None
        )
        assert resolved == "docker"
        assert err is None

    def test_auto_on_a_remote_host_does_not_probe_locally(self) -> None:
        """`shutil.which` here says nothing about the far side."""
        called: list[int] = []

        def finder():
            called.append(1)
            return "podman"

        resolved, _err = _runtime.resolve_runtime(
            _runtime.RUNTIME_AUTO, host="pai", finder=finder
        )
        assert resolved == "docker"
        assert called == []

    def test_missing_binary_is_dependency_missing(self) -> None:
        assert _runtime.classify_local_exit(
            FileNotFoundError("docker"), None
        ) == "dependency_missing"

    def test_bad_cwd_is_a_project_dir_problem(self) -> None:
        assert _runtime.classify_local_exit(
            NotADirectoryError(20, "not a dir"), None
        ) == "project_dir_invalid"


class TestSpawnFailureAmbiguity:
    """C6: a bad cwd and a missing binary raise the same exception on Windows.

    ``subprocess.run(["no-such-bin"], cwd=bad)`` raises ``FileNotFoundError``
    with ``errno=2`` and ``filename=None`` on Windows, so the exception alone
    cannot say which half was wrong. The cwd is checkable; the binary is not.
    """

    def test_bad_cwd_wins_over_missing_binary(self, tmp_path) -> None:
        missing = str(tmp_path / "no" / "such" / "dir")
        assert _runtime.classify_spawn_failure(
            FileNotFoundError(2, "No such file"), missing
        ) == "project_dir_invalid"

    def test_missing_binary_keeps_dependency_missing(self, tmp_path) -> None:
        assert _runtime.classify_spawn_failure(
            FileNotFoundError(2, "No such file"), str(tmp_path)
        ) == "dependency_missing"

    def test_no_cwd_means_the_binary_is_the_suspect(self) -> None:
        assert _runtime.classify_spawn_failure(
            FileNotFoundError(2, "No such file"), None
        ) == "dependency_missing"

    def test_not_a_directory_error(self, tmp_path) -> None:
        assert _runtime.classify_spawn_failure(
            NotADirectoryError(20, "Not a directory"), str(tmp_path)
        ) == "project_dir_invalid"

    def test_windows_winerror_267_is_a_directory_problem(self, tmp_path) -> None:
        exc = OSError(22, "The directory name is invalid")
        exc.winerror = 267  # type: ignore[attr-defined]
        assert _runtime.classify_spawn_failure(
            exc, str(tmp_path)
        ) == "project_dir_invalid"

    def test_other_os_errors_are_local_errors(self, tmp_path) -> None:
        assert _runtime.classify_spawn_failure(
            PermissionError(13, "denied"), str(tmp_path)
        ) == "local_error"

    def test_compose_end_to_end_bad_cwd(self, monkeypatch, capsys, tmp_path) -> None:
        """The regression: exit 3 with `dependency_missing` was the wrong answer."""
        import compose_status as cs

        def raise_fnf(*_a, **_k):
            raise FileNotFoundError(2, "No such file")

        monkeypatch.setattr(cs.subprocess, "run", raise_fnf)
        rc = cs.main(["local", str(tmp_path / "gone"), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_FAIL
        assert payload["data"]["failure_class"] == "project_dir_invalid"

    def test_compose_end_to_end_missing_binary(self, monkeypatch, capsys, tmp_path) -> None:
        import compose_status as cs

        def raise_fnf(*_a, **_k):
            raise FileNotFoundError(2, "No such file")

        monkeypatch.setattr(cs.subprocess, "run", raise_fnf)
        rc = cs.main(["local", str(tmp_path), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == EXIT_DEPENDENCY_MISSING
        assert payload["data"]["failure_class"] == "dependency_missing"

    def test_a_real_docker_answer_is_left_alone(self) -> None:
        assert _runtime.classify_local_exit(None, 1) == "remote_error"
        assert _runtime.classify_local_exit(None, 0) is None

    def test_inspect_container_reports_dependency_missing(self, monkeypatch) -> None:
        import inspect_container as ic

        def raise_fnf(*_a, **_k):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(ic.subprocess, "run", raise_fnf)
        env = ic._inspect_one("local", "ctr", "docker", 8)
        assert env["data"]["failure_class"] == "dependency_missing"
        assert env["exit_code"] == EXIT_DEPENDENCY_MISSING
